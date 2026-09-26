"""Declarative device support: a *recipe* describes a request/reply battery
exchange as data, so most headsets can be added without writing Python.

    {
      "name": "SteelSeries Arctis Nova 7",
      "kind": "headset",
      "vendor_id": "0x1038",
      "product_ids": ["0x2202"],
      "match": {"usage_page": "0xffc0", "interface": 3},      # optional filters
      "steps": [
        {"write": "00 b0", "pad_to": 0,                      # bytes to send
         "expect": "b0",   "expect_at": 0,                   # optional reply check
         "level":    {"byte": 2, "max": 4},                  # scaled to 0..100
         "charging": {"byte": 3, "in": [1]},
         "offline":  {"byte": 3, "in": [0]},
         "muted":    {"byte": 5, "mask": "0x01", "in": [1]}}
      ]
    }

Devices that *push* their state (a mute button, a periodic battery report)
get "listen" rules - the same shape as a step, without "write"::

      "listen": [{"expect": "0b 00 bb 08", "muted": {"byte": 4, "in": [1]}}]

A recipe with listen rules keeps the device open and applies every incoming
report to the rules (steps with an "expect" count as rules too, so their
replies are picked up the same way), so changes show up immediately.

Byte positions are counted in the reply exactly as hidapi returns it. Every
field is optional; a step without "expect" accepts the first reply. Bundled
recipes live in ``recipes.json`` next to this file, and extra ones can be put
in ``recipes.json`` in the settings folder - user recipes win on a clash.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .hidio import HidApi, hexdump
from .model import DEVICE, Reading

log = logging.getLogger("peribatt")

BUNDLED = Path(__file__).with_name("recipes.json")


def _int(v) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


def _bytes(v) -> bytes:
    if isinstance(v, list):
        return bytes(_int(x) for x in v)
    return bytes.fromhex(str(v).replace(" ", ""))


@dataclass
class Field:
    byte: int
    mask: int = 0xFF
    shift: int = 0
    values: Optional[List[int]] = None     # for booleans: true when the value is one of these
    min: int = 0
    max: int = 100

    @classmethod
    def parse(cls, spec) -> Optional["Field"]:
        if not spec:
            return None
        return cls(byte=_int(spec["byte"]), mask=_int(spec.get("mask", 0xFF)),
                   shift=_int(spec.get("shift", 0)),
                   values=[_int(x) for x in spec["in"]] if "in" in spec else None,
                   min=_int(spec.get("min", 0)), max=_int(spec.get("max", 100)))

    def raw(self, reply: Sequence[int]) -> Optional[int]:
        if self.byte >= len(reply):
            return None
        return (reply[self.byte] & self.mask) >> self.shift

    def flag(self, reply) -> Optional[bool]:
        v = self.raw(reply)
        if v is None:
            return None
        return v in self.values if self.values is not None else bool(v)

    def percent(self, reply) -> Optional[int]:
        v = self.raw(reply)
        if v is None or self.max <= self.min or not self.min <= v <= self.max:
            return None
        return round((v - self.min) * 100 / (self.max - self.min))


@dataclass
class Step:
    write: bytes
    expect: bytes = b""
    expect_at: int = 0
    feature: bool = False                  # use feature reports instead of output/input
    read_len: int = 64
    fields: Dict[str, Field] = field(default_factory=dict)

    @classmethod
    def parse(cls, spec) -> "Step":
        data = _bytes(spec.get("write", ""))
        pad = _int(spec.get("pad_to", 0))
        if pad > len(data):
            data += bytes(pad - len(data))
        fields = {k: Field.parse(spec.get(k)) for k in ("level", "charging", "offline", "muted")}
        return cls(write=data, expect=_bytes(spec.get("expect", "")),
                   expect_at=_int(spec.get("expect_at", 0)),
                   feature=bool(spec.get("feature", False)),
                   read_len=_int(spec.get("read_len", 64)),
                   fields={k: v for k, v in fields.items() if v})

    @property
    def min_len(self) -> int:
        return max((f.byte + 1 for f in self.fields.values()), default=1)

    def accepts(self, reply: Sequence[int]) -> bool:
        # Too short to hold every field means it is some other report.
        if not reply or len(reply) < self.min_len:
            return False
        e, at = self.expect, self.expect_at
        return not e or bytes(reply[at:at + len(e)]) == e


@dataclass
class Recipe:
    name: str
    vendor_id: int
    product_ids: List[int]
    steps: List[Step]
    kind: str = DEVICE
    match: Dict[str, int] = field(default_factory=dict)
    listen: List[Step] = field(default_factory=list)

    @classmethod
    def parse(cls, spec: dict) -> "Recipe":
        r = cls(name=spec["name"], vendor_id=_int(spec["vendor_id"]),
                product_ids=[_int(p) for p in spec["product_ids"]],
                steps=[Step.parse(s) for s in spec.get("steps", [])],
                kind=spec.get("kind", DEVICE),
                match={k: _int(v) for k, v in (spec.get("match") or {}).items()},
                listen=[Step.parse(s) for s in spec.get("listen", [])])
        if not r.steps and not r.listen:
            raise ValueError("a recipe needs steps or listen rules")
        return r

    @property
    def rules(self) -> List[Step]:
        """What incoming reports are matched against in listen mode."""
        return [s for s in self.steps if s.expect and s.fields] + self.listen

    def picks(self, info: dict) -> bool:
        keymap = {"usage_page": "usage_page", "usage": "usage", "interface": "interface_number"}
        return all(info.get(keymap.get(k, k)) == v for k, v in self.match.items())


def load(paths: Sequence[Path] = ()) -> List[Recipe]:
    """Bundled recipes plus any user files. A broken entry is logged and
    skipped rather than taking the others down with it."""
    by_key: Dict[tuple, Recipe] = {}
    for path in (BUNDLED, *paths):
        try:
            entries = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            log.warning("recipes %s: %s", path, e)
            continue
        for spec in entries if isinstance(entries, list) else entries.get("recipes", []):
            try:
                r = Recipe.parse(spec)
            except (KeyError, ValueError, TypeError) as e:
                log.warning("recipe %r skipped: %s", spec.get("name") if isinstance(spec, dict) else spec, e)
                continue
            for pid in r.product_ids:
                by_key[(r.vendor_id, pid)] = r
    # Each product id belongs to exactly one recipe (the last one loaded wins).
    owned: Dict[int, List[int]] = {}
    recipes: Dict[int, Recipe] = {}
    for (_, pid), r in by_key.items():
        owned.setdefault(id(r), []).append(pid)
        recipes[id(r)] = r
    out = []
    for rid, r in recipes.items():
        r.product_ids = [p for p in r.product_ids if p in owned[rid]]
        out.append(r)
    return out


def evaluate(step: Step, reply: Sequence[int]) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for name, f in step.fields.items():
        v = f.percent(reply) if name == "level" else f.flag(reply)
        if v is not None:
            out[name] = v
    return out


class Listener:
    """Keeps one listen-mode device open: a reader thread per collection feeds
    every report through the recipe's rules; :meth:`request` sends the
    recipe's queries, whose replies arrive through the same readers."""

    def __init__(self, recipe: Recipe, key: str, infos: List[dict], api,
                 on_change: Optional[Callable[[Reading], None]] = None, threaded: bool = True):
        self.recipe, self.key, self.infos, self.api = recipe, key, infos, api
        self.on_change = on_change
        self.threaded = threaded
        self.state: Dict[str, object] = {}
        self.handles: List[object] = []
        self.writer = None
        self.lost = False
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def open(self) -> bool:
        ranked = []                                   # (rank, handle): where queries go
        for d in self.infos:
            if d.get("usage_page") == 0x01:          # keyboard/mouse: owned by Windows
                continue
            try:
                h = self.api.open(d["path"])
            except OSError:
                continue
            self.handles.append(h)
            rank = 0 if self.recipe.match and self.recipe.picks(d) else \
                1 if d.get("usage_page", 0) >= 0xFF00 else 2
            ranked.append((rank, len(ranked), h))
            if self.threaded:
                threading.Thread(target=self._reader, args=(h,), daemon=True,
                                 name=f"recipe-{self.key}").start()
        self.writer = min(ranked)[2] if ranked else None
        return bool(self.handles)

    def close(self) -> None:
        self._stop.set()
        for h in self.handles:
            try:
                h.close()
            except Exception:
                pass
        self.handles, self.writer = [], None

    def _reader(self, handle) -> None:
        while not self._stop.is_set():
            try:
                report = handle.read(64, 5000)
            except (OSError, ValueError):
                if not self._stop.is_set():
                    self.lost = True
                return
            if report:
                self.feed(report)

    def feed(self, report: Sequence[int]) -> None:
        changes: Dict[str, object] = {}
        for rule in self.recipe.rules:
            if rule.accepts(report):
                changes.update(evaluate(rule, report))
        if not changes:
            return
        with self._lock:
            before = self.reading()
            self.state.update(changes)
            if changes.get("level") is not None and "offline" not in changes:
                self.state["offline"] = False
            after = self.reading()
        if after != before and self.on_change:
            self.on_change(after)

    def request(self) -> None:
        for step in self.recipe.steps:
            if not step.write or self.writer is None:
                continue
            try:
                if step.feature:
                    self.writer.send_feature_report(step.write)
                    self.feed(self.writer.get_feature_report(step.write[0], step.read_len))
                else:
                    self.writer.write(step.write)
            except (OSError, ValueError):
                self.lost = True

    def reading(self) -> Reading:
        st = self.state
        online = not st.get("offline") and not self.lost
        return Reading(self.key, self.recipe.name, self.recipe.kind, st.get("level"),
                       bool(st.get("charging")) and online, online=online,
                       muted=bool(st.get("muted")) and online,
                       note="" if online else "switched off")


class RecipeSource:
    name = "recipes"

    def __init__(self, recipes: Sequence[Recipe], api=None,
                 clock: Callable[[], float] = time.monotonic,
                 on_change: Optional[Callable[[Reading], None]] = None, threaded: bool = True):
        self.recipes = list(recipes)
        self.api = api or HidApi()
        self.clock = clock
        self.on_change = on_change
        self.threaded = threaded
        self.last: Dict[str, Reading] = {}
        self.listeners: Dict[str, Listener] = {}
        self.log: List[str] = []

    def set_recipes(self, recipes: Sequence[Recipe]) -> None:
        """Swap in a new recipe list (after the learn wizard saved one)."""
        self.close()
        self.recipes = list(recipes)

    def close(self) -> None:
        for lst in self.listeners.values():
            lst.close()
        self.listeners = {}

    def readings(self) -> List[Reading]:
        return [lst.reading() for lst in self.listeners.values() if lst.state]

    @property
    def claimed(self) -> set:
        return {(r.vendor_id, p) for r in self.recipes for p in r.product_ids}

    def _run(self, handle, step: Step, timeout: float = 1.0) -> Optional[List[int]]:
        if step.feature:
            handle.send_feature_report(step.write)
            reply = handle.get_feature_report(step.write[0], step.read_len)
            return list(reply) if step.accepts(reply) else None
        for _ in range(16):                      # drop stale input reports
            if not handle.read(step.read_len, 0):
                break
        handle.write(step.write)
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            reply = handle.read(step.read_len, 100)
            if step.accepts(reply):
                return list(reply)
        return None

    def _read(self, recipe: Recipe, info: dict) -> Optional[Reading]:
        key = f"hid-{recipe.vendor_id:04x}-{info['product_id']:04x}"
        state: Dict[str, object] = {}
        handle = self.api.open(info["path"])
        try:
            for step in recipe.steps:
                reply = self._run(handle, step)
                self.log.append(f"{recipe.name}: {hexdump(reply, 12)}")
                if reply is None:
                    continue
                state.update(evaluate(step, reply))
                if state.get("offline"):
                    break
        finally:
            handle.close()
        if not state:
            return None
        if state.get("offline"):
            prev = self.last.get(key)
            return Reading(key, recipe.name, recipe.kind, prev.level if prev else None,
                           online=False, note="switched off")
        r = Reading(key, recipe.name, recipe.kind, state.get("level"),
                    bool(state.get("charging")), muted=bool(state.get("muted")))
        self.last[key] = r
        return r

    def poll(self) -> List[Reading]:
        self.log = []
        out: List[Reading] = []
        by_vid: Dict[int, List[Recipe]] = {}
        for r in self.recipes:
            by_vid.setdefault(r.vendor_id, []).append(r)
        for vid, recipes in by_vid.items():
            infos = self.api.enumerate(vid)
            for recipe in recipes:
                for pid in recipe.product_ids:
                    if recipe.listen:
                        r = self._listen(recipe, pid, [d for d in infos if d.get("product_id") == pid])
                        if r:
                            out.append(r)
                        continue
                    candidates = [d for d in infos if d.get("product_id") == pid and recipe.picks(d)]
                    if not candidates:
                        continue
                    try:
                        r = self._read(recipe, candidates[0])
                    except (OSError, ValueError) as e:
                        self.log.append(f"{recipe.name}: {e}")
                        r = None
                    if r:
                        out.append(r)
                    break
        return out

    def _listen(self, recipe: Recipe, pid: int, infos: List[dict]) -> Optional[Reading]:
        key = f"hid-{recipe.vendor_id:04x}-{pid:04x}"
        lst = self.listeners.get(key)
        if lst is not None and (lst.lost or not infos):
            lst.close()
            self.listeners.pop(key)
            lst = None
        if lst is None and infos:
            lst = Listener(recipe, key, infos, self.api, self.on_change, self.threaded)
            if lst.open():
                self.listeners[key] = lst
            else:
                lst = None
        if lst is None:
            prev = self.last.get(key)
            return prev.with_(online=False, charging=False, muted=False,
                              note="not connected") if prev else None
        lst.request()
        if not lst.state:
            return None
        r = lst.reading()
        self.last[key] = r
        return r
