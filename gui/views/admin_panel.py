import json
from typing import Callable, Optional

import tkinter as tk
from tkinter import ttk

import tg_rent_tracker
from gui.components.scroll_frame import ScrollFrame
from gui.components.status_bar import StatusBar


class AdminPanel(ttk.Frame):
    _STATUS_REFRESH_MS = 800

    def __init__(self, parent, *, colors: dict[str, str]) -> None:
        super().__init__(parent)
        self._colors = colors
        self._status_job: Optional[str] = None
        self.configure(style="Root.TFrame")

        _bg     = self._colors.get("bg",     "#1a1a2e")
        _panel  = self._colors.get("panel",  "#16213e")
        _fg     = self._colors.get("fg",     "#d4d4dc")
        _muted  = self._colors.get("muted",  "#7a7a8e")
        _accent = self._colors.get("accent", "#4a6fa5")
        _border = self._colors.get("border", "#2a2a4a")
        _danger = self._colors.get("danger", "#c0544e")
        _success = self._colors.get("success", "#4caf7c")

        self.scroll = ScrollFrame(self, colors=self._colors)
        self.scroll.pack(fill="both", expand=True)

        # ── Header ────────────────────────────────────────────────────────────
        header = tk.Frame(self.scroll.inner, bg=_panel,
                          highlightthickness=1, highlightbackground=_border)
        header.pack(fill="x", padx=20, pady=(20, 12))

        # Accent top bar in header
        tk.Frame(header, bg=_accent, height=2).pack(fill="x", side="top")

        header_inner = tk.Frame(header, bg=_panel)
        header_inner.pack(fill="x", padx=12, pady=8)

        tk.Label(header_inner, text="Admin Panel",
                 bg=_panel, fg=_accent,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        self.status_bar = StatusBar(header_inner, colors=self._colors)
        self.status_bar.pack(side="left", fill="x", expand=True, padx=(16, 0))
        self.status_bar.set_text("Admin controls")

        # ── Body ──────────────────────────────────────────────────────────────
        body = tk.Frame(self.scroll.inner, bg=_bg)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        # Section: TG tracker control
        tk.Frame(body, bg=_accent, height=1).pack(fill="x", pady=(0, 8))
        ttk.Label(body, text="TG tracker control",
                  style="SectionTitle.TLabel").pack(anchor="w", pady=(0, 4))

        controls = tk.Frame(body, bg=_panel,
                            highlightthickness=1, highlightbackground=_border)
        controls.pack(fill="x", pady=(0, 12))
        controls_inner = tk.Frame(controls, bg=_panel)
        controls_inner.pack(fill="x", padx=12, pady=8)

        ttk.Button(controls_inner, text="Start TG tracker",
                   command=self._start_tracker).pack(side="left", padx=(0, 8))
        ttk.Button(controls_inner, text="Disable",
                   command=self._disable_tracker).pack(side="left")

        # Section: TG tracker status (live)
        tk.Frame(body, bg=_border, height=1).pack(fill="x", pady=(4, 8))
        ttk.Label(body, text="TG tracker status (live)",
                  style="SectionTitle.TLabel").pack(anchor="w", pady=(0, 4))

        status_box = tk.Frame(body, bg=_panel,
                              highlightthickness=1, highlightbackground=_border)
        status_box.pack(fill="x", pady=(0, 0))
        # Accent top bar on status box
        tk.Frame(status_box, bg=_accent, height=2).pack(fill="x", side="top")

        self.status_text = tk.Text(
            status_box,
            height=7,
            wrap="word",
            bg=_panel,
            fg=_fg,
            insertbackground=_accent,
            selectbackground=_border,
            selectforeground=_accent,
            relief="flat",
            padx=12,
            pady=8,
            font=("Consolas", 9),
        )
        self.status_text.configure(state="disabled", takefocus=0)
        self.status_text.pack(fill="x", padx=0, pady=0)

        self._refresh_status()

    def _start_tracker(self) -> None:
        tg_rent_tracker.start(enabled=True)

    def _disable_tracker(self) -> None:
        if hasattr(tg_rent_tracker, "disable"):
            tg_rent_tracker.disable()
        elif hasattr(tg_rent_tracker, "stop"):
            tg_rent_tracker.stop()

    def _refresh_status(self) -> None:
        status = tg_rent_tracker.get_status()
        summary = f"TG tracker: {status.get('state', 'unknown')} · enabled={status.get('enabled', False)}"
        last_error = status.get("last_error")
        if last_error:
            summary = f"{summary} · {last_error}"
        self.status_bar.set_text(summary)
        payload = json.dumps(status, ensure_ascii=False, indent=2)
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.insert("end", payload)
        self.status_text.configure(state="disabled")
        if self.winfo_exists():
            self._status_job = self.after(self._STATUS_REFRESH_MS, self._refresh_status)

    def destroy(self) -> None:
        if self._status_job is not None:
            try:
                self.after_cancel(self._status_job)
            except Exception:
                pass
            self._status_job = None
        super().destroy()


def attach_admin_tabs(app, build_callbacks: Callable[[], None]) -> None:
    if getattr(app, "_admin_tabs_attached", False):
        return
    setattr(app, "_admin_tabs_attached", True)
    try:
        build_callbacks()
    except Exception:
        setattr(app, "_admin_tabs_attached", False)
        raise
