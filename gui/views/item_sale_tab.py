"""
gui/views/item_sale_tab.py — вкладка «Мониторинг продажи предметов».

Интерфейс:
  - Панель управления монитором (Старт/Стоп, индикатор статуса)
  - Панель настроек (сворачиваемая)
  - Панель статистики (живые счётчики)
  - Таблица отслеживаемых предметов (название | игрок | цена | min_price | статус | репостов | ...)
  - История событий
  - Лог

min_price — цена ниже которой бот не будет перевыставлять лот.
  0 = не задана (бот всегда снижает цену).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime

from item_sale_monitor import (
    ItemCheckResult,
    STATUS_TOP, STATUS_OUTBID, STATUS_NOT_FOUND,
    STATUS_NO_LOT, STATUS_ERROR, STATUS_PRICE_FLOOR,
)

# Цветовые метки статусов
STATUS_COLORS: Dict[str, str] = {
    STATUS_TOP:         "#2ecc71",  # зелёный
    STATUS_OUTBID:      "#e74c3c",  # красный
    STATUS_NOT_FOUND:   "#e67e22",  # оранжевый
    STATUS_NO_LOT:      "#e67e22",  # оранжевый
    STATUS_ERROR:       "#95a5a6",  # серый
    STATUS_PRICE_FLOOR: "#f39c12",  # жёлтый
}

# Отображаемые названия статусов
STATUS_LABELS: Dict[str, str] = {
    STATUS_TOP:         "Первый",
    STATUS_OUTBID:      "Перебили",
    STATUS_NOT_FOUND:   "Не найден",
    STATUS_NO_LOT:      "Нет лота",
    STATUS_ERROR:       "Ошибка",
    STATUS_PRICE_FLOOR: "Мин. цена",
}

# Колонки таблицы (расширенные: добавлены reposts и last_status)
COLS = (
    "name", "player", "target_price", "min_price",
    "status", "our_price", "best_price", "rank",
    "reposts", "last_status",
)
COL_HEADERS = {
    "name":         "Предмет",
    "player":       "Игрок",
    "target_price": "Целевая цена",
    "min_price":    "Мин. цена",
    "status":       "Статус",
    "our_price":    "Наша цена",
    "best_price":   "Лучшая цена",
    "rank":         "Место",
    "reposts":      "Репостов",
    "last_status":  "Последн. статус",
}
COL_WIDTHS = {
    "name":         180,
    "player":       120,
    "target_price": 100,
    "min_price":    90,
    "status":       90,
    "our_price":    90,
    "best_price":   90,
    "rank":         60,
    "reposts":      70,
    "last_status":  100,
}

# Максимальное количество записей в истории событий
_HISTORY_MAX = 100


class _Tooltip:
    """Всплывающая подсказка при наведении на виджет."""
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self._text   = text
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show, add=True)
        widget.bind("<Leave>", self._hide, add=True)

    def _show(self, event=None) -> None:
        if self._tip:
            return
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(
            self._tip, text=self._text, justify="left",
            background="#ffffe0", relief="solid", borderwidth=1,
            font=("Segoe UI", 9), wraplength=320,
        )
        lbl.pack(ipadx=4, ipady=2)

    def _hide(self, event=None) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


class ItemSaleTab(ttk.Frame):
    """
    Фрейм-вкладка «Мониторинг предметов».

    Параметры
    ----------
    parent : tk.Widget
        Родительский виджет.
    cfg_provider : Callable[[], dict]
        Функция, возвращающая текущий конфиг (читается при каждом сохранении).
    cfg_saver : Callable[[dict], None]
        Функция для сохранения конфига.
    log : Callable[[str], None]
        Функция логирования.
    """

    def __init__(
        self,
        parent: tk.Widget,
        cfg_provider: Callable[[], Dict[str, Any]],
        cfg_saver: Callable[[Dict[str, Any]], None],
        log: Callable[[str], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._cfg_provider = cfg_provider
        self._cfg_saver    = cfg_saver
        self._log          = log

        # Ссылка на объект монитора (устанавливается через set_monitor)
        self._monitor = None

        # Статистика по каждому предмету: {item_name: {repost_count, last_status, last_price}}
        self._item_stats: Dict[str, Dict] = {}

        # Общая статистика сессии
        self._session_stats: Dict[str, Any] = {
            "reposts":         0,
            "errors":          0,
            "start_time":      None,
            "last_repost_time": None,
        }

        # История событий (макс. _HISTORY_MAX записей)
        self._history: List[str] = []

        # Признак: панель настроек развёрнута
        self._settings_expanded = True

        self._build_ui()
        self._refresh_table()
        # Запустить опрос статуса монитора
        self.after(2000, self._poll_monitor_status)

    # ───────────────────────────────────────────────────────────────────────────
    # Публичный API
    # ───────────────────────────────────────────────────────────────────────────

    def set_monitor(self, monitor) -> None:
        """Привязать объект ItemSaleMonitor для управления Start/Stop."""
        self._monitor = monitor
        self._update_monitor_buttons()

    def update_stats(self, stats: dict) -> None:
        """
        Обновить панель статистики.
        stats: total_reposts, total_errors, start_time (float|None), last_repost_time (float|None)
        """
        if "total_reposts" in stats:
            self._session_stats["reposts"] = stats["total_reposts"]
        if "total_errors" in stats:
            self._session_stats["errors"] = stats["total_errors"]
        if "start_time" in stats:
            self._session_stats["start_time"] = stats["start_time"]
        if "last_repost_time" in stats:
            self._session_stats["last_repost_time"] = stats["last_repost_time"]
        self._refresh_stats_panel()

    def update_results(self, results: List[ItemCheckResult]) -> None:
        """
        Вызывается из on_status_change callback с результатами проверки.
        Обновляет строки таблицы, цветовое выделение, историю событий и статистику.
        """
        # Индекс по имени предмета
        result_map = {r.item_name: r for r in results}

        for iid in self._tree.get_children():
            vals = list(self._tree.item(iid, "values"))
            item_name = vals[0]
            if item_name not in result_map:
                continue
            r = result_map[item_name]

            # Обновить статистику предмета
            prev = self._item_stats.setdefault(item_name, {
                "repost_count": 0,
                "last_status":  "",
                "last_price":   None,
            })
            status_changed = (prev["last_status"] != r.status)

            if r.reposted:
                prev["repost_count"] += 1
                self._session_stats["reposts"] += 1
                self._session_stats["last_repost_time"] = datetime.now().timestamp()

            if r.status == STATUS_ERROR:
                self._session_stats["errors"] += 1

            prev["last_status"] = r.status
            prev["last_price"]  = r.our_price

            # Обновить строку таблицы
            # Индексы: 0=name,1=player,2=target,3=min,4=status,5=our,6=best,7=rank,8=reposts,9=last_status
            vals[4] = STATUS_LABELS.get(r.status, r.status)
            vals[5] = f"{r.our_price:.0f}" if r.our_price else ""
            vals[6] = f"{r.best_price:.0f}" if r.best_price else ""
            vals[7] = str(r.rank) if r.rank else ""
            vals[8] = str(prev["repost_count"])
            vals[9] = STATUS_LABELS.get(r.status, r.status)
            self._tree.item(iid, values=vals)

            # Цветовое выделение строки
            color = STATUS_COLORS.get(r.status, "")
            tag   = f"color_{r.status}"
            self._tree.tag_configure(tag, foreground=color)
            self._tree.item(iid, tags=(tag,))

            # Добавить запись в историю событий при изменении статуса или репосте
            if status_changed or r.reposted:
                self._add_history_entry(r)

        # Обновить строку общего статуса
        tops     = sum(1 for r in results if r.status == STATUS_TOP)
        outbids  = sum(1 for r in results if r.status == STATUS_OUTBID)
        reposted = sum(1 for r in results if r.reposted)
        self._status_var.set(
            f"Проверок: {len(results)} | Первых: {tops} | Перебили: {outbids} | Репостов: {reposted}"
        )

        # Обновить карточки статистики
        self._refresh_stats_panel(tops=tops, outbids=outbids)

    def append_log(self, msg: str) -> None:
        """Добавить строку в лог-панель."""
        self._log_text.configure(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    # ───────────────────────────────────────────────────────────────────────────
    # UI builder
    # ───────────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        # Строки с переменным весом будут назначены ниже
        self.rowconfigure(5, weight=1)   # таблица предметов
        self.rowconfigure(8, weight=0)   # история событий
        self.rowconfigure(10, weight=1)  # лог

        row = 0

        # ─── 1. Панель управления монитором ───
        self._build_monitor_panel(row)
        row += 1

        # ─── 2. Панель настроек (сворачиваемая) ───
        self._build_settings_panel(row)
        row += 1

        # ─── 3. Панель статистики ───
        self._build_stats_panel(row)
        row += 1

        # ─── 4. Панель управления предметами ───
        ctrl_frame = ttk.LabelFrame(self, text="Предметы")
        ctrl_frame.grid(row=row, column=0, sticky="ew", padx=6, pady=(4, 2))
        row += 1

        btn_add = ttk.Button(ctrl_frame, text="+  Добавить", command=self._on_add)
        btn_add.pack(side=tk.LEFT, padx=4, pady=4)
        _Tooltip(btn_add, "Добавить новый предмет для отслеживания в маркетплейсе")

        self._btn_del = ttk.Button(ctrl_frame, text="−  Удалить", command=self._on_remove, state="disabled")
        self._btn_del.pack(side=tk.LEFT, padx=4, pady=4)
        _Tooltip(self._btn_del, "Удалить выбранный предмет из списка отслеживания")

        self._btn_edit = ttk.Button(ctrl_frame, text="✏  Редактировать", command=self._on_edit, state="disabled")
        self._btn_edit.pack(side=tk.LEFT, padx=4, pady=4)
        _Tooltip(self._btn_edit, "Изменить настройки выбранного предмета (цену, имя игрока)")

        btn_refresh = ttk.Button(ctrl_frame, text="↻  Обновить", command=self._refresh_table)
        btn_refresh.pack(side=tk.LEFT, padx=4, pady=4)
        _Tooltip(btn_refresh, "Перечитать конфиг и обновить таблицу")

        # ─── 5. Таблица предметов ───
        tbl_frame = ttk.Frame(self)
        tbl_frame.grid(row=row, column=0, sticky="nsew", padx=6, pady=2)
        tbl_frame.columnconfigure(0, weight=1)
        tbl_frame.rowconfigure(0, weight=1)
        self.rowconfigure(row, weight=3)
        row += 1

        self._tree = ttk.Treeview(
            tbl_frame,
            columns=COLS,
            show="headings",
            selectmode="browse",
        )
        for col in COLS:
            self._tree.heading(col, text=COL_HEADERS[col])
            self._tree.column(col, width=COL_WIDTHS[col], anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._tree.bind("<Double-1>", lambda e: self._on_edit())
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ─── 6. Легенда статусов ───
        legend = (
            "Статусы:  🟢 Первый  🔴 Перебили  🟡 Мин.цена  🟠 Нет лота/Не найден  ⚪ Ошибка  "
            "│  Мин. цена: бот не опускает цену ниже этого значения (0 = без ограничения)  "
            "│  Целевая цена: желаемая цена при выставлении лота"
        )
        ttk.Label(self, text=legend, anchor="w", font=("Segoe UI", 8),
                  foreground="#666666").grid(row=row, column=0, sticky="ew", padx=6, pady=(2, 0))
        row += 1

        # ─── 7. Строка статуса ───
        self._status_var = tk.StringVar(value="Ожидание...")
        ttk.Label(self, textvariable=self._status_var, anchor="w").grid(
            row=row, column=0, sticky="ew", padx=6, pady=2
        )
        row += 1

        # ─── 8. История событий ───
        self._build_history_panel(row)
        self.rowconfigure(row, weight=2)
        row += 1

        # ─── 9. Лог-вывод ───
        log_frame = ttk.LabelFrame(self, text="Лог")
        log_frame.grid(row=row, column=0, sticky="nsew", padx=6, pady=(2, 6))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.rowconfigure(row, weight=1)

        self._log_text = tk.Text(
            log_frame, height=6, state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9),
        )
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_vsb.set)
        self._log_text.grid(row=0, column=0, sticky="nsew")
        log_vsb.grid(row=0, column=1, sticky="ns")

    # ───────────────────────────────────────────────────────────────────────────
    # Панель управления монитором
    # ───────────────────────────────────────────────────────────────────────────

    def _build_monitor_panel(self, row: int) -> None:
        """Построить панель «Управление монитором»."""
        frame = ttk.LabelFrame(self, text="Управление монитором")
        frame.grid(row=row, column=0, sticky="ew", padx=6, pady=(6, 2))

        # Чекбокс «Мониторинг включён»
        cfg = self._cfg_provider()
        monitor_enabled_val = cfg.get("item_monitor_enabled", False)
        self._monitor_enabled_var = tk.BooleanVar(value=bool(monitor_enabled_val))
        chk = ttk.Checkbutton(
            frame,
            text="Мониторинг включён",
            variable=self._monitor_enabled_var,
            command=self._on_monitor_enabled_toggle,
        )
        chk.pack(side=tk.LEFT, padx=6, pady=4)
        _Tooltip(chk, "Включить/выключить автоматическую проверку и перевыставление предметов")

        # Кнопка «Старт»
        self._btn_start = ttk.Button(
            frame, text="▶ Старт", command=self._on_monitor_start, state="disabled"
        )
        self._btn_start.pack(side=tk.LEFT, padx=4, pady=4)
        _Tooltip(self._btn_start, "Запустить монитор предметов")

        # Кнопка «Стоп»
        self._btn_stop = ttk.Button(
            frame, text="⏹ Стоп", command=self._on_monitor_stop, state="disabled"
        )
        self._btn_stop.pack(side=tk.LEFT, padx=4, pady=4)
        _Tooltip(self._btn_stop, "Остановить монитор предметов")

        # Индикатор статуса
        self._monitor_status_var = tk.StringVar(value="● Остановлен")
        self._monitor_status_lbl = tk.Label(
            frame,
            textvariable=self._monitor_status_var,
            font=("Segoe UI", 10, "bold"),
            foreground="#e74c3c",  # по умолчанию красный (остановлен)
            # bg не указываем — ttk.LabelFrame не поддерживает .cget("background")
        )
        self._monitor_status_lbl.pack(side=tk.LEFT, padx=10, pady=4)

    def _on_monitor_enabled_toggle(self) -> None:
        """Сохранить состояние чекбокса «Мониторинг включён» в конфиг."""
        cfg = self._cfg_provider()
        cfg["item_monitor_enabled"] = self._monitor_enabled_var.get()
        self._cfg_saver(cfg)
        self._log(f"[GUI] Мониторинг {'включён' if self._monitor_enabled_var.get() else 'выключен'}")

    def _on_monitor_start(self) -> None:
        """Запустить монитор."""
        if self._monitor is not None:
            self._monitor.start()
            self._log("[GUI] Монитор запущен")

    def _on_monitor_stop(self) -> None:
        """Остановить монитор."""
        if self._monitor is not None:
            self._monitor.stop()
            self._log("[GUI] Монитор остановлен")

    def _update_monitor_buttons(self) -> None:
        """Обновить состояние кнопок Старт/Стоп в зависимости от наличия монитора."""
        state = "normal" if self._monitor is not None else "disabled"
        self._btn_start.configure(state=state)
        self._btn_stop.configure(state=state)

    def _poll_monitor_status(self) -> None:
        """Опрашивать статус монитора каждые 2 секунды и обновлять индикатор."""
        try:
            if self._monitor is not None and hasattr(self._monitor, "is_running"):
                running = self._monitor.is_running()
            elif self._monitor is not None and hasattr(self._monitor, "running"):
                running = bool(self._monitor.running)
            else:
                running = False

            if running:
                self._monitor_status_var.set("● Работает")
                self._monitor_status_lbl.configure(foreground="#2ecc71")
            else:
                self._monitor_status_var.set("● Остановлен")
                self._monitor_status_lbl.configure(foreground="#e74c3c")
        except Exception:
            # Если монитор недоступен — показать «Остановлен»
            self._monitor_status_var.set("● Остановлен")
            self._monitor_status_lbl.configure(foreground="#e74c3c")

        # Повторить через 2 секунды
        self.after(2000, self._poll_monitor_status)

    # ───────────────────────────────────────────────────────────────────────────
    # Панель настроек (сворачиваемая)
    # ───────────────────────────────────────────────────────────────────────────

    def _build_settings_panel(self, row: int) -> None:
        """Построить сворачиваемую панель «Настройки»."""
        # Внешний контейнер
        outer = ttk.Frame(self)
        outer.grid(row=row, column=0, sticky="ew", padx=6, pady=(2, 2))
        outer.columnconfigure(0, weight=1)

        # Кнопка переключения (▼/▶ Настройки)
        self._settings_toggle_btn = ttk.Button(
            outer,
            text="▼ Настройки",
            command=self._toggle_settings,
            style="Toolbutton",
        )
        self._settings_toggle_btn.grid(row=0, column=0, sticky="w", pady=(0, 2))

        # LabelFrame с содержимым
        self._settings_frame = ttk.LabelFrame(outer, text="Настройки")
        self._settings_frame.grid(row=1, column=0, sticky="ew")
        self._settings_frame.columnconfigure(1, weight=1)
        self._settings_frame.columnconfigure(3, weight=1)

        cfg = self._cfg_provider()

        # Список полей: (метка, ключ_конфига, значение_по_умолчанию, подсказка, тип_виджета)
        # тип_виджета: "entry" или "combo"
        fields = [
            (
                "Шаг цены",
                "price_step",
                str(cfg.get("price_step", "1")),
                "На сколько снизить цену при перебитии конкурентом (например 1 = на 1$)",
                "entry",
                None,
            ),
            (
                "Кол-во в лоте",
                "lot_qty",
                str(cfg.get("lot_qty", "1")),
                "Количество предметов в одном лоте",
                "entry",
                None,
            ),
            (
                "Интервал проверки (сек)",
                "item_check_interval",
                str(cfg.get("item_check_interval", "30")),
                "Как часто проверять статус предметов (секунды)",
                "entry",
                None,
            ),
            (
                "Оплата",
                "item_payment_method",
                str(cfg.get("item_payment_method", "наличными")),
                "Способ оплаты при выставлении лота",
                "combo",
                ["наличными", "картой"],
            ),
        ]

        self._settings_vars: Dict[str, tk.StringVar] = {}

        # Размещаем поля в 2 колонки (по 2 поля на строку)
        for idx, (label, key, default, tooltip, wtype, options) in enumerate(fields):
            col_label = (idx % 2) * 2      # 0 или 2
            col_widget = col_label + 1     # 1 или 3
            f_row = idx // 2

            ttk.Label(self._settings_frame, text=label + ":", anchor="e").grid(
                row=f_row, column=col_label, sticky="e", padx=(8, 2), pady=4
            )
            var = tk.StringVar(value=default)
            self._settings_vars[key] = var

            if wtype == "combo":
                widget = ttk.Combobox(
                    self._settings_frame,
                    textvariable=var,
                    values=options,
                    state="readonly",
                    width=14,
                )
                widget.bind("<<ComboboxSelected>>", lambda e, k=key: self._save_setting(k))
            else:
                widget = ttk.Entry(self._settings_frame, textvariable=var, width=16)
                widget.bind("<FocusOut>", lambda e, k=key: self._save_setting(k))
                widget.bind("<Return>",   lambda e, k=key: self._save_setting(k))

            widget.grid(row=f_row, column=col_widget, sticky="ew", padx=(0, 12), pady=4)
            _Tooltip(widget, tooltip)

        # Панель начинает развёрнутой
        self._settings_expanded = True

    def _toggle_settings(self) -> None:
        """Свернуть/развернуть панель настроек."""
        if self._settings_expanded:
            self._settings_frame.grid_remove()
            self._settings_toggle_btn.configure(text="▶ Настройки")
            self._settings_expanded = False
        else:
            self._settings_frame.grid()
            self._settings_toggle_btn.configure(text="▼ Настройки")
            self._settings_expanded = True

    def _save_setting(self, key: str) -> None:
        """Сохранить одно поле настроек в конфиг."""
        cfg = self._cfg_provider()
        val = self._settings_vars[key].get().strip()
        cfg[key] = val
        self._cfg_saver(cfg)
        self._log(f"[GUI] Настройка сохранена: {key} = {val!r}")

    # ───────────────────────────────────────────────────────────────────────────
    # Панель статистики
    # ───────────────────────────────────────────────────────────────────────────

    def _build_stats_panel(self, row: int) -> None:
        """Построить панель «Статистика»."""
        frame = ttk.LabelFrame(self, text="Статистика")
        frame.grid(row=row, column=0, sticky="ew", padx=6, pady=(2, 2))

        # Карточки статистики: (атрибут, заголовок, цвет_акцента)
        cards = [
            ("_stat_reposts",    "Репостов",        "#2ecc71"),
            ("_stat_errors",     "Ошибок",           "#e74c3c"),
            ("_stat_uptime",     "Uptime",           "#95a5a6"),
            ("_stat_last_repost","Последний репост", "#95a5a6"),
            ("_stat_top",        "На первом месте",  "#2ecc71"),
            ("_stat_outbid",     "Перебили",         "#e74c3c"),
        ]

        self._stat_vars: Dict[str, tk.StringVar] = {}

        for col_idx, (attr, title, color) in enumerate(cards):
            card = ttk.Frame(frame, relief="groove", borderwidth=1)
            card.grid(row=0, column=col_idx, padx=4, pady=6, sticky="ns")
            card.columnconfigure(0, weight=1)

            ttk.Label(card, text=title, font=("Segoe UI", 8), foreground="#888888").pack(
                padx=8, pady=(4, 0)
            )
            var = tk.StringVar(value="0" if attr not in ("_stat_uptime", "_stat_last_repost") else "—")
            self._stat_vars[attr] = var
            tk.Label(
                card,
                textvariable=var,
                font=("Segoe UI", 14, "bold"),
                foreground=color,
            ).pack(padx=8, pady=(0, 4))

    def _refresh_stats_panel(self, tops: int = 0, outbids: int = 0) -> None:
        """Обновить значения карточек статистики."""
        self._stat_vars["_stat_reposts"].set(str(self._session_stats["reposts"]))
        self._stat_vars["_stat_errors"].set(str(self._session_stats["errors"]))

        # Uptime
        start = self._session_stats["start_time"]
        if start is not None:
            try:
                elapsed = datetime.now().timestamp() - float(start)
                h = int(elapsed // 3600)
                m = int((elapsed % 3600) // 60)
                self._stat_vars["_stat_uptime"].set(f"{h:02d}:{m:02d}")
            except Exception:
                self._stat_vars["_stat_uptime"].set("—")
        else:
            self._stat_vars["_stat_uptime"].set("—")

        # Последний репост
        last = self._session_stats["last_repost_time"]
        if last is not None:
            try:
                self._stat_vars["_stat_last_repost"].set(
                    datetime.fromtimestamp(float(last)).strftime("%H:%M:%S")
                )
            except Exception:
                self._stat_vars["_stat_last_repost"].set("—")
        else:
            self._stat_vars["_stat_last_repost"].set("—")

        self._stat_vars["_stat_top"].set(str(tops))
        self._stat_vars["_stat_outbid"].set(str(outbids))

    # ───────────────────────────────────────────────────────────────────────────
    # Панель истории событий
    # ───────────────────────────────────────────────────────────────────────────

    def _build_history_panel(self, row: int) -> None:
        """Построить панель «История событий»."""
        frame = ttk.LabelFrame(self, text="История событий")
        frame.grid(row=row, column=0, sticky="nsew", padx=6, pady=(2, 2))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        # Кнопка очистки
        btn_clear = ttk.Button(frame, text="🗑 Очистить", command=self._clear_history)
        btn_clear.grid(row=0, column=1, sticky="ne", padx=4, pady=4)
        _Tooltip(btn_clear, "Очистить историю событий")

        # Listbox с прокруткой
        listbox_frame = ttk.Frame(frame)
        listbox_frame.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        listbox_frame.columnconfigure(0, weight=1)
        listbox_frame.rowconfigure(0, weight=1)

        self._history_lb = tk.Listbox(
            listbox_frame,
            height=8,
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            selectbackground="#3a3a3a",
            activestyle="none",
        )
        hist_vsb = ttk.Scrollbar(listbox_frame, orient="vertical", command=self._history_lb.yview)
        hist_hsb = ttk.Scrollbar(listbox_frame, orient="horizontal", command=self._history_lb.xview)
        self._history_lb.configure(
            yscrollcommand=hist_vsb.set,
            xscrollcommand=hist_hsb.set,
        )
        self._history_lb.grid(row=0, column=0, sticky="nsew")
        hist_vsb.grid(row=0, column=1, sticky="ns")
        hist_hsb.grid(row=1, column=0, sticky="ew")

    def _add_history_entry(self, r: "ItemCheckResult") -> None:
        """Добавить запись в историю событий."""
        ts      = datetime.now().strftime("%H:%M:%S")
        status  = STATUS_LABELS.get(r.status, r.status)
        price   = f"{r.our_price:.0f}" if r.our_price else "—"
        repost  = "  (репост)" if r.reposted else ""
        entry   = f"{ts}  {r.item_name} → {status}  цена: {price}{repost}"

        self._history.append(entry)
        # Ограничить до _HISTORY_MAX
        if len(self._history) > _HISTORY_MAX:
            self._history = self._history[-_HISTORY_MAX:]

        # Обновить listbox
        self._history_lb.insert(tk.END, entry)
        # Удалить лишние строки из начала
        while self._history_lb.size() > _HISTORY_MAX:
            self._history_lb.delete(0)
        # Прокрутить в конец
        self._history_lb.yview_moveto(1.0)

    def _clear_history(self) -> None:
        """Очистить историю событий."""
        self._history.clear()
        self._history_lb.delete(0, tk.END)
        self._log("[GUI] История событий очищена")

    # ───────────────────────────────────────────────────────────────────────────
    # Выделение строки в таблице
    # ───────────────────────────────────────────────────────────────────────────

    def _on_tree_select(self, event=None) -> None:
        """Активировать/деактивировать кнопки при изменении выделения строки."""
        has_sel = bool(self._tree.selection())
        state = "normal" if has_sel else "disabled"
        self._btn_del.configure(state=state)
        self._btn_edit.configure(state=state)

    # ───────────────────────────────────────────────────────────────────────────
    # Обновление таблицы
    # ───────────────────────────────────────────────────────────────────────────

    def _refresh_table(self) -> None:
        """Перечитывает конфиг и обновляет список предметов."""
        cfg   = self._cfg_provider()
        items = cfg.get("tracked_items", [])

        self._tree.delete(*self._tree.get_children())
        for it in items:
            name = it.get("name", "")
            item_stat = self._item_stats.get(name, {})
            self._tree.insert("", "end", values=(
                name,
                it.get("our_player",   ""),
                it.get("target_price", ""),
                it.get("min_price",    "0"),
                "—",                                     # status
                "",                                      # our_price
                "",                                      # best_price
                "",                                      # rank
                str(item_stat.get("repost_count", 0)),   # reposts
                STATUS_LABELS.get(item_stat.get("last_status", ""), "—"),  # last_status
            ))

    # ───────────────────────────────────────────────────────────────────────────
    # Кнопки управления предметами
    # ───────────────────────────────────────────────────────────────────────────

    def _on_add(self) -> None:
        """Открыть диалог добавления нового предмета."""
        dlg = _ItemDialog(self, title="Добавить предмет")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        cfg = self._cfg_provider()
        items: List[Dict] = cfg.setdefault("tracked_items", [])
        items.append(dlg.result)
        self._cfg_saver(cfg)
        self._refresh_table()
        self._log(f"[GUI] Добавлен {dlg.result['name']}")

    def _on_remove(self) -> None:
        """Удалить выбранный предмет."""
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("Выбор", "Выберите предмет для удаления")
            return
        vals = self._tree.item(sel[0], "values")
        name = vals[0]
        if not messagebox.askyesno("Удаление", f"Удалить '{name}'?"):
            return
        cfg = self._cfg_provider()
        items: List[Dict] = cfg.get("tracked_items", [])
        cfg["tracked_items"] = [it for it in items if it.get("name") != name]
        self._cfg_saver(cfg)
        self._item_stats.pop(name, None)
        self._refresh_table()
        self._log(f"[GUI] Удалён {name}")

    def _on_edit(self) -> None:
        """Открыть диалог редактирования выбранного предмета."""
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("Выбор", "Выберите предмет для редактирования")
            return
        vals = self._tree.item(sel[0], "values")
        current = {
            "name":         vals[0],
            "our_player":   vals[1],
            "target_price": vals[2],
            "min_price":    vals[3],
        }
        dlg = _ItemDialog(self, title="Редактировать предмет", initial=current)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        cfg = self._cfg_provider()
        items: List[Dict] = cfg.get("tracked_items", [])
        for i, it in enumerate(items):
            if it.get("name") == current["name"]:
                items[i] = dlg.result
                break
        self._cfg_saver(cfg)
        self._refresh_table()
        self._log(f"[GUI] Обновлён {dlg.result['name']}")


# ───────────────────────────────────────────────────────────────────────────
class _ItemDialog(tk.Toplevel):
    """
    Модальный диалог добавления/редактирования предмета.

    Поля:
      - Название предмета
      - Имя игрока (нашего)
      - Целевая цена
      - Минимальная цена (0 = не ограничено)
    """

    FIELD_TIPS = {
        "name":         "Точное название предмета как оно отображается в поиске маркетплейса",
        "our_player":   "Имя игрока-продавца (нашего). Бот ищет именно этот никнейм в таблице продавцов",
        "target_price": "Желаемая цена за 1 шт. Бот выставляет лот по этой цене, затем снижает если перебили",
        "min_price":    "Минимальная цена за 1 шт.\n0 = без ограничения (бот снижает до упора)\nЕсли цену надо снизить ниже этого значения — бот НЕ делает репост",
    }

    def __init__(
        self,
        parent: tk.Widget,
        title: str = "Предмет",
        initial: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.grab_set()
        self.result: Optional[Dict[str, Any]] = None

        init = initial or {}

        # ── Поля ──
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        labels = [
            ("Название предмета:",  "name"),
            ("Имя нашего игрока:", "our_player"),
            ("Целевая цена:",       "target_price"),
            ("Мин. цена (0=нет):",  "min_price"),
        ]
        self._vars: Dict[str, tk.StringVar] = {}
        self._first_entry: Optional[ttk.Entry] = None
        for row, (lbl, key) in enumerate(labels):
            ttk.Label(frame, text=lbl, anchor="w").grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 8)
            )
            var = tk.StringVar(value=str(init.get(key, "" if key != "min_price" else "0")))
            self._vars[key] = var
            ent = ttk.Entry(frame, textvariable=var, width=28)
            ent.grid(row=row, column=1, sticky="ew", pady=3)
            if row == 0:
                self._first_entry = ent
            tip_text = self.FIELD_TIPS.get(key, "")
            if tip_text:
                _Tooltip(ent, tip_text)
        frame.columnconfigure(1, weight=1)

        # ── Кнопки ──
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="ОК",     command=self._ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side=tk.RIGHT)

        self.after(50, self._first_entry.focus_set)

    def _ok(self) -> None:
        name   = self._vars["name"].get().strip()
        player = self._vars["our_player"].get().strip()
        if not name:
            messagebox.showerror("Ошибка", "Название предмета не может быть пустым", parent=self)
            return
        if not player:
            messagebox.showerror("Ошибка", "Имя игрока не может быть пустым", parent=self)
            return
        try:
            target_price = float(self._vars["target_price"].get().strip() or "0")
        except ValueError:
            messagebox.showerror("Ошибка", "Целевая цена должна быть числом", parent=self)
            return
        try:
            min_price = float(self._vars["min_price"].get().strip() or "0")
        except ValueError:
            messagebox.showerror("Ошибка", "Мин. цена должна быть числом (или 0)", parent=self)
            return
        self.result = {
            "name":         name,
            "our_player":   player,
            "target_price": target_price,
            "min_price":    min_price,
        }
        self.destroy()
