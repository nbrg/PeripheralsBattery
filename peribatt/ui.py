"""A Tk thread that exists only while one of our windows is open.

Tk must be driven from a single thread, and the tray callbacks arrive on
pystray's threads, so windows are requested through a queue. When the last
window closes the Tk interpreter is torn down and the thread ends: an idle app
pays nothing for having a settings window.
"""
from __future__ import annotations

import gc
import logging
import queue
import threading
from typing import Callable, Dict

log = logging.getLogger("peribatt")

PUMP_MS = 150


class UiThread:
    def __init__(self):
        self._q: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None
        self.windows: Dict[str, object] = {}

    def show(self, name: str, factory: Callable[[object], object]) -> None:
        """Opens window ``name`` built by ``factory(root)``, or raises it if open."""
        with self._lock:
            self._q.put((name, factory))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="ui", daemon=True)
                self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None

    def _run(self) -> None:
        import tkinter as tk
        while True:
            root = tk.Tk()
            root.withdraw()
            _style(root)
            self._root = root
            root.after(0, self._pump)
            root.mainloop()
            self.windows.clear()
            try:
                root.destroy()
            except tk.TclError:
                pass
            # Tk objects must be finalised by the thread that made them, or Tcl
            # aborts the whole process: drop every reference and collect here.
            self._root = root = None
            gc.collect()
            with self._lock:
                if self._q.empty():
                    self._thread = None
                    return

    def _pump(self) -> None:
        root = self._root
        while True:
            try:
                name, factory = self._q.get_nowait()
            except queue.Empty:
                break
            win = self.windows.get(name)
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                continue
            try:
                win = factory(root)
            except Exception:
                log.exception("could not open %s", name)
                continue
            self.windows[name] = win
        self.windows = {n: w for n, w in self.windows.items() if w.winfo_exists()}
        with self._lock:
            if not self.windows and self._q.empty():
                root.quit()
                return
        root.after(PUMP_MS, self._pump)


def _style(root) -> None:
    from tkinter import ttk
    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    style.configure("Hint.TLabel", foreground="#666666")
    style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
    style.configure("Found.TLabel", foreground="#1e8449")
    style.configure("Missing.TLabel", foreground="#a04000")


def window_icon(win, kind: str = "headset") -> None:
    """Uses our own pictogram as the window icon."""
    try:
        from PIL import ImageTk

        from .render import render
        from .style import GREEN
        img = ImageTk.PhotoImage(render(32, kind, 70, GREEN), master=win)
        win.iconphoto(False, img)
        win._icon_ref = img                      # keep a reference, or Tk drops it
    except Exception as e:                      # cosmetic only
        log.debug("window icon: %s", e)
