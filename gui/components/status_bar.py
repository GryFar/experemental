import tkinter as tk
from typing import Dict, Optional


class StatusBar(tk.Frame):
    def __init__(self, parent, *, colors: Optional[Dict[str, str]] = None) -> None:
        self._colors = colors or {}
        bg = self._colors.get("panel", "#16213e")
        muted = self._colors.get("muted", "#7a7a8e")
        super().__init__(
            parent,
            bg=bg,
            highlightthickness=0,
        )

        self.label = tk.Label(
            self,
            text="—",
            bg=bg,
            fg=muted,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.label.pack(side="left", padx=(4, 8), pady=4, fill="x", expand=True)

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)
