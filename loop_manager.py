from __future__ import annotations

import random
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass
class PostResult:
    status: str  # "OK" | "TEMPLATE_NOT_FOUND" | "DUPLICATE_HASH" | "INVALID_ITEM" | "FORM_TIMEOUT" | "DIALOG_FAIL" | "STOPPED" | "ERROR" | "BLOCKED"
    price_raw: str = ""
    price_value: float = 0.0
    photo_hash_to_commit: Optional[str] = None
    reason: str = ""


class LoopManager(threading.Thread):
    """
    Endless loop manager:
    - In loop_mode: does NOT use processed.json as a blocker.
      It keeps re-checking NOT_FOUND templates forever.
    - In oneshot_mode: uses processed set to skip already posted templates.
    - Reads config live via cfg_provider() every step (no restart needed).
    """

    def __init__(
        self,
        *,
        cfg_provider: Callable[[], Dict[str, Any]],
        run_event: threading.Event,
        stop_event: threading.Event,
        log: Callable[[str], None],
        list_templates: Callable[[Any], list],
        is_valid_item: Callable[[Any, str], bool],
        enter_create_rent: Callable[[Dict[str, Any], threading.Event, threading.Event], bool],
        post_one_item: Callable[[Dict[str, Any], Any, threading.Event, threading.Event], PostResult],
        load_processed: Callable[[], set],
        save_processed: Callable[[set], None],
        load_photo_hashes: Callable[[], Dict[str, str]],
        save_photo_hashes: Callable[[Dict[str, str]], None],
        record_post: Callable[[str, float], None],
        append_archive_row: Callable[[str, str, float], None],
        pre_sweep_hook: Optional[Callable[[Dict[str, Any], list, threading.Event, threading.Event], None]] = None,
        watchdog_tick: Optional[Callable[[Dict[str, Any], threading.Event, threading.Event], None]] = None,
        visible_templates_provider: Optional[Callable[[Dict[str, Any], list], Optional[list]]] = None,
        loop_idle_event: Optional[threading.Event] = None,
        items_busy_event: Optional[threading.Event] = None,
    ):
        super().__init__(daemon=True)
        self.cfg_provider = cfg_provider
        self.run_event = run_event
        self.stop_event = stop_event

        self.log = log
        self.list_templates = list_templates
        self.is_valid_item = is_valid_item
        self.enter_create_rent = enter_create_rent
        self.post_one_item = post_one_item

        self.load_processed = load_processed
        self.save_processed = save_processed
        self.load_photo_hashes = load_photo_hashes
        self.save_photo_hashes = save_photo_hashes

        self.record_post = record_post
        self.append_archive_row = append_archive_row

        self.pre_sweep_hook = pre_sweep_hook
        self.watchdog_tick = watchdog_tick
        self.visible_templates_provider = visible_templates_provider
        # Event взводится когда бот в режиме ожидания (все машины выставлены/заняты)
        # Сбрасывается как только начинается реальная работа
        self.loop_idle_event: Optional[threading.Event] = loop_idle_event
        # Event взводится когда Items Monitor активно работает с экраном
        # LoopManager ждёт его сброса перед началом нового свипа
        self.items_busy_event: Optional[threading.Event] = items_busy_event

        # Live state
        self.processed = set()
        self.photo_hashes: Dict[str, str] = {}
        self.next_check_at: Dict[str, float] = {}  # stem -> unix time

        self._idle_last_reason = ""
        self._idle_last_log_at = 0.0
        self._visible_fallback_last = 0.0

    # ── Public helpers ─────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """True если поток ещё жив."""
        return self.is_alive()

    def stop_and_join(self, timeout: float = 5.0) -> None:
        """Аккуратно остановить и дождаться завершения потока."""
        self.stop_event.set()
        try:
            self.run_event.clear()
        except Exception:
            pass
        self.join(timeout=timeout)

    def status(self) -> dict:
        """Краткий статус состояния менеджера цикла."""
        return {
            "alive": self.is_alive(),
            "stopped": self.stop_event.is_set(),
            "paused": not self.run_event.is_set(),
        }

    def _wait_if_paused(self):
        while not self.stop_event.is_set() and not self.run_event.is_set():
            time.sleep(0.10)

    def _sleep_coop(self, seconds: float):
        # Cooperative sleep with pause/stop (+ watchdog tick)
        end = time.time() + max(0.0, float(seconds))
        last_wd = 0.0
        while time.time() < end and not self.stop_event.is_set():
            if not self.run_event.is_set():
                self._wait_if_paused()
                if self.stop_event.is_set():
                    return

            # watchdog tick at most ~3x/sec
            if self.watchdog_tick is not None:
                now = time.time()
                if (now - last_wd) >= 0.33:
                    last_wd = now
                    try:
                        cfg = self.cfg_provider()
                        self.watchdog_tick(cfg, self.run_event, self.stop_event)
                    except Exception:
                        pass

            time.sleep(0.05)

    def _wait_items_busy(self):
        """Ждёт пока ItemSaleMonitor не завершит цикл (items_busy_event сброшен).
        Вызывается ПЕРЕД loop_idle_event.clear() — чтобы ItemMonitor успел запуститься.
        Максимум 120 секунд, потом продолжаем без ожидания."""
        if self.items_busy_event is None or not self.items_busy_event.is_set():
            return
        self.log("[LoopManager] Items Monitor занят — жду завершения цикла предметов…")
        wait_start = time.time()
        while self.items_busy_event.is_set():
            if self.stop_event.is_set():
                break
            if time.time() - wait_start > 120.0:
                self.log("[LoopManager] Items Monitor занят >120с — продолжаю без ожидания")
                break
            time.sleep(0.5)

    def _idle_log(self, reason: str, cooldown: float):
        now = time.time()
        if reason != self._idle_last_reason or (now - self._idle_last_log_at) > cooldown:
            self.log(reason)
            self._idle_last_reason = reason
            self._idle_last_log_at = now

    def _should_skip_due_next_check(self, stem: str) -> bool:
        t = self.next_check_at.get(stem, 0.0)
        return time.time() < t

    def _schedule_next_check(self, stem: str, delay: float):
        self.next_check_at[stem] = time.time() + max(0.25, float(delay))

    def reload_state(self):
        # called once at thread start
        self.processed = self.load_processed()
        self.photo_hashes = self.load_photo_hashes()

    def run(self):
        self.reload_state()
        self.log("LoopManager started (patch v8_7_4)")

        idle_cooldown = 7.0

        while not self.stop_event.is_set():
            try:
                if not self.run_event.is_set():
                    self._wait_if_paused()
                    continue

                cfg = self.cfg_provider()

                loop_mode = bool(cfg.get("loop_mode", True))
                retry_not_found = float(cfg.get("not_found_retry_delay", 8.0))
                retry_error = float(cfg.get("error_retry_delay", 3.0))

                post_interval_min = float(cfg.get("post_interval_min", 0.8))
                post_interval_max = float(cfg.get("post_interval_max", 1.6))
                if post_interval_max < post_interval_min:
                    post_interval_min, post_interval_max = post_interval_max, post_interval_min

                cycle_sleep = float(cfg.get("cycle_sleep", 0.8))  # small nap between sweeps

                # Dedupe policy
                # "off" => never block reposts by photo hash (recommended for loop_mode)
                # "on_success" => commit hash only on successful post and block future same hash
                # "always" => commit & block even on attempt (not recommended)
                dedupe_policy = str(cfg.get("dedupe_policy", "off")).strip().lower()
                if loop_mode and dedupe_policy == "on_success":
                    # In loop mode, on_success dedupe will still block repost forever.
                    # So enforce off unless user explicitly sets otherwise.
                    if bool(cfg.get("dedupe_force_in_loop", False)) is False:
                        dedupe_policy = "off"

                # Build template list
                templates_all = self.list_templates(cfg)
                if not templates_all:
                    self._idle_log("IDLE: no templates found in car_dir", idle_cooldown)
                    if self.loop_idle_event is not None:
                        self.loop_idle_event.set()
                    self._sleep_coop(1.0)
                    continue

                # keep only valid items
                templates = [p for p in templates_all if self.is_valid_item(cfg, p.stem)]
                if not templates:
                    self._idle_log(
                        "IDLE: no VALID templates. Need: root has *.png and each has folder with description+price+1/2/3 photos",
                        idle_cooldown,
                    )
                    self._sleep_coop(1.2)
                    continue

                # One-shot pending logic
                if not loop_mode:
                    pending = [p for p in templates if p.name not in self.processed]
                    if not pending:
                        self._idle_log(
                            f"IDLE: nothing pending. valid={len(templates)} already in processed.json. Toggle loop_mode for infinite repost.",
                            idle_cooldown,
                        )
                        self._sleep_coop(1.5)
                        continue
                    work_list = pending
                else:
                    work_list = templates

                # Pre-sweep hook (fast scan cache, etc.)
                # Enter create rent once per sweep — but NOT with force
                # (cooldown prevents spamming Create button every 4 seconds)
                self.enter_create_rent(cfg, self.run_event, self.stop_event)

                if self.pre_sweep_hook is not None and bool(cfg.get("fast_scan_pre_sweep_enabled", True)):
                    try:
                        self.pre_sweep_hook(cfg, work_list, self.run_event, self.stop_event)
                    except Exception:
                        pass

                # Optionally restrict to visible templates only (FASTSCAN hit list).
                if bool(cfg.get("fast_scan_visible_only", True)) and self.visible_templates_provider is not None:
                    try:
                        visible = self.visible_templates_provider(cfg, list(work_list))
                    except Exception:
                        visible = None
                    if visible is not None:
                        if not visible:
                            now = time.time()
                            fallback_every = float(cfg.get("fast_scan_visible_fallback_every_s", 12.0))
                            if (now - self._visible_fallback_last) < max(1.0, fallback_every):
                                idle_sleep = float(cfg.get("fast_scan_visible_idle_sleep", cycle_sleep))
                                self._idle_log("FASTSCAN: no visible vehicles -> idle", idle_cooldown)
                                # Нет машин на экране — бот в режиме ожидания
                                if self.loop_idle_event is not None:
                                    self.loop_idle_event.set()
                                self._sleep_coop(idle_sleep)
                                continue
                            self._visible_fallback_last = now
                            # fullback-свип: машин не видно дольше fallback_every секунд
                            # Устанавливаем idle — бот в режиме ожидания, экран свободен
                            if self.loop_idle_event is not None:
                                self.loop_idle_event.set()
                            self.log("FASTSCAN: empty visible list -> running full sweep fallback")
                            # Ждём завершения цикла ItemMonitor перед тем как сбросим idle
                            self._wait_items_busy()
                        else:
                            work_list = visible

                created_any = False

                # rotate sweep start (prevents always starting from same item)
                start_idx = int(cfg.get("rotate_index", 0)) % max(1, len(work_list))
                rotated = work_list[start_idx:] + work_list[:start_idx]
                cfg["rotate_index"] = (start_idx + 1) % max(1, len(work_list))

                # Начинаем реальную работу — ждём ItemMonitor и сбрасываем idle
                self._wait_items_busy()
                if self.loop_idle_event is not None:
                    self.loop_idle_event.clear()

                for tmpl in rotated:
                    if self.stop_event.is_set():
                        break
                    if not self.run_event.is_set():
                        self._wait_if_paused()
                        if self.stop_event.is_set():
                            break

                    stem = tmpl.stem

                    # In loop mode, a NOT_FOUND car should be rechecked later
                    if loop_mode and self._should_skip_due_next_check(stem):
                        continue

                    # Post attempts
                    result = None
                    for attempt in range(1, 3):
                        result = self.post_one_item(cfg, tmpl, self.run_event, self.stop_event)
                        if self.stop_event.is_set():
                            result = PostResult(status="STOPPED")
                            break

                        if result.status == "OK":
                            break

                        # NOT_FOUND => do NOT spam, just schedule recheck and move on
                        if result.status == "TEMPLATE_NOT_FOUND":
                            # Soft retry: don't refresh UI immediately; just wait a bit and let the matcher retry.
                            soft_retry = bool(cfg.get("not_found_soft_retry", True))
                            if soft_retry:
                                self._sleep_coop(0.25)
                                continue
                            # Fallback: re-enter create to keep UI stable
                            self.enter_create_rent(cfg, self.run_event, self.stop_event)
                            continue

                        # Other failures: re-enter create and retry
                        self.log(f"[{stem}] post failed attempt {attempt} ({result.status}) -> re-enter create")
                        self.enter_create_rent(cfg, self.run_event, self.stop_event)
                        self._sleep_coop(0.7)

                    if result is None:
                        continue

                    if result.status == "OK":
                        created_any = True

                        # processed only in oneshot
                        if not loop_mode:
                            self.processed.add(tmpl.name)
                            self.save_processed(self.processed)

                        # photo hash commit (depends on policy)
                        if dedupe_policy == "on_success" and result.photo_hash_to_commit:
                            self.photo_hashes[stem] = result.photo_hash_to_commit
                            self.save_photo_hashes(self.photo_hashes)

                        # stats & archive
                        try:
                            self.record_post(stem, float(result.price_value))
                            self.append_archive_row(stem, result.price_raw, float(result.price_value))
                        except Exception:
                            pass

                        # After POST the service redirects to "My Ads" page.
                        # We MUST navigate back to Create->Rent, bypassing cooldown,
                        # otherwise the bot will scan "My Ads" and try to re-post
                        # vehicles that are already listed.
                        try:
                            if bool(cfg.get("enter_create_after_ok", True)):
                                # Wait for the server-side cooldown first
                                post_cooldown = float(cfg.get("post_ok_server_cooldown", 5.0))
                                self._sleep_coop(post_cooldown)
                                # Force navigation (reset cooldown so it actually clicks)
                                from wiwang_poster_loop import NAV_STATE
                                NAV_STATE["last_enter"] = 0.0
                                self.enter_create_rent(cfg, self.run_event, self.stop_event)
                        except Exception:
                            pass

                        wait_s = random.uniform(post_interval_min, post_interval_max)
                        self.log(f"[{stem}] next post in ~{wait_s:.1f}s")
                        # After a successful post, avoid immediately re-scanning the same vehicle in loop mode.
                        # This prevents long "NOT FOUND" loops when the posted vehicle disappears from the list.
                        try:
                            ok_delay = float(cfg.get("ok_retry_delay_loop", 240.0))
                        except Exception:
                            ok_delay = 240.0
                        self._schedule_next_check(stem, ok_delay)
                        self._sleep_coop(wait_s)
                        continue

                    if result.status == "DUPLICATE_HASH":
                        # In loop mode we usually disable dedupe; if enabled, schedule recheck.
                        if loop_mode:
                            self.log(f"[{stem}] DUPLICATE_HASH -> retry later (loop)")
                            self._schedule_next_check(stem, retry_not_found)
                        else:
                            # In oneshot, mark processed so it doesn't block queue forever
                            self.log(f"[{stem}] DUPLICATE_HASH -> marked processed (oneshot)")
                            self.processed.add(tmpl.name)
                            self.save_processed(self.processed)
                        continue

                    if result.status == "TEMPLATE_NOT_FOUND":
                        if loop_mode:
                            # In loop mode most templates are NOT on screen.
                            # Schedule a short delay to avoid scanning the same vehicle
                            # immediately on the next sweep (reduces CPU / UI spam).
                            nf_delay = float(cfg.get("not_found_loop_delay", 5.0))
                            self.log(f"[{stem}] NOT FOUND on screen -> recheck in {nf_delay:.0f}s")
                            self._schedule_next_check(stem, nf_delay)
                        else:
                            # In oneshot, monitoring makes sense to wait for the item to appear.
                            self.log(f"[{stem}] NOT FOUND on screen -> retry in ~{retry_not_found:.1f}s (monitor mode)")
                            self._schedule_next_check(stem, retry_not_found)
                        continue

                    if result.status == "PLATE_MISMATCH":
                        delay = float(cfg.get("plate_mismatch_retry_delay", max(15.0, retry_not_found)))
                        self.log(f"[{stem}] PLATE mismatch -> retry in ~{delay:.1f}s")
                        self._schedule_next_check(stem, delay)
                        continue

                    if result.status == "BLOCKED":
                        delay = float(cfg.get("blocked_retry_delay", max(10.0, retry_not_found)))
                        reason = f" ({result.reason})" if result.reason else ""
                        self.log(f"[{stem}] BLOCKED{reason} -> retry in ~{delay:.1f}s")
                        self._schedule_next_check(stem, delay)
                        continue

                    if result.status in ("INVALID_ITEM",):
                        if not loop_mode:
                            self.processed.add(tmpl.name)
                            self.save_processed(self.processed)
                        continue

                    # Errors / timeouts / dialog failures
                    self.log(f"[{stem}] {result.status} -> retry in ~{retry_error:.1f}s")
                    if loop_mode:
                        self._schedule_next_check(stem, retry_error)
                    else:
                        self._sleep_coop(retry_error)

                # Пауза между циклами — ничего не выставлено
                if not created_any:
                    # Если все машины на cooldown — бот в режиме ожидания
                    if loop_mode and self.next_check_at:
                        try:
                            soonest = min(self.next_check_at.values())
                            now_ts = time.time()
                            wait_due = soonest - now_ts
                        except Exception:
                            wait_due = 0.0
                        if wait_due > 0.25:
                            # Все машины на cooldown — бот в режиме ожидания
                            if self.loop_idle_event is not None:
                                self.loop_idle_event.set()
                            self._sleep_coop(min(5.0, wait_due))
                            continue

                    # Полный свип не дал результата — все машины не найдены/выставлены
                    self._visible_fallback_last = 0.0
                    # Бот в idle — экран свободен, можно запустить Items Monitor + bump
                    if self.loop_idle_event is not None:
                        self.loop_idle_event.set()

                    # Try bump (click through My Ads to refresh views)
                    try:
                        from wiwang_poster_loop import IdleTracker
                        IdleTracker.maybe_bump(cfg, self.run_event, self.stop_event)
                    except Exception:
                        pass

                    self._sleep_coop(max(1.5, cycle_sleep))

            except Exception as e:
                self.log(f"LoopManager outer error: {e}\n{traceback.format_exc()}")
                self._sleep_coop(1.0)

        self.log("LoopManager stopped")
