import tkinter as tk
from typing import Dict, Optional


class MetricCard(tk.Frame):
    def __init__(
        self,
        parent,
        title: str,
        value: str = "—",
        *,
        colors: Optional[Dict[str, str]] = None,
        accent: Optional[str] = None,
    ) -> None:
        self._colors = colors or {}
        bg = self._colors.get("panel", "#16213e")
        border = self._colors.get("border", "#2a2a4a")
        super().__init__(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        self._title = title
        self._value = value
        self._target = value

        accent_color = accent or self._colors.get("accent", "#4a6fa5")

        # Calm left accent bar (4px thick) — Grafana card style
        left_bar = tk.Frame(self, bg=accent_color, width=4)
        left_bar.pack(fill="y", side="left")

        body = tk.Frame(self, bg=bg)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        self.title_label = tk.Label(
            body,
            text=title.upper(),
            bg=bg,
            fg=self._colors.get("muted", "#7a7a8e"),
            font=("Segoe UI", 8),
        )
        self.value_label = tk.Label(
            body,
            text=value,
            bg=bg,
            fg=self._colors.get("fg", "#d4d4dc"),
            font=("Segoe UI", 18, "bold"),
        )

        self.title_label.pack(anchor="w")
        self.value_label.pack(anchor="w", pady=(2, 0))

    def set_value(self, value: str) -> None:
        self._target = value
        self._value = value
        self.value_label.configure(text=value)

    def animate_to(self, new_value: float, prefix: str = "", suffix: str = "") -> None:
        try:
            start = float(self._value) if isinstance(self._value, (int, float)) else 0.0
        except Exception:
            start = 0.0
        steps = 14
        delta = (new_value - start) / max(1, steps)

        def _step(i: int, current: float) -> None:
            val = current + delta
            self._value = val
            self.value_label.configure(text=f"{prefix}{val:,.0f}{suffix}")
            if i < steps:
                self.after(35, _step, i + 1, val)
            else:
                self._value = new_value
                self.value_label.configure(text=f"{prefix}{new_value:,.0f}{suffix}")

        _step(0, start)
