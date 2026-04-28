"""
item_sale_monitor.py  — фоновый поток мониторинга продажи предметов на маркетплейсе.

Логика:
1. Периодически (каждые `item_check_interval` секунд) запускает полный цикл проверки.
2. Вызывает item_actions.full_monitor_cycle() — навигация + поиск + таблица + repost.
3. Уведомляет GUI через on_status_change callback.

Интеграция:
    from item_sale_monitor import ItemSaleMonitor, TrackedItem
    monitor = ItemSaleMonitor(
        cfg_provider=lambda: cfg,
        run_event=run_event,
        stop_event=stop_event,
        log=log_fn,
        on_status_change=on_status_change_cb,
    )
    monitor.start()

    # Автономный режим (без основного бота аренды):
    monitor = ItemSaleMonitor(
        cfg_provider=lambda: cfg,
        run_event=run_event,
        stop_event=stop_event,
        log=log_fn,
        on_status_change=on_status_change_cb,
        standalone=True,
    )
    monitor.start()

    # Получение статистики через callback:
    def on_stats(stats: dict):
        print(stats["total_reposts"], stats["total_errors"])

    monitor = ItemSaleMonitor(
        cfg_provider=lambda: cfg,
        run_event=run_event,
        stop_event=stop_event,
        log=log_fn,
        on_status_change=on_status_change_cb,
        on_stats_update=on_stats,
    )
    monitor.start()

    # on_stats_update : callable(dict) | None
    #     Вызывается после каждого цикла проверки со словарём статистики.
    #     Ключи словаря:
    #       total_reposts    — суммарное количество репостов с момента запуска
    #       total_errors     — суммарное количество ошибок с момента запуска
    #       start_time       — время запуска потока (time.time())
    #       last_repost_time — время последнего успешного репоста (time.time() или None)
    #       is_alive         — True, если поток активен
    #       last_results     — список ItemCheckResult последнего цикла
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ─── Status codes ──────────────────────────────────────────────────────────────
STATUS_TOP         = "TOP"          # мы первые — всё хорошо
STATUS_OUTBID      = "OUTBID"       # нас перебили
STATUS_NOT_FOUND   = "NOT_FOUND"    # предмет не найден в поиске
STATUS_NO_LOT      = "NO_LOT"       # наш лот отсутствует
STATUS_ERROR       = "ERROR"        # техническая ошибка
STATUS_PRICE_FLOOR = "PRICE_FLOOR"  # нас перебили, но опускать ниже минимума невыгодно


@dataclass
class ItemCheckResult:
    item_name:    str
    status:       str
    our_price:    float = 0.0
    best_price:   float = 0.0
    best_seller:  str   = ""
    rank:         int   = 0
    reposted:     bool  = False
    reason:       str   = ""


@dataclass
class TrackedItem:
    """Один отслеживаемый предмет."""
    name:          str           # название для поиска
    our_player:    str           # имя нашего игрока
    target_price:  float         # желаемая цена (отображается в GUI)
    min_price:     float = 0.0   # минимально допустимая цена (0 = не задана)
    last_status:   str   = ""
    last_price:    float = 0.0
    last_checked:  float = 0.0   # timestamp
    check_count:   int   = 0
    repost_count:  int   = 0


class ItemSaleMonitor:
    """
    Фоновый поток, который периодически проверяет статус продажи предметов.

    Параметры
    ---------
    cfg_provider : callable -> dict
        Функция, возвращающая актуальный конфиг (читается при каждой итерации).
    run_event : threading.Event
        Сигнал «бот запущен» — при сбросе поток засыпает, но не завершается.
        Игнорируется если standalone=True.
    stop_event : threading.Event
        Внешний сигнал «завершить поток» — используется ТОЛЬКО ДЛЯ ЧТЕНИЯ.
        Мониторинг завершается когда взведён наш собственный _own_stop
        ИЛИ когда взведён внешний stop_event (App.stop_event).
        stop() взводит только _own_stop — НЕ трогает внешний stop_event.
    log : callable(str)
        Функция логирования.
    on_status_change : callable(list[ItemCheckResult]) | None
        Вызывается после каждого цикла проверки с результатами.
    standalone : bool
        Если True — монитор работает независимо от run_event (не ждёт запуска
        основного бота аренды). Используется когда Items Monitor запущен
        без основного цикла аренды.
    """

    def __init__(
        self,
        cfg_provider:      Callable[[], Dict[str, Any]],
        run_event:         threading.Event,
        stop_event:        threading.Event,
        log:               Callable[[str], None],
        on_status_change:  Optional[Callable[[List[ItemCheckResult]], None]] = None,
        standalone:        bool = False,
        on_stats_update:   Optional[Callable[[dict], None]] = None,
        loop_idle_event:   Optional[threading.Event] = None,
        items_busy_event:  Optional[threading.Event] = None,
    ) -> None:
        self._cfg_provider     = cfg_provider
        self._run_event        = run_event
        # Внешний stop_event (App.stop_event) — только читаем, никогда не взводим
        self._ext_stop_event   = stop_event
        # Собственный стоп-ивент — взводится только через наш stop()
        # Это ключевой инвариант: stop() не убивает LoopManager и другие компоненты
        self._own_stop         = threading.Event()
        self.log               = log
        self._on_status_change = on_status_change
        self._standalone       = standalone
        self._on_stats_update  = on_stats_update
        # Event от LoopManager — взводится когда основной бот ожидает
        # (все машины выставлены/заняты, экран маркетплейса свободен)
        self._loop_idle_event: Optional[threading.Event] = loop_idle_event
        # Флаг занятости монитора предметов — устанавливается перед full_monitor_cycle,
        # сбрасывается в finally после. LoopManager ждёт его сброса перед началом sweep.
        self._items_busy_event: Optional[threading.Event] = items_busy_event

        self._thread: Optional[threading.Thread] = None

        # Статистика работы монитора
        self._start_time:       Optional[float]          = None  # время запуска (time.time())
        self._total_reposts:    int                      = 0     # всего репостов с начала
        self._total_errors:     int                      = 0     # всего ошибок
        self._last_repost_time: Optional[float]          = None  # time.time() последнего репоста
        self._last_results:     List[ItemCheckResult]    = []    # последние результаты

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Запустить фоновый поток (если ещё не запущен)."""
        if self._thread and self._thread.is_alive():
            return
        self._own_stop.clear()  # сбрасываем перед каждым стартом
        self._start_time = time.time()  # фиксируем время запуска
        self._thread = threading.Thread(
            target=self._run,
            name="ItemSaleMonitor",
            daemon=True,
        )
        self._thread.start()
        if self._standalone:
            self.log("ItemSaleMonitor запущен (автономный режим)")
        else:
            self.log("ItemSaleMonitor запущен")

    def stop(self) -> None:
        """
        Попросить поток остановиться.
        Взводит только СОБСТВЕННЫЙ стоп-ивент (_own_stop).
        Внешний App.stop_event НЕ трогается — это позволяет безопасно
        пересоздавать монитор в _restart_item_monitor() не убивая LoopManager.
        """
        self._own_stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def get_stats(self) -> dict:
        """Возвращает статистику работы монитора."""
        return {
            "total_reposts":    self._total_reposts,
            "total_errors":     self._total_errors,
            "start_time":       self._start_time,
            "last_repost_time": self._last_repost_time,
            "is_alive":         self.is_alive(),
            "last_results":     list(self._last_results),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _should_stop(self) -> bool:
        """
        Проверяет нужно ли завершать поток.
        Завершаем если взведён собственный стоп ИЛИ внешний App.stop_event.
        """
        return self._own_stop.is_set() or self._ext_stop_event.is_set()

    def _sleep_coop(self, seconds: float) -> bool:
        """
        Кооперативный sleep — просыпаемся каждые 0.5 с и проверяем stop.
        Возвращает True, если нужно выйти.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._should_stop():
                return True
            time.sleep(min(0.5, deadline - time.monotonic()))
        return False

    def _wait_for_run(self) -> bool:
        """
        Ожидать, пока run_event не будет установлен.
        Возвращает True, если нужно выйти (стоп взведён).

        В standalone-режиме: не ждём run_event, работаем всегда.
        """
        # standalone-режим: не ждём run_event, работаем всегда
        if self._standalone:
            return self._should_stop()
        while not self._run_event.is_set():
            if self._should_stop():
                return True
            time.sleep(0.5)
        return False

    def _wait_for_loop_idle(self, timeout: float = 60.0) -> bool:
        """
        Ждём пока основной бот не перейдёт в idle (все машины выставлены/заняты).

        Когда loop_idle_event не задан — сразу возвращаем False (нет координации).
        Возвращает True если нужно выйти (стоп взведён).
        Ожидает не дольше timeout секунд; если бот так и не перешёл в idle —
        возвращает False (пропускаем итерацию и пробуем позже).
        """
        if self._loop_idle_event is None:
            return self._should_stop()

        if self._loop_idle_event.is_set():
            return False  # бот уже в idle, можно работать

        # Ждём с ограниченным таймаутом — не вешаем монитор навсегда
        self.log("[Items Monitor] ожидаю idle основного бота (все машины выставлены/заняты)…")
        deadline = time.time() + timeout
        while not self._loop_idle_event.is_set():
            if self._should_stop():
                return True
            if time.time() >= deadline:
                # Таймаут истёк — бот так и не перешёл в idle
                # Возвращаем False чтобы пропустить итерацию и попробовать позже
                return False
            time.sleep(0.5)
        self.log("[Items Monitor] основной бот в idle — запускаю цикл предметов")
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # Main loop
    # ──────────────────────────────────────────────────────────────────────────

    def _run(self) -> None:  # noqa: C901
        import item_actions  # lazy import to avoid circular deps at module level

        while not self._should_stop():
            # Ждём пока бот не запущен
            if self._wait_for_run():
                break

            cfg = self._cfg_provider()

            # Проверяем глобальный флаг включения мониторинга предметов
            if not bool(cfg.get("item_monitor_enabled", True)):
                if self._sleep_coop(5.0):
                    break
                continue

            # Ждём пока основной бот не освободит экран (перейдёт в idle)
            # Таймаут = интервал проверки или 120 сек, чтобы цикл не засыпал если бот долго активен
            idle_timeout = max(float(cfg.get("item_check_interval", 30)) * 4, 120.0)
            if self._wait_for_loop_idle(timeout=idle_timeout):
                break

            # Читаем список предметов из конфига
            items_raw: List[Dict[str, Any]] = cfg.get("tracked_items", [])
            if not items_raw:
                if self._sleep_coop(5.0):
                    break
                continue

            tracked: List[TrackedItem] = [
                TrackedItem(
                    name=it.get("name", ""),
                    our_player=it.get("our_player", ""),
                    target_price=float(it.get("target_price", 0)),
                    min_price=float(it.get("min_price", 0)),
                )
                for it in items_raw
                if it.get("name")
            ]

            interval: float = float(cfg.get("item_check_interval", 30))

            # Сигнализируем LoopManager, что мы занимаем экран (предотвращает конфликт)
            if self._items_busy_event is not None:
                self._items_busy_event.set()
                self.log("[ItemSaleMonitor] items_busy_event установлен — LoopManager заблокирован")
            try:
                results: List[ItemCheckResult] = item_actions.full_monitor_cycle(
                    tracked_items=tracked,
                    cfg=cfg,
                    log=self.log,
                    stop_event=self._own_stop,  # передаём собственный стоп в actions
                )
            except Exception as exc:
                self.log(
                    f"ItemSaleMonitor ошибка цикла: {exc}\n"
                    f"{traceback.format_exc()}"
                )
                results = []
            finally:
                # Освобождаем экран — LoopManager может продолжать sweep
                if self._items_busy_event is not None:
                    self._items_busy_event.clear()
                    self.log("[ItemSaleMonitor] items_busy_event сброшен — LoopManager разблокирован")

            # Обновляем статистику по результатам цикла
            if results:
                self._total_reposts += sum(1 for r in results if r.reposted)
                self._total_errors  += sum(1 for r in results if r.status == STATUS_ERROR)
                if any(r.reposted for r in results):
                    self._last_repost_time = time.time()
                self._last_results = list(results)

            if results and self._on_status_change:
                try:
                    self._on_status_change(results)
                except Exception as exc:
                    self.log(f"on_status_change callback ошибка: {exc}")

            # Уведомляем подписчика статистики
            if self._on_stats_update is not None:
                try:
                    self._on_stats_update(self.get_stats())
                except Exception:
                    pass

            # Пауза между итерациями
            if self._sleep_coop(interval):
                break

        self.log("ItemSaleMonitor остановлен")
