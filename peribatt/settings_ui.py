"""The settings window (tkinter). All decisions live in ``prefs.py``."""
from __future__ import annotations

import os
import sys
from typing import Callable, Dict, Optional

from . import DISPLAY_NAME, __version__, prefs
from .ui import window_icon

PAD = 8


def open_settings(root, app, open_learn: Optional[Callable[[], None]] = None,
                  autostart_enabled=None, set_autostart=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    win = tk.Toplevel(root)
    win.title(f"{DISPLAY_NAME} - Settings")
    win.minsize(520, 420)
    window_icon(win)

    kwargs = {}
    if autostart_enabled is not None:
        kwargs["autostart_enabled"] = autostart_enabled
    values = prefs.current(app.store, **kwargs)
    variables: Dict[str, tk.Variable] = {}

    book = ttk.Notebook(win)
    book.pack(fill="both", expand=True, padx=PAD, pady=(PAD, 0))

    # --- Devices tab ---------------------------------------------------------
    dev = ttk.Frame(book, padding=PAD)
    book.add(dev, text="Devices")
    cols = ("name", "kind", "status", "shown")
    tree = ttk.Treeview(dev, columns=cols, show="headings", height=8, selectmode="browse")
    for col, title, width in (("name", "Device", 220), ("kind", "Type", 80),
                              ("status", "Battery", 110), ("shown", "In tray", 60)):
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor="w")
    tree.grid(row=0, column=0, columnspan=5, sticky="nsew")
    dev.rowconfigure(0, weight=1)
    dev.columnconfigure(4, weight=1)

    def fill():
        tree.delete(*tree.get_children())
        for row in prefs.device_rows(app):
            name = row.name if row.name == row.original else f"{row.name}  ({row.original})"
            tree.insert("", "end", iid=row.key,
                        values=(name, row.kind, row.status, "no" if row.hidden else "yes"))

    def selected() -> Optional[str]:
        sel = tree.selection()
        return sel[0] if sel else None

    def rename():
        key = selected()
        if not key:
            return
        current = app.effective(app.readings[key]).name
        new = simpledialog.askstring("Rename", "Name shown in the tooltip\n(empty = original name):",
                                     initialvalue=current, parent=win)
        if new is not None:
            app.rename(key, new)
            fill()

    def toggle_hidden():
        key = selected()
        if key:
            prefs.set_hidden(app, key, key not in app.store["hidden"])
            fill()

    def forget():
        key = selected()
        if key:
            err = prefs.forget(app, key)
            if err:
                messagebox.showinfo("Forget device", err, parent=win)
            fill()

    buttons = (("Rename…", rename), ("Hide / show", toggle_hidden), ("Forget", forget))
    for i, (text, cmd) in enumerate(buttons):
        ttk.Button(dev, text=text, command=cmd).grid(row=1, column=i, pady=(PAD, 0), sticky="w",
                                                    padx=(0, 4))
    if open_learn:
        ttk.Button(dev, text="Learn a new device…", command=open_learn).grid(
            row=1, column=4, pady=(PAD, 0), sticky="e")
    ttk.Label(dev, style="Hint.TLabel", wraplength=480,
              text="Missing a device? Bluetooth devices appear once Windows shows their battery. "
                   "For a USB dongle that isn't supported yet, use Learn a new device.").grid(
        row=2, column=0, columnspan=5, sticky="w", pady=(PAD, 0))
    tree.bind("<Double-1>", lambda e: rename())
    fill()

    # --- option tabs -----------------------------------------------------------
    for title, opts in prefs.SECTIONS:
        frame = ttk.Frame(book, padding=PAD)
        book.add(frame, text=title)
        frame.columnconfigure(1, weight=1)
        for row, o in enumerate(opts):
            if isinstance(o, prefs.Toggle):
                var = tk.BooleanVar(value=bool(values[o.key]))
                ttk.Checkbutton(frame, text=o.label, variable=var).grid(
                    row=row, column=0, columnspan=3, sticky="w", pady=3)
            elif isinstance(o, prefs.Choice):
                labels = [label for _, label in o.options]
                current = next((label for v, label in o.options if v == values[o.key]), labels[0])
                var = tk.StringVar(value=current)
                ttk.Label(frame, text=o.label).grid(row=row, column=0, sticky="w", pady=3)
                ttk.Combobox(frame, textvariable=var, values=labels, state="readonly",
                             width=14).grid(row=row, column=1, sticky="w", padx=PAD)
            elif isinstance(o, prefs.Number):
                var = tk.StringVar(value=str(values[o.key]))
                ttk.Label(frame, text=o.label).grid(row=row, column=0, sticky="w", pady=3)
                ttk.Spinbox(frame, from_=o.lo, to=o.hi, textvariable=var, width=6).grid(
                    row=row, column=1, sticky="w", padx=PAD)
            else:
                var = tk.StringVar(value=str(values[o.key]))
                ttk.Label(frame, text=o.label).grid(row=row, column=0, sticky="w", pady=3)
                ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", padx=PAD)

                def browse(var=var):
                    path = filedialog.askopenfilename(
                        parent=win, title="headsetcontrol.exe",
                        filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
                    if path:
                        var.set(path)
                ttk.Button(frame, text="Browse…", command=browse).grid(row=row, column=2)
                ttk.Label(frame, text=o.hint, style="Hint.TLabel", wraplength=460).grid(
                    row=row + 1, column=0, columnspan=3, sticky="w")
            variables[o.key] = var

    # --- bottom bar -------------------------------------------------------------
    bar = ttk.Frame(win, padding=PAD)
    bar.pack(fill="x")
    error = ttk.Label(bar, style="Missing.TLabel", wraplength=300)
    error.pack(side="left")
    ttk.Label(bar, text=f"v{__version__}", style="Hint.TLabel").pack(side="left", padx=PAD)

    def form() -> dict:
        out = {}
        for o in prefs.options():
            v = variables[o.key].get()
            if isinstance(o, prefs.Choice):
                v = next((value for value, label in o.options if label == v), v)
            out[o.key] = v
        return out

    def save():
        clean, errors = prefs.validate(form())
        if errors:
            error.configure(text="\n".join(errors))
            return
        kw = {"set_autostart": set_autostart} if set_autostart is not None else {}
        prefs.apply(app, clean, **kw)
        win.destroy()

    ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right")
    ttk.Button(bar, text="Save", command=save).pack(side="right", padx=PAD)
    ttk.Button(bar, text="Open data folder", command=lambda: _open_data()).pack(side="right")
    win.bind("<Escape>", lambda e: win.destroy())
    win._widgets = {"tree": tree, "vars": variables, "save": save, "error": error, "fill": fill}
    return win


def _open_data():
    from .config import app_dir
    path = app_dir()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
