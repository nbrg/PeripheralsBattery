"""Teach the app an unsupported device, without writing code.

The wizard listens to every HID collection of the chosen device while the
user does a few things - nothing, then mute/unmute, then plug/unplug the
charger - and optionally sends the read-only battery queries that other
brands use. The analysis below then looks for:

* a byte that matches the battery percentage the user typed in (as a
  percentage, as 0-255, or as a 0-4 / 0-10 step scale);
* a byte that flips between the "muted" and "unmuted" phases;
* a byte that flips between the "plugged in" and "unplugged" phases;

and the result is written as an ordinary recipe (see ``recipes.py``), so it can
be shared with friends as a few lines of JSON.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .hidio import hexdump

# Read-only battery queries known from other device families: (label, bytes, pad_to).
KNOWN_QUERIES: Tuple[Tuple[str, str, int], ...] = (
    ("HyperX, Kingston dongle", "06 00 02 00 9a 00 00 68 4a 8e 0a 00 00 00 bb 02", 62),
    ("HyperX, HP dongle", "06 ff bb 02", 52),
    ("HyperX Alpha family", "21 bb 0b", 31),
    ("SteelSeries Nova", "00 b0", 0),
    ("SteelSeries Arctis", "06 18", 0),
    ("SteelSeries Arctis 1", "06 12", 0),
    ("Corsair", "c9 64", 0),
)

MAX_PREFIX = 4


@dataclass(frozen=True)
class Report:
    t: float
    collection: int
    data: Tuple[int, ...]
    phase: str
    query: Optional[int] = None        # index into KNOWN_QUERIES if sent just before


@dataclass
class Candidate:
    """One way to read a value: in reports of ``collection`` starting with
    ``prefix``, byte ``byte`` holds it."""
    collection: int
    prefix: Tuple[int, ...]
    byte: int
    spec: dict                          # the recipe field, e.g. {"byte": 7} or {"byte": 3, "in": [1]}
    score: float
    support: int = 1
    query: Optional[int] = None

    def rule(self, name: str) -> dict:
        return {"expect": " ".join(f"{b:02x}" for b in self.prefix), name: dict(self.spec)}


# --- capture ----------------------------------------------------------------

class Capture:
    """Opens every usable collection of one device and records what it sends."""

    def __init__(self, api, infos: Sequence[dict], clock: Callable[[], float] = time.monotonic,
                 threaded: bool = True):
        self.api = api
        self.infos = [d for d in infos if d.get("usage_page") != 0x01]
        self.clock = clock
        self.threaded = threaded
        self.reports: List[Report] = []
        self.phase = "idle"
        self.handles: List[object] = []
        self._query: Optional[int] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def open(self) -> int:
        for i, d in enumerate(self.infos):
            try:
                h = self.api.open(d["path"])
            except OSError:
                h = None
            self.handles.append(h)
            if h is not None and self.threaded:
                threading.Thread(target=self._reader, args=(i, h), daemon=True,
                                 name=f"learn-{i}").start()
        return sum(h is not None for h in self.handles)

    def close(self) -> None:
        self._stop.set()
        if not self.threaded:
            for h in self.handles:
                if h is not None:
                    try:
                        h.close()
                    except Exception:
                        pass
        # Threaded: readers close their own handles (never under a blocked read).

    def _reader(self, i: int, h) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = h.read(64, 250)
                except (OSError, ValueError):
                    return
                if data and not self._stop.is_set():
                    self.feed(i, data)
        finally:
            try:
                h.close()
            except Exception:
                pass

    def feed(self, collection: int, data: Sequence[int]) -> None:
        with self._lock:
            self.reports.append(Report(self.clock(), collection, tuple(data), self.phase, self._query))

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def send_known_queries(self, pause: Callable[[float], None] = time.sleep) -> None:
        """Sends each known read-only query to each vendor collection."""
        for qi, (_, hexbytes, pad) in enumerate(KNOWN_QUERIES):
            data = bytes.fromhex(hexbytes.replace(" ", ""))
            data += bytes(max(0, pad - len(data)))
            for i, d in enumerate(self.infos):
                h = self.handles[i] if i < len(self.handles) else None
                if h is None or d.get("usage_page", 0) < 0xFF00:
                    continue
                self._query = qi
                try:
                    h.write(data)
                except (OSError, ValueError):
                    pass
                pause(0.25)
        self._query = None

    def in_phase(self, *phases: str) -> List[Report]:
        with self._lock:
            return [r for r in self.reports if r.phase in phases]


# --- analysis ---------------------------------------------------------------

def _scales(value: int, level: int, tol: int) -> List[Tuple[dict, float]]:
    """Ways ``value`` could encode ``level``, with a confidence weight."""
    out = []
    if value <= 100 and abs(value - level) <= tol:
        out.append(({}, 1.0))
    if abs(round(value * 100 / 255) - level) <= tol and value > 4:
        out.append(({"max": 255}, 0.6))
    if value <= 4 and abs(value * 25 - level) <= 13:
        out.append(({"max": 4}, 0.3))
    if value <= 10 and abs(value * 10 - level) <= 6:
        out.append(({"max": 10}, 0.3))
    return out


def level_candidates(reports: Sequence[Report], level: int, tol: int = 2) -> List[Candidate]:
    """Byte positions that hold the battery level the user read off the device."""
    found: Dict[tuple, Candidate] = {}
    contradict: Dict[tuple, int] = defaultdict(int)
    for r in reports:
        d = r.data
        for i in range(1, len(d)):
            prefix = d[:min(i, MAX_PREFIX)]
            for extra, weight in _scales(d[i], level, tol):
                key = (r.collection, len(d), prefix, i, tuple(sorted(extra.items())))
                c = found.get(key)
                if c is None:
                    spec = {"byte": i, **extra}
                    found[key] = Candidate(r.collection, prefix, i, spec, weight, 1, r.query)
                else:
                    c.support += 1
                    c.score += weight
    # A position that also shows unrelated values in the same kind of report is noise.
    for r in reports:
        for key, c in found.items():
            if key[0] == r.collection and key[1] == len(r.data) and r.data[:len(c.prefix)] == c.prefix:
                if not _scales(r.data[c.byte], level, tol + 3):
                    contradict[key] += 1
    out = []
    for key, c in found.items():
        c.score = c.score - 2 * contradict[key] + 0.25 * len(c.prefix)
        if c.query is not None:
            c.score += 1.0                      # answered a battery query: very likely
        if c.score > 0:
            out.append(c)
    # Values that happen to equal the level in padding/zero-heavy reports are common:
    # prefer longer prefixes and more support.
    return sorted(out, key=lambda c: (-c.score, -len(c.prefix), c.byte))


def toggle_candidates(off: Sequence[Report], on: Sequence[Report]) -> List[Candidate]:
    """Byte positions that hold one value in every "off" report and another in
    every "on" report of the same kind (e.g. unmuted -> muted)."""
    groups: Dict[tuple, Tuple[List[Report], List[Report]]] = defaultdict(lambda: ([], []))
    for side, reps in ((0, off), (1, on)):
        for r in reps:
            groups[(r.collection, len(r.data), r.data[0] if r.data else None)][side].append(r)
    out = []
    for (coll, length, _), (a, b) in groups.items():
        if not a or not b:
            continue
        for i in range(1, length):
            va = {r.data[i] for r in a}
            vb = {r.data[i] for r in b}
            if va & vb or len(va) > 1 or len(vb) > 1:
                continue
            everything = a + b
            prefix = _common_prefix([r.data for r in everything], i)
            score = 1.0 + 0.3 * len(prefix) + 0.2 * min(len(a), len(b))
            if max(va | vb) <= 2:
                score += 1.0                    # flags are usually 0/1/2
            out.append(Candidate(coll, prefix, i, {"byte": i, "in": sorted(vb)}, score,
                                 len(everything)))
    return sorted(out, key=lambda c: (-c.score, c.byte))


def _common_prefix(datas: Sequence[Tuple[int, ...]], limit: int) -> Tuple[int, ...]:
    n = min(limit, MAX_PREFIX, *(len(d) for d in datas))
    k = 0
    while k < n and all(d[k] == datas[0][k] for d in datas):
        k += 1
    return datas[0][:k]


# --- result -----------------------------------------------------------------

@dataclass
class Findings:
    level: Optional[Candidate] = None
    muted: Optional[Candidate] = None
    charging: Optional[Candidate] = None
    notes: List[str] = field(default_factory=list)


def build_recipe(name: str, kind: str, vendor_id: int, product_id: int,
                 findings: Findings) -> dict:
    """An ordinary recipe dict: queries become steps, everything else listen rules."""
    recipe: dict = {"name": name, "kind": kind, "vendor_id": f"0x{vendor_id:04x}",
                    "product_ids": [f"0x{product_id:04x}"], "learned": True, "listen": []}
    steps = []
    lvl = findings.level
    if lvl is not None and lvl.query is not None:
        _, hexbytes, pad = KNOWN_QUERIES[lvl.query]
        step = {"write": hexbytes, **lvl.rule("level")}
        if pad:
            step["pad_to"] = pad
        steps.append(step)
    elif lvl is not None:
        recipe["listen"].append(lvl.rule("level"))
    for name_, cand in (("muted", findings.muted), ("charging", findings.charging)):
        if cand is not None:
            recipe["listen"].append(cand.rule(name_))
    if steps:
        recipe["steps"] = steps
    if not recipe["listen"]:
        del recipe["listen"]
    return recipe


def save_recipe(recipe: dict, path: Path) -> None:
    """Adds (or replaces, by vendor/product id) a recipe in the user's file."""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            entries = entries.get("recipes", [])
    except (FileNotFoundError, ValueError):
        entries = []
    ids = {(recipe["vendor_id"].lower(), p.lower()) for p in recipe["product_ids"]}
    entries = [e for e in entries if not any(
        (str(e.get("vendor_id", "")).lower(), str(p).lower()) in ids for p in e.get("product_ids", []))]
    entries.append(recipe)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def describe(c: Optional[Candidate]) -> str:
    if c is None:
        return "not found"
    scale = {255: " (0-255 scale)", 4: " (5 steps)", 10: " (10 steps)"}.get(c.spec.get("max"), "")
    return f"byte {c.byte} of reports starting {hexdump(c.prefix) or '(any)'}{scale}"


def device_list(api, skip: Callable[[int, int], bool] = lambda v, p: False) -> List[dict]:
    """One entry per USB device that has a collection we could read."""
    devices: Dict[Tuple[int, int], dict] = {}
    for d in api.enumerate(0):
        vid, pid = d.get("vendor_id", 0), d.get("product_id", 0)
        if not vid or d.get("usage_page") == 0x01 and d.get("usage") in (0x02, 0x06):
            continue
        entry = devices.setdefault((vid, pid), {
            "vendor_id": vid, "product_id": pid, "infos": [],
            "name": (d.get("product_string") or "").strip() or f"Device {vid:04x}:{pid:04x}",
            "maker": (d.get("manufacturer_string") or "").strip(),
            "supported": skip(vid, pid)})
        entry["infos"].append(d)
    return sorted(devices.values(), key=lambda e: (e["supported"], e["name"].lower()))
