"""The "Learn a new device" wizard (tkinter). The analysis is in ``learn.py``.

Pages: pick the device -> battery -> mic mute -> charging -> name and save.
Every timed step is driven by ``after`` callbacks, so the window never freezes.
"""
from __future__ import annotations

import json
import threading
from typing import Callable, List, Optional, Sequence, Tuple

from . import DISPLAY_NAME, learn
from .hidio import HidApi
from .model import DEVICE, HEADSET, KEYBOARD, MOUSE
from .ui import window_icon

PAD = 10
KINDS = (HEADSET, MOUSE, KEYBOARD, "gamepad", DEVICE)
BATTERY_SECONDS = 15


class Wizard:
    def __init__(self, root, api=None, recipes_path=None,
                 is_supported: Callable[[int, int], bool] = lambda v, p: False,
                 on_saved: Optional[Callable[[dict], None]] = None,
                 tick_ms: int = 1000):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.api = api or HidApi()
        self.recipes_path = recipes_path
        self.is_supported = is_supported
        self.on_saved = on_saved
        self.tick_ms = tick_ms                  # one countdown second (tests shorten it)
        self.device: Optional[dict] = None
        self.capture: Optional[learn.Capture] = None
        self.findings = learn.Findings()
        self.recipe: Optional[dict] = None

        self.win = win = tk.Toplevel(root)
        win.title(f"{DISPLAY_NAME} - Learn a new device")
        win.minsize(560, 420)
        window_icon(win)
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.body = ttk.Frame(win, padding=PAD)
        self.body.pack(fill="both", expand=True)
        self.nav = ttk.Frame(win, padding=(PAD, 0, PAD, PAD))
        self.nav.pack(fill="x")
        self.page_pick()

    # -- helpers ---------------------------------------------------------------
    def clear(self, title: str, text: str = ""):
        for w in list(self.body.winfo_children()) + list(self.nav.winfo_children()):
            w.destroy()
        self.ttk.Label(self.body, text=title, style="Title.TLabel").pack(anchor="w")
        if text:
            self.ttk.Label(self.body, text=text, wraplength=520, justify="left").pack(
                anchor="w", pady=(4, PAD))

    def button(self, text: str, command, side="right", **kw):
        b = self.ttk.Button(self.nav, text=text, command=command, **kw)
        b.pack(side=side, padx=(PAD // 2, 0))
        return b

    def close(self):
        if self.capture:
            self.capture.close()
        self.win.destroy()

    def timed(self, steps: Sequence[Tuple[str, str, int]], status, bar, done: Callable[[], None]):
        """Runs (phase, instruction, seconds) steps one after another."""
        total = sum(s for _, _, s in steps) or 1
        state = {"i": 0, "left": steps[0][2] if steps else 0, "elapsed": 0}

        def tick():
            if not self.win.winfo_exists():
                return
            i = state["i"]
            if i >= len(steps):
                bar["value"] = 100
                done()
                return
            phase, text, _ = steps[i]
            self.capture.set_phase(phase)
            status.configure(text=f"{text}  ({state['left']} s)")
            bar["value"] = 100 * state["elapsed"] / total
            if state["left"] <= 0:
                state["i"] += 1
                if state["i"] < len(steps):
                    state["left"] = steps[state["i"]][2]
                self.win.after(0, tick)
                return
            state["left"] -= 1
            state["elapsed"] += 1
            self.win.after(self.tick_ms, tick)
        tick()

    # -- page 1: pick ----------------------------------------------------------
    def page_pick(self):
        ttk = self.ttk
        self.clear("Which device?",
                   "Plug in the device's USB dongle (or cable) and switch the device on. "
                   "Devices this app already reads are hidden unless you tick the box.")
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=("name", "maker", "id"), show="headings", height=9,
                            selectmode="browse")
        for col, title, width in (("name", "Device", 250), ("maker", "Maker", 150), ("id", "ID", 90)):
            tree.heading(col, text=title)
            tree.column(col, width=width)
        tree.pack(fill="both", expand=True)
        show_all = self.tk.BooleanVar(value=False)
        devices: List[dict] = []

        def fill():
            tree.delete(*tree.get_children())
            devices[:] = [d for d in learn.device_list(self.api, self.is_supported)
                          if show_all.get() or not d["supported"]]
            for i, d in enumerate(devices):
                tree.insert("", "end", iid=str(i), values=(
                    d["name"] + ("  (supported)" if d["supported"] else ""), d["maker"],
                    f"{d['vendor_id']:04x}:{d['product_id']:04x}"))
        ttk.Checkbutton(self.body, text="Show devices that are already supported",
                        variable=show_all, command=fill).pack(anchor="w", pady=(4, 0))
        fill()

        def next_():
            sel = tree.selection()
            if not sel:
                return
            self.device = devices[int(sel[0])]
            self.capture = learn.Capture(self.api, self.device["infos"])
            if not self.capture.open():
                self.status_error("Could not open this device. Close its vendor app and try again.")
                return
            self.page_battery()
        self.button("Next", next_)
        self.button("Refresh list", fill, side="left")
        self.tree = tree

    def status_error(self, text):
        self.ttk.Label(self.body, text=text, style="Missing.TLabel").pack(anchor="w")

    # -- page 2: battery ---------------------------------------------------------
    def page_battery(self):
        ttk, tk = self.ttk, self.tk
        self.clear("Battery level",
                   "Type the battery level the device reports right now - from its vendor app, a "
                   "voice prompt or its LEDs. Then press Listen and leave the device alone.")
        row = ttk.Frame(self.body)
        row.pack(anchor="w")
        level = tk.StringVar()
        ttk.Label(row, text="Battery now (%):").pack(side="left")
        ttk.Entry(row, textvariable=level, width=6).pack(side="left", padx=PAD)
        probe = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.body, variable=probe,
                        text="Also ask the device the battery questions other brands understand "
                             "(read-only requests)").pack(anchor="w", pady=4)
        bar = ttk.Progressbar(self.body, maximum=100)
        bar.pack(fill="x", pady=(PAD, 4))
        status = ttk.Label(self.body, text="")
        status.pack(anchor="w")
        result = ttk.Label(self.body, text="", wraplength=520)
        result.pack(anchor="w", pady=PAD)

        def finish():
            try:
                pct = int(level.get().strip().rstrip("%"))
            except ValueError:
                pct = None
            reports = self.capture.in_phase("battery")
            if pct is None:
                result.configure(text=f"Heard {len(reports)} reports. Without a known level the "
                                      "battery byte can't be picked out - you can still teach mute "
                                      "and charging.", style="Missing.TLabel")
            else:
                cands = learn.level_candidates(reports, pct)
                self.findings.level = cands[0] if cands else None
                style = "Found.TLabel" if cands else "Missing.TLabel"
                result.configure(text=f"Battery: {learn.describe(self.findings.level)}"
                                      f"  - heard {len(reports)} reports.", style=style)
            go.configure(state="normal")

        def listen():
            listen_btn.configure(state="disabled")
            self.capture.set_phase("battery")
            if probe.get():
                threading.Thread(target=self.capture.send_known_queries, daemon=True).start()
            self.timed([("battery", "Listening…", BATTERY_SECONDS)], status, bar, finish)

        listen_btn = ttk.Button(self.body, text="Listen", command=listen)
        listen_btn.pack(anchor="w")
        go = self.button("Next", self.page_mute)
        self.button("Skip", self.page_mute, side="left")

    # -- page 3: mute ---------------------------------------------------------------
    def page_mute(self):
        ttk = self.ttk
        self.clear("Microphone mute (headsets)",
                   "Press Start, then follow the instructions: the mic has to be unmuted, "
                   "muted, and unmuted again. Skip this for devices without a mic.")
        bar = ttk.Progressbar(self.body, maximum=100)
        bar.pack(fill="x", pady=(PAD, 4))
        status = ttk.Label(self.body, text="", style="Title.TLabel")
        status.pack(anchor="w")
        result = ttk.Label(self.body, text="", wraplength=520)
        result.pack(anchor="w", pady=PAD)

        def finish():
            cands = learn.toggle_candidates(self.capture.in_phase("unmuted", "unmuted2"),
                                            self.capture.in_phase("muted"))
            self.findings.muted = cands[0] if cands else None
            result.configure(text=f"Mute: {learn.describe(self.findings.muted)}",
                             style="Found.TLabel" if cands else "Missing.TLabel")
            go.configure(state="normal")

        def start():
            start_btn.configure(state="disabled")
            self.timed([("unmuted", "Make sure the mic is UNMUTED", 5),
                        ("muted", "Now MUTE the mic", 6),
                        ("unmuted2", "Now UNMUTE it again", 6)], status, bar, finish)

        start_btn = ttk.Button(self.body, text="Start", command=start)
        start_btn.pack(anchor="w")
        go = self.button("Next", self.page_charging)
        self.button("Skip", self.page_charging, side="left")

    # -- page 4: charging -------------------------------------------------------------
    def page_charging(self):
        ttk = self.ttk
        self.clear("Charging",
                   "Press Start, then unplug and plug in the charging cable when asked. "
                   "Skip this if the device charges on the same cable as its data.")
        bar = ttk.Progressbar(self.body, maximum=100)
        bar.pack(fill="x", pady=(PAD, 4))
        status = ttk.Label(self.body, text="", style="Title.TLabel")
        status.pack(anchor="w")
        result = ttk.Label(self.body, text="", wraplength=520)
        result.pack(anchor="w", pady=PAD)

        def finish():
            cands = learn.toggle_candidates(self.capture.in_phase("unplugged"),
                                            self.capture.in_phase("plugged"))
            self.findings.charging = cands[0] if cands else None
            result.configure(text=f"Charging: {learn.describe(self.findings.charging)}",
                             style="Found.TLabel" if cands else "Missing.TLabel")
            go.configure(state="normal")

        def start():
            start_btn.configure(state="disabled")
            self.timed([("unplugged", "UNPLUG the charging cable", 8),
                        ("plugged", "Now PLUG IT IN", 10)], status, bar, finish)

        start_btn = ttk.Button(self.body, text="Start", command=start)
        start_btn.pack(anchor="w")
        go = self.button("Next", self.page_save)
        self.button("Skip", self.page_save, side="left")

    # -- page 5: save ---------------------------------------------------------------
    def page_save(self):
        ttk, tk = self.ttk, self.tk
        f = self.findings
        if not (f.level or f.muted or f.charging):
            self.clear("Nothing recognised",
                       "None of the steps found a usable signal. This device may need its vendor "
                       "app's own commands. Run the app with --probe and share the report in an "
                       "issue to get it supported.")
            self.button("Close", self.close)
            self.button("Start over", self.page_pick, side="left")
            return
        self.clear("Name and save",
                   "This becomes a recipe in your settings folder. Use Copy to share it: a friend "
                   "with the same device can paste it into their recipes.json.")
        grid = ttk.Frame(self.body)
        grid.pack(anchor="w", fill="x")
        name = tk.StringVar(value=self.device["name"])
        kind = tk.StringVar(value=HEADSET if f.muted else DEVICE)
        ttk.Label(grid, text="Name").grid(row=0, column=0, sticky="w")
        ttk.Entry(grid, textvariable=name, width=40).grid(row=0, column=1, sticky="w", padx=PAD)
        ttk.Label(grid, text="Type").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=kind, values=KINDS, state="readonly", width=12).grid(
            row=1, column=1, sticky="w", padx=PAD)
        text = tk.Text(self.body, height=10, width=70, font=("Consolas", 9))
        text.pack(fill="both", expand=True, pady=PAD)

        def refresh(*_):
            self.recipe = learn.build_recipe(name.get().strip() or self.device["name"], kind.get(),
                                             self.device["vendor_id"], self.device["product_id"], f)
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", json.dumps(self.recipe, indent=2))
            text.configure(state="disabled")
        name.trace_add("write", refresh)
        kind.trace_add("write", refresh)
        refresh()

        def copy():
            self.win.clipboard_clear()
            self.win.clipboard_append(json.dumps(self.recipe, indent=2))

        def save():
            learn.save_recipe(self.recipe, self.recipes_path)
            if self.on_saved:
                self.on_saved(self.recipe)
            self.close()
        self.button("Save", save)
        self.button("Copy", copy, side="left")
        self.saved_text = text


def open_learn(root, **kwargs):
    return Wizard(root, **kwargs).win
