import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List

from gui.components.metric_card import MetricCard
from gui.components.scroll_frame import ScrollFrame
from gui.components.tables import Table
from gui.components.status_bar import StatusBar


class TgDashboard(ttk.Frame):
    def __init__(self, parent, *, on_search, colors: Dict[str, str]) -> None:
        super().__init__(parent)
        self._colors = colors
        self._on_search = on_search

        self.configure(style="Root.TFrame")

        self.scroll = ScrollFrame(self, colors=self._colors)
        self.scroll.pack(fill="both", expand=True)

        header = tk.Frame(self.scroll.inner, bg=self._colors["panel"],
                          highlightthickness=1,
                          highlightbackground=self._colors.get("border", "#2a2a4a"))
        header.pack(fill="x", padx=20, pady=(20, 12))

        self.status_bar = StatusBar(header, colors=self._colors)
        self.status_bar.pack(side="left", fill="x", expand=True)

        self.pulse_canvas = tk.Canvas(header, width=12, height=12, highlightthickness=0, bg=self._colors["panel"])
        self.pulse_canvas.pack(side="left", padx=(8, 0))
        self.pulse_dot = self.pulse_canvas.create_oval(2, 2, 10, 10,
                                                       fill=self._colors.get("accent", "#4a6fa5"), outline="")
        self._pulse_state = False

        search_box = tk.Frame(header, bg=self._colors["panel"])
        search_box.pack(side="right")
        tk.Label(
            search_box,
            text="Search:",
            bg=self._colors["panel"],
            fg=self._colors["muted"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 6))
        self.search_var = tk.StringVar(value="")
        self.search_entry = ttk.Entry(search_box, textvariable=self.search_var, width=28, style="Search.TEntry")
        self.search_entry.pack(side="left")
        self.search_entry.bind("<KeyRelease>", self._handle_filter, add="+")

        self.remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(search_box, text="Remember until close", variable=self.remember_var).pack(side="left", padx=(8, 0))
        ttk.Button(search_box, text="Search", command=self._handle_submit).pack(side="left", padx=(8, 0))

        cards = tk.Frame(self.scroll.inner, bg=self._colors["bg"])
        cards.pack(fill="x", padx=20, pady=(0, 8))

        # Grafana-inspired card accents — calm, differentiated by semantic color
        _acc  = self._colors.get("accent",  "#4a6fa5")  # steel blue
        _acc2 = self._colors.get("accent2", "#5b8a72")  # sage green
        _succ = self._colors.get("success", "#4caf7c")  # muted green
        _warn = self._colors.get("warning", "#c4903e")  # warm amber
        _dang = self._colors.get("danger",  "#c0544e")  # muted red
        self.card_today  = MetricCard(cards, "Today income",    colors=self._colors, accent=_acc)
        self.card_week   = MetricCard(cards, "Last 7 days",     colors=self._colors, accent=_acc2)
        self.card_month  = MetricCard(cards, "This month",      colors=self._colors, accent=_acc)
        self.card_active = MetricCard(cards, "Active rentals",  colors=self._colors, accent=_succ)
        self.card_avg    = MetricCard(cards, "Avg $/hour",       colors=self._colors, accent=_warn)
        self.card_top    = MetricCard(cards, "Top vehicle",      colors=self._colors, accent=_acc2)
        self.card_uptime = MetricCard(cards, "Last message age", colors=self._colors, accent=_acc)
        self.card_errors = MetricCard(cards, "Errors (10m/1h)", colors=self._colors, accent=_dang)

        for i, card in enumerate([
            self.card_today, self.card_week, self.card_month, self.card_active,
            self.card_avg, self.card_top, self.card_uptime, self.card_errors,
        ]):
            card.grid(row=i // 4, column=i % 4, padx=8, pady=8, sticky="nsew")
        for i in range(4):
            cards.grid_columnconfigure(i, weight=1)

        tables = tk.Frame(self.scroll.inner, bg=self._colors["bg"])
        tables.pack(fill="both", expand=True, padx=20, pady=8)

        ttk.Label(tables, text="Rentals feed", style="SectionTitle.TLabel").pack(anchor="w")
        self.rentals_table = Table(
            tables,
            columns=[
                ("timestamp", "timestamp", 150),
                ("plate", "plate", 100),
                ("vehicle_key", "vehicle", 120),
                ("hours", "hours", 60),
                ("rate", "rate", 80),
                ("total", "total", 80),
                ("source", "source", 80),
            ],
        )
        self.rentals_table.pack(fill="x", pady=(4, 16))

        ttk.Label(tables, text="Vehicles summary", style="SectionTitle.TLabel").pack(anchor="w")
        self.vehicles_table = Table(
            tables,
            columns=[
                ("vehicle", "vehicle", 140),
                ("plate", "plate", 100),
                ("income_7d", "income_7d", 100),
                ("income_30d", "income_30d", 110),
                ("total_income", "total", 90),
                ("avg_rate", "avg_rate", 90),
                ("count", "count", 70),
            ],
        )
        self.vehicles_table.pack(fill="x", pady=(4, 16))

        logs = tk.Frame(self.scroll.inner, bg=self._colors["bg"])
        logs.pack(fill="x", padx=20, pady=(0, 20))
        ttk.Label(logs, text="Logs", style="SectionTitle.TLabel").pack(anchor="w")
        logs_body = tk.Frame(logs, bg=self._colors["panel"],
                             highlightthickness=1,
                             highlightbackground=self._colors.get("border", "#2a2a4a"))
        logs_body.pack(fill="x", pady=(4, 0))
        self.logs_text = tk.Text(
            logs_body,
            height=6,
            wrap="none",
            bg=self._colors["panel"],
            fg=self._colors["fg"],
            insertbackground=self._colors.get("accent", "#4a6fa5"),
            selectbackground=self._colors.get("border", "#2a2a4a"),
            selectforeground=self._colors.get("fg", "#d4d4dc"),
            relief="flat",
            font=("Consolas", 9),
        )
        logs_scrollbar = ttk.Scrollbar(logs_body, orient="vertical", command=self.logs_text.yview)
        self.logs_text.configure(yscrollcommand=logs_scrollbar.set)
        self.logs_text.configure(state="disabled", takefocus=0)
        self.logs_text.bind("<Key>", self._block_log_input, add="+")
        self.logs_text.pack(side="left", fill="both", expand=True)
        logs_scrollbar.pack(side="right", fill="y")

    def _handle_filter(self, _event=None) -> None:
        value = self.search_var.get()
        if callable(self._on_search):
            self._on_search(value, submit=False)

    def _handle_submit(self) -> None:
        value = self.search_var.get()
        if callable(self._on_search):
            self._on_search(value, submit=True)

    def clear_search(self) -> None:
        self.search_var.set("")

    def set_status(self, text: str) -> None:
        self.status_bar.set_text(text)

    def update_cards(self, data: Dict[str, Any]) -> None:
        self.card_today.animate_to(float(data.get("today", 0)), prefix="$")
        self.card_week.animate_to(float(data.get("week", 0)), prefix="$")
        self.card_month.animate_to(float(data.get("month", 0)), prefix="$")
        self.card_active.animate_to(float(data.get("active", 0)))
        self.card_avg.animate_to(float(data.get("avg_rate", 0)), prefix="$")
        self.card_top.set_value(str(data.get("top_vehicle", "—")))
        self.card_uptime.set_value(str(data.get("last_msg_age", "—")))
        self.card_errors.set_value(str(data.get("errors", "0/0")))

    def update_rentals(self, rows: List[Dict[str, Any]]) -> None:
        table_rows = []
        for row in rows:
            rate = row.get("rate", "")
            if rate in (None, ""):
                rate = row.get("price_per_hour", "")
            total = row.get("total", "")
            if total in (None, ""):
                total = row.get("total_sum", "")
            table_rows.append((
                row.get("timestamp", ""),
                row.get("plate", ""),
                row.get("vehicle_key", ""),
                str(row.get("hours", "")),
                str(rate),
                str(total),
                str(row.get("source", "")),
            ))
        self.rentals_table.set_rows(table_rows)

    def update_vehicles(self, rows: List[Dict[str, Any]]) -> None:
        table_rows = []
        for row in rows:
            table_rows.append((
                row.get("vehicle", ""),
                row.get("plate", ""),
                f"{row.get('income_7d', 0):.0f}",
                f"{row.get('income_30d', 0):.0f}",
                f"{row.get('total_income', 0):.0f}",
                f"{row.get('avg_rate', 0):.0f}",
                str(row.get("count", 0)),
            ))
        self.vehicles_table.set_rows(table_rows)

    def update_logs(self, lines: List[str]) -> None:
        self.logs_text.configure(state="normal")
        self.logs_text.delete("1.0", "end")
        self.logs_text.insert("end", "\n".join(lines))
        self.logs_text.configure(state="disabled")

    def _block_log_input(self, event) -> str:
        if event.keysym in ("c", "C", "Insert") and (event.state & (0x4 | 0x20000)):
            return ""
        return "break"

    def pulse(self) -> None:
        self._pulse_state = not self._pulse_state
        # Alternate between accent (active) and muted (inactive) — calm pulsing
        color = self._colors.get("accent", "#4a6fa5") if self._pulse_state else self._colors.get("border", "#2a2a4a")
        self.pulse_canvas.itemconfigure(self.pulse_dot, fill=color)
