import tkinter as tk
from tkinter import ttk
from typing import Iterable, List, Tuple


class Table(ttk.Frame):
    def __init__(self, parent, columns: List[Tuple[str, str, int]]) -> None:
        super().__init__(parent)
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=10)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.hscrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self.scrollbar.set, xscrollcommand=self.hscrollbar.set)

        for key, title, width in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")

        self.hscrollbar.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

    def set_rows(self, rows: Iterable[Tuple[str, ...]]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in rows:
            self.tree.insert("", "end", values=row)
