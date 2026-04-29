import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, messagebox

from gui.auth.admin_gate import AdminGate
from gui.persistence.ui_prefs import load_ui_prefs, save_ui_prefs
from gui.services.metrics_engine import compute_error_counts, compute_metrics
from gui.services.tg_service import TgService
from gui.state.app_state import AppState
from gui.views.admin_panel import AdminPanel, attach_admin_tabs
from gui.views.tg_dashboard import TgDashboard

# ── Автоочистка мусора ────────────────────────────────────────────────────────────────────────
try:
    from cleaner import start_cleaner
    _CLEANER_AVAILABLE = True
except ImportError:
    _CLEANER_AVAILABLE = False

# ── Telegram трекер аренды ───────────────────────────────────────────────────────────────────────
try:
    import tg_rent_tracker as _tg_tracker
    _TG_TRACKER_AVAILABLE = True
except ImportError:
    _TG_TRACKER_AVAILABLE = False


class AppGUI:
    _AUTH_NOTICE_INTERVAL = 3

    def __init__(self, root, *, cfg_provider, cfg_save, log_fn, colors: Dict[str, str]) -> None:
        self.root = root
        self.cfg_provider = cfg_provider
        self.cfg_save = cfg_save
        self._log_lines: list = []
        _orig_log = log_fn

        def _wrapped_log(msg: str) -> None:
            try:
                _orig_log(msg)
            except Exception:
                pass
            try:
                self._log_lines.append(msg)
                if len(self._log_lines) > 500:
                    self._log_lines = self._log_lines[-500:]
            except Exception:
                pass

        self.log = _wrapped_log
        self.colors = colors

        self.state = AppState()
        self.admin_gate = AdminGate()
        self.tg_service = TgService()
        self.admin_unlocked = False
        self._remember_until_close = tk.BooleanVar(value=False)
        self._search_query = ""
        self._last_event_count = 0
        self._prefs_save_job = None
        self._last_auth_notice_ts = 0.0

        # ── Автоочистка мусора (логи, скрины) ────────────────────────────────────────────
        self._cleaner = None
        if _CLEANER_AVAILABLE:
            try:
                # Передаём log_fn — клинер будет писать в общий лог
                self._cleaner = start_cleaner(base_dir=".", log_fn=self.log)
            except Exception as exc:
                self.log(f"[CLEANER] Не удалось запустить: {exc}")

        # ── TG трекер аренды ──────────────────────────────────────────────────────────────────────
        self._tg_tracker_thread = None
        if _TG_TRACKER_AVAILABLE:
            try:
                cfg = cfg_provider()
                # Передаём только bool флаг — start() не принимает dict
                tg_enabled = bool((cfg or {}).get("telegram", {}).get("enabled", False))
                _tg_tracker.start(enabled=tg_enabled)
                # start() возвращает None — используем флаг для обозначения запуска
                self._tg_tracker_thread = True
            except Exception as exc:
                self.log(f"[TG_TRACKER] Не удалось запустить: {exc}")

        self._build_ui()
        self._restore_prefs()
        self._schedule_refresh()
        # Сохраняем геометрию при любом изменении размера/положения окна
        self.root.bind("<Configure>", lambda e: self._schedule_prefs_save())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ═══════════════════════════ UI BUILD ═════════════════════════════════════════

    def _on_close(self) -> None:
        for attr in ("_refresh_job", "_prefs_save_job"):
            try:
                job = getattr(self, attr, None)
                if job is not None:
                    self.root.after_cancel(job)
                    setattr(self, attr, None)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _build_ui(self) -> None:
        self.root.configure(bg=self.colors["bg"])
        self._apply_ttk_styles()
        self._build_toolbar()
        self._build_notebook()
        self._build_status_bar()

    def _apply_ttk_styles(self) -> None:
        """Apply Grafana-inspired calm dark theme to all ttk widgets."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            try:
                style.theme_use("default")
            except Exception:
                pass

        _bg      = self.colors["bg"]
        _panel   = self.colors["panel"]
        _fg      = self.colors["fg"]
        _muted   = self.colors.get("muted", "#7a7a8e")
        _accent  = self.colors["accent"]
        _accent2 = self.colors.get("accent2", "#5b8a72")
        _danger  = self.colors.get("danger", "#c0544e")
        _border  = self.colors["border"]
        _btn     = self.colors.get("btn", _panel)

        # Root / Frame
        style.configure("Root.TFrame",  background=_bg)
        style.configure("TFrame",        background=_bg)
        style.configure("Panel.TFrame",  background=_panel, relief="flat")

        # Notebook
        style.configure("TNotebook",
                        background=_bg, borderwidth=0, tabmargins=[0, 0, 0, 0])
        style.configure("TNotebook.Tab",
                        background=_panel, foreground=_muted,
                        padding=(16, 8), font=("Segoe UI", 9), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", _bg), ("active", _btn)],
                  foreground=[("selected", _fg), ("active", _fg)],
                  expand=[("selected", [1, 1, 1, 0])])

        # Labels
        style.configure("TLabel",
                        background=_bg, foreground=_fg, font=("Segoe UI", 9))
        style.configure("SectionTitle.TLabel",
                        background=_bg, foreground=_accent,
                        font=("Segoe UI", 10, "bold"))
        style.configure("Muted.TLabel",
                        background=_bg, foreground=_muted, font=("Segoe UI", 9))
        style.configure("Accent.TLabel",
                        background=_bg, foreground=_accent,
                        font=("Segoe UI", 9, "bold"))

        # Buttons
        style.configure("TButton",
                        background=_btn, foreground=_fg,
                        padding=(12, 6), font=("Segoe UI", 9),
                        borderwidth=1, relief="flat",
                        focusthickness=0, focuscolor="none")
        style.map("TButton",
                  background=[("active", "#2e4268"), ("pressed", _accent)],
                  foreground=[("active", _fg), ("pressed", _fg)],
                  relief=[("pressed", "flat")])

        style.configure("Danger.TButton",
                        background=_danger, foreground="#ffffff",
                        padding=(12, 6), font=("Segoe UI", 9, "bold"),
                        borderwidth=0, relief="flat",
                        focusthickness=0, focuscolor="none")
        style.map("Danger.TButton",
                  background=[("active", "#a84540"), ("pressed", "#8a3830")],
                  foreground=[("active", "#ffffff")])

        # Entry
        style.configure("TEntry",
                        fieldbackground=_panel, foreground=_fg,
                        insertcolor=_accent, borderwidth=1, relief="flat",
                        padding=(6, 4), font=("Segoe UI", 9))
        style.map("TEntry",
                  fieldbackground=[("focus", _panel)],
                  bordercolor=[("focus", _accent), ("!focus", _border)])
        style.configure("Search.TEntry",
                        fieldbackground=_panel, foreground=_fg,
                        insertcolor=_accent, borderwidth=1, relief="flat",
                        padding=(6, 4), font=("Segoe UI", 9))

        # Scrollbar
        style.configure("TScrollbar",
                        background=_panel, troughcolor=_bg,
                        arrowcolor=_muted, borderwidth=0, relief="flat")
        style.map("TScrollbar",
                  background=[("active", _border), ("pressed", _accent)])

        # Treeview
        _panel2 = self.colors.get("panel2", "#0f3460")
        style.configure("Treeview",
                        background=_panel, fieldbackground=_panel, foreground=_fg,
                        rowheight=26, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                        background=_panel2, foreground=_muted,
                        font=("Segoe UI", 9, "bold"), relief="flat", padding=(8, 5))
        style.map("Treeview",
                  background=[("selected", _accent)],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading",
                  background=[("active", _btn)],
                  foreground=[("active", _fg)])

        # Checkbutton
        style.configure("TCheckbutton",
                        background=_bg, foreground=_fg,
                        font=("Segoe UI", 9), focuscolor="none")
        style.map("TCheckbutton",
                  background=[("active", _bg)],
                  foreground=[("active", _accent)])

        # Combobox
        style.configure("TCombobox",
                        fieldbackground=_panel, background=_panel, foreground=_fg,
                        arrowcolor=_accent, borderwidth=1, relief="flat",
                        padding=(6, 4), font=("Segoe UI", 9))
        style.map("TCombobox",
                  fieldbackground=[("readonly", _panel), ("focus", _panel)],
                  foreground=[("readonly", _fg)],
                  selectbackground=[("readonly", _border)],
                  selectforeground=[("readonly", _accent)])

        # Progressbar
        style.configure("TProgressbar",
                        background=_accent, troughcolor=_border,
                        borderwidth=0, thickness=6)

        # Separator
        style.configure("TSeparator", background=_border)

    def _build_toolbar(self) -> None:
        _panel   = self.colors["panel"]
        _fg      = self.colors["fg"]
        _muted   = self.colors.get("muted", "#7a7a8e")
        _accent  = self.colors["accent"]
        _btn     = self.colors.get("btn", _panel)
        _border  = self.colors["border"]

        bar = tk.Frame(self.root, bg=_panel, pady=6)
        bar.pack(fill="x")

        # Left separator accent line
        tk.Frame(bar, bg=_accent, width=3).pack(side="left", fill="y", padx=(0, 8))

        tk.Label(bar, text="Поиск:", bg=_panel,
                 fg=_muted, font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search())
        _entry = tk.Entry(bar, textvariable=self._search_var, width=22,
                          bg=self.colors["bg"], fg=_fg,
                          insertbackground=_accent,
                          relief="flat", bd=1,
                          highlightthickness=1,
                          highlightcolor=_accent,
                          highlightbackground=_border,
                          font=("Segoe UI", 9))
        _entry.pack(side="left", ipady=3)

        # Цвет кнопок: если в colors нет ключа "btn" — используем "panel"
        _btn_bg = self.colors.get("btn", self.colors.get("panel", "#16213e"))
        tk.Button(bar, text="Обновить", command=self._manual_refresh,
                  bg=_btn_bg, fg=_accent,
                  activebackground=_border, activeforeground=_accent,
                  relief="flat", bd=0, padx=10, pady=3,
                  font=("Segoe UI", 9)).pack(side="left", padx=6)

        self._auth_btn = tk.Button(bar, text="Войти", command=self._toggle_admin,
                                   bg=_accent, fg=self.colors["bg"],
                                   activebackground=self.colors.get("accent2", "#5b8a72"),
                                   activeforeground=self.colors["bg"],
                                   relief="flat", bd=0, padx=10, pady=3,
                                   font=("Segoe UI", 9, "bold"))
        self._auth_btn.pack(side="right", padx=8)

    def _build_notebook(self) -> None:
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill="both", expand=True, padx=4, pady=(2, 4))

        # ── Главная вкладка ──────────────────────────────────────────────────────────────────
        self._main_frame = tk.Frame(self._nb, bg=self.colors["bg"])
        self._nb.add(self._main_frame, text="Главная")
        self._build_main_tab()

        # ── TG Dashboard ───────────────────────────────────────────────────────────────────────
        self._tg_frame = tk.Frame(self._nb, bg=self.colors["bg"])
        self._nb.add(self._tg_frame, text="TG")
        # TgDashboard принимает только: parent, *, on_search, colors
        self._tg_dashboard = TgDashboard(self._tg_frame, colors=self.colors,
                                         on_search=self._on_tg_search)

    def _build_main_tab(self) -> None:
        top = tk.Frame(self._main_frame, bg=self.colors["bg"])
        top.pack(fill="x", padx=8, pady=(8, 2))

        self._metrics_labels: Dict[str, tk.Label] = {}
        _accent_colors = {
            "active":  self.colors["accent"],
            "errors":  self.colors.get("danger", "#c0544e"),
            "paused":  self.colors.get("warning", "#c4903e"),
            "total":   self.colors.get("accent2", "#5b8a72"),
        }
        for key, title in [("active", "Активных"), ("errors", "Ошибок"),
                           ("paused", "Пауза"), ("total", "Всего")]:
            frm = tk.Frame(top, bg=self.colors["panel"],
                           highlightthickness=1,
                           highlightbackground=self.colors["border"])
            frm.pack(side="left", padx=4, pady=2, ipadx=10, ipady=6)
            # Accent top bar
            tk.Frame(frm, bg=_accent_colors[key], height=2).pack(fill="x", side="top")
            tk.Label(frm, text=title, bg=self.colors["panel"],
                     fg=self.colors.get("muted", "#7a7a8e"),
                     font=("Segoe UI", 8, "bold")).pack(padx=8, pady=(4, 0))
            lbl = tk.Label(frm, text="-", bg=self.colors["panel"],
                           fg=_accent_colors[key], font=("Segoe UI", 16, "bold"))
            lbl.pack(padx=8, pady=(2, 6))
            self._metrics_labels[key] = lbl

        # ── Таблица задач ──────────────────────────────────────────────────────────────────
        cols = ("id", "name", "status", "platform", "last_run", "next_run", "errors")
        self._tree = ttk.Treeview(self._main_frame, columns=cols,
                                   show="headings", selectmode="browse")
        for col, w, title in [
            ("id",       50,  "ID"),
            ("name",    180,  "Название"),
            ("status",   80,  "Статус"),
            ("platform", 80,  "Платформа"),
            ("last_run", 130, "Посл. запуск"),
            ("next_run", 130, "След. запуск"),
            ("errors",   60,  "Ошибок"),
        ]:
            self._tree.heading(col, text=title)
            self._tree.column(col, width=w, anchor="center")

        vsb = ttk.Scrollbar(self._main_frame, orient="vertical",
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        vsb.pack(side="left", fill="y", pady=4)

        self._tree.tag_configure("error",  background="#2a1a1a", foreground=self.colors.get("danger", "#c0544e"))
        self._tree.tag_configure("paused", background="#26221a", foreground=self.colors.get("warning", "#c4903e"))
        self._tree.tag_configure("active", background="#1a2520", foreground=self.colors.get("success", "#4caf7c"))

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self.root, bg=self.colors["panel"],
                       highlightthickness=1,
                       highlightbackground=self.colors["border"])
        bar.pack(fill="x", side="bottom")
        # Accent left stripe
        tk.Frame(bar, bg=self.colors["accent"], width=3).pack(side="left", fill="y")
        self._status_lbl = tk.Label(bar, text="Готово", bg=self.colors["panel"],
                                    fg=self.colors.get("muted", "#7a7a8e"),
                                    anchor="w", font=("Segoe UI", 8))
        self._status_lbl.pack(fill="x", padx=8, pady=3)

    # ═══════════════════════════ DATA REFRESH ═════════════════════════════════════════

    def _schedule_refresh(self, interval_ms: int = 15_000) -> None:
        try:
            if getattr(self, "_refresh_job", None) is not None:
                self.root.after_cancel(self._refresh_job)
                self._refresh_job = None
        except Exception:
            pass
        try:
            self._refresh_job = self.root.after(interval_ms, self._auto_refresh)
        except Exception:
            pass

    def _auto_refresh(self) -> None:
        self._refresh_job = None
        try:
            self._do_refresh()
            self._schedule_refresh()
        except Exception as exc:
            self.log(f"[GUI] _auto_refresh crashed: {exc}")
            self._schedule_refresh(30_000)  # backoff to 30s on error

    def _manual_refresh(self) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        try:
            now_ts = time.time()
            try:
                new_events = self.tg_service.poll()
                if new_events:
                    self.state.add_events(new_events)
            except Exception:
                pass

            snapshot = self.state.get_snapshot()
            events = snapshot.get("events", [])
            errors = snapshot.get("errors", [])

            try:
                records = self.tg_service.records()
                metrics = compute_metrics(records, now_ts)
                ec = compute_error_counts(errors, now_ts)
                tg_status = self.tg_service.status()
                _last_ts = tg_status.get("last_message_ts") or 0
                try:
                    _age_sec = int(now_ts - float(_last_ts)) if _last_ts else None
                    if _age_sec is None:
                        _age_str = "—"
                    elif _age_sec < 60:
                        _age_str = f"{_age_sec}s"
                    elif _age_sec < 3600:
                        _age_str = f"{_age_sec // 60}m"
                    else:
                        _age_str = f"{_age_sec // 3600}h {(_age_sec % 3600) // 60}m"
                except Exception:
                    _age_str = "—"
                self._tg_dashboard.update_cards({
                    "today": metrics.get("totals", {}).get("today", 0),
                    "week": metrics.get("totals", {}).get("week", 0),
                    "month": metrics.get("totals", {}).get("month", 0),
                    "active": metrics.get("active", 0),
                    "avg_rate": metrics.get("avg_rate", 0),
                    "top_vehicle": metrics.get("top_vehicle", "—"),
                    "errors": f"{ec.get('10m',0)}/{ec.get('1h',0)}",
                    "last_msg_age": _age_str,
                })
                self._tg_dashboard.update_rentals(records[-50:])
                veh = metrics.get("by_vehicle", {})
                self._tg_dashboard.update_vehicles([{
                    "vehicle": k,
                    "plate": v.get("plate", ""),
                    "income_7d": v.get("income_7d", 0),
                    "income_30d": v.get("income_30d", 0),
                    "total_income": v.get("income_total", 0),
                    "avg_rate": round(v.get("income_total", 0) / max(v.get("hours", 1), 0.01), 1),
                    "count": v.get("count", 0),
                } for k, v in sorted(veh.items(), key=lambda x: x[1].get("income_total", 0), reverse=True)])
                try:
                    self._tg_dashboard.pulse()
                except Exception:
                    pass
                try:
                    log_lines = getattr(self, "_log_lines", [])
                    self._tg_dashboard.update_logs(log_lines[-200:])
                except Exception:
                    pass
            except Exception:
                pass

            self._refresh_tree(events)
            self._refresh_metrics(events, errors, now_ts)
            self._update_status(f"Обновлено: {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
        except Exception as exc:
            self.log(f"[GUI] Ошибка обновления: {exc}")
            self._update_status(f"Ошибка: {exc}")

    def _refresh_tree(self, events: List[Dict[str, Any]]) -> None:
        query = self._search_query.lower()
        self._tree.delete(*self._tree.get_children())
        for t in events:
            name = str(t.get("vehicle_key") or t.get("plate") or t.get("name") or "")
            if query and query not in name.lower():
                continue
            status = str(t.get("status") or "аренда")
            errs   = int(t.get("error_count") or 0)
            tag    = "error" if errs else ("paused" if status == "paused" else "active")
            self._tree.insert("", "end", values=(
                t.get("id", ""),
                name,
                status,
                t.get("platform", ""),
                str(t.get("timestamp") or ""),
                "",
                errs,
            ), tags=(tag,))

    def _refresh_metrics(self, events: List[Dict[str, Any]],
                         errors: List[Dict[str, Any]], now_ts: float) -> None:
        m  = compute_metrics(events, now_ts)
        ec = compute_error_counts(errors, now_ts)
        # Карточки: Активных / Ошибок / Пауза / Всего
        self._metrics_labels["active"].config(text=str(m.get("active", 0)))
        self._metrics_labels["errors"].config(text=f"{ec.get('10m',0)}/{ec.get('1h',0)}")
        self._metrics_labels["paused"].config(text="—")
        self._metrics_labels["total"].config(text=str(len(events)))

    # ═══════════════════════════ SEARCH ══════════════════════════════════════════════

    def _on_search(self) -> None:
        self._search_query = self._search_var.get()
        self._do_refresh()

    def _on_tg_search(self, query: str, submit: bool = False) -> None:
        """Callback-поиск из TgDashboard — перенаправляем в общий поиск."""
        self._search_query = query
        self._do_refresh()

    # ═══════════════════════════ ADMIN AUTH ═════════════════════════════════════════

    def _toggle_admin(self) -> None:
        if self.admin_unlocked:
            self._lock_admin()
        else:
            self._try_unlock_admin()

    def _try_unlock_admin(self) -> None:
        now = time.time()
        if now - self._last_auth_notice_ts < self._AUTH_NOTICE_INTERVAL:
            return
        self._last_auth_notice_ts = now

        win = tk.Toplevel(self.root)
        win.title("Вход")
        win.resizable(False, False)
        win.grab_set()
        win.focus_force()  # принудительный фокус — иначе grab_set мешает keyboard events

        tk.Label(win, text="Пароль:").grid(row=0, column=0, padx=8, pady=8)
        pwd_var = tk.StringVar()
        entry = tk.Entry(win, textvariable=pwd_var, show="*")
        entry.grid(row=0, column=1, padx=8, pady=8)

        # Ctrl+V и ПКМ — вставка пароля из буфера обмена
        def _paste_password(event=None):
            try:
                # Пробуем через root (надёжнее на Windows), затем через win
                try:
                    text = self.root.clipboard_get()
                except tk.TclError:
                    text = win.clipboard_get()
                if not text:
                    return "break"
                cur = entry.index(tk.INSERT)
                try:
                    sel_start = entry.index(tk.SEL_FIRST)
                    sel_end   = entry.index(tk.SEL_LAST)
                    new_val = pwd_var.get()[:sel_start] + text + pwd_var.get()[sel_end:]
                    pwd_var.set(new_val)
                    entry.icursor(sel_start + len(text))
                except tk.TclError:
                    new_val = pwd_var.get()[:cur] + text + pwd_var.get()[cur:]
                    pwd_var.set(new_val)
                    entry.icursor(cur + len(text))
            except tk.TclError:
                pass
            return "break"

        # Привязываем Ctrl+V на entry И на уровне всего окна (grab_set может поглощать)
        entry.bind("<Control-v>", _paste_password)
        entry.bind("<Control-V>", _paste_password)
        entry.bind("<<Paste>>",   _paste_password)
        win.bind("<Control-v>",   lambda e: _paste_password())
        win.bind("<Control-V>",   lambda e: _paste_password())

        # Контекстное меню (правая кнопка мыши) — тоже вставка
        ctx_menu = tk.Menu(win, tearoff=0)
        ctx_menu.add_command(label="Вставить", command=_paste_password)
        def _show_ctx(event):
            try:
                ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                ctx_menu.grab_release()
        entry.bind("<Button-3>", _show_ctx)

        entry.focus_set()

        remember_var = tk.BooleanVar(value=self._remember_until_close.get())
        tk.Checkbutton(win, text="Запомнить до закрытия",
                       variable=remember_var).grid(row=1, column=0, columnspan=2)

        def _submit(*_):
            candidate = pwd_var.get()
            cfg = self.cfg_provider()
            # Проверяем через check_password; если gate выключен — любой пароль даёт доступ
            gate_cfg = cfg.get("admin_gate", {}) if isinstance(cfg.get("admin_gate"), dict) else {}
            if gate_cfg.get("enabled"):
                ok = self.admin_gate.check_password(cfg, candidate)
            else:
                # Если gate не настроен — вход без пароля
                ok = True
            if ok:
                if remember_var.get():
                    self._remember_until_close.set(True)
                self._unlock_admin()
                win.destroy()
            else:
                tk.messagebox.showerror("Ошибка", "Неверный пароль", parent=win)

        entry.bind("<Return>", _submit)
        tk.Button(win, text="OK", command=_submit).grid(
            row=2, column=0, columnspan=2, pady=8)

    def _unlock_admin(self) -> None:
        self.admin_unlocked = True
        self._auth_btn.config(text="Выйти")
        # attach_admin_tabs(апп, build_callbacks) — первый арг self, второй — callable
        attach_admin_tabs(self, self._build_admin_tabs)
        # Запускаем полный App (wiwang_poster_loop.py) как subprocess через событие <<AdminUnlocked>>
        # main() в wiwang_poster_loop.py слушает это событие и запускает subprocess с ADMIN_MODE=1
        self.root.event_generate("<<AdminUnlocked>>")

    def _build_admin_tabs(self) -> None:
        """Cтроит вкладку Admin в нотбуке."""
        # Вкладка Admin Panel
        admin_frame = tk.Frame(self._nb, bg=self.colors["bg"])
        self._nb.add(admin_frame, text="Admin")
        panel = AdminPanel(admin_frame, colors=self.colors)
        panel.pack(fill="both", expand=True)

    def _lock_admin(self) -> None:
        self.admin_unlocked = False
        setattr(self, "_admin_tabs_attached", False)  # сброс флага
        if not self._remember_until_close.get():
            self._auth_btn.config(text="Войти")
            # Удаляем вкладки начиная с 3-й (Главная + TG остаются)
            tabs = list(self._nb.tabs())
            for tab in tabs[2:]:
                self._nb.forget(tab)

    # ═══════════════════════════ PREFS ════════════════════════════════════════════════

    def _restore_prefs(self) -> None:
        # load_ui_prefs читает поле ui_prefs из cfg словаря
        try:
            cfg = self.cfg_provider()
            prefs = load_ui_prefs(cfg)
        except Exception:
            prefs = {}
        if "geometry" in prefs:
            try:
                self.root.geometry(prefs["geometry"])
            except Exception:
                pass

    def _schedule_prefs_save(self) -> None:
        if self._prefs_save_job:
            self.root.after_cancel(self._prefs_save_job)
        self._prefs_save_job = self.root.after(500, self._flush_prefs)

    def _flush_prefs(self) -> None:
        # save_ui_prefs обновляет ui_prefs в cfg и возвращает обновлённый cfg
        try:
            cfg = self.cfg_provider()
            updated = save_ui_prefs(cfg, {"geometry": self.root.winfo_geometry()})
            self.cfg_save(updated)
        except Exception:
            pass
        self._prefs_save_job = None

    # ═══════════════════════════ STATUS ══════════════════════════════════════════════

    def _update_status(self, msg: str) -> None:
        try:
            self._status_lbl.config(text=msg)
        except Exception:
            pass
