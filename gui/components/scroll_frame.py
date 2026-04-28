import tkinter as tk
from tkinter import ttk
from typing import Dict, Optional


class ScrollFrame(ttk.Frame):
    def __init__(self, parent, *args, colors: Optional[Dict[str, str]] = None, **kwargs) -> None:
        super().__init__(parent, *args, **kwargs)
        self._colors = colors or {}
        bg = self._colors.get("bg", "#1a1a2e")
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.configure(style="Root.TFrame")

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.inner.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
