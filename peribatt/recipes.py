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
field is optional; a step without "expect" accepts the first reply.

More step options, for protocols that need them:

* ``"expect": ["12 f9 ac ad", "12 f9 c8 c7"]`` - any of several echoes;
  ``"anchor": true`` finds the echo anywhere in the reply and counts the fields
  from it (a report id that may or may not be in front, a rolling buffer).
* ``"require": [{"byte": 15, "in": [1, 2, 8]}]`` - flags an answer must have,
  for replies without an echo.
* ``"send": "output" | "feature" | "none"`` and ``"receive": "input" |
  "feature" | "none"`` - a feature request with an input reply (Keychron), a
  step that only reads what the device streams (PlayStation), or a handshake
  that gets no reply (Corsair).
* ``"checksum": {"at": 16, "over": [0, 16], "base": "0x55"}`` signs the request,
  ``"verify_checksum": true`` checks the reply (Pulsar / ATK).
* ``"tries"``, ``"delay_ms"``, ``"timeout_ms"``, and ``"if_missing": "level"`` to
  run a fallback step only when an earlier one found nothing.

Field options: ``"bytes": 2`` (+ ``"order": "big"``) for 16-bit values,
``"xor"`` for inverted payloads, ``"steps"``/``"steps_from"`` for coarse step
scales, ``"clamp"`` to clip instead of refusing an out-of-range value.

Recipe options: ``"names"`` (a name per product id), ``"product_ids": "any"``
(every product of the vendor), ``"match": {"bluetooth": true}``, list values in
``match`` (any of them), and ``"off_when_named"`` (the product string says the
headset is off - Audeze's dongle renames itself). Every collection that matches
is tried until one answers, and the one that did is remembered. Bundled
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
    width: int = 1                         # 2: a 16-bit value (Corsair's 0..1000)
    big_endian: bool = False
    xor: int = 0                           # MCHOSE sends every byte inverted
    steps: int = 0                         # raw <= steps: a coarse step, above it a percent
    steps_from: int = 0                    # the value of the lowest step
    clamp: bool = False                    # out-of-range values are clipped, not refused

    @classmethod
    def parse(cls, spec) -> Optional["Field"]:
        if not spec:
            return None
        width = _int(spec.get("bytes", 1))
        return cls(byte=_int(spec["byte"]), mask=_int(spec.get("mask", 0xFFFF if width == 2 else 0xFF)),
                   shift=_int(spec.get("shift", 0)),
                   values=[_int(x) for x in spec["in"]] if "in" in spec else None,
                   min=_int(spec.get("min", 0)), max=_int(spec.get("max", 100)),
                   width=width, big_endian=spec.get("order") == "big",
                   xor=_int(spec.get("xor", 0)), steps=_int(spec.get("steps", 0)),
                   steps_from=_int(spec.get("steps_from", 0)), clamp=bool(spec.get("clamp", False)))

    def raw(self, reply: Sequence[int], base: int = 0) -> Optional[int]:
        at = base + self.byte
        if at < 0 or at + self.width > len(reply):
            return None
        data = bytes((b ^ self.xor) & 0xFF for b in reply[at:at + self.width])
        v = int.from_bytes(data, "big" if self.big_endian else "little")
        return (v & self.mask) >> self.shift

    def flag(self, reply, base: int = 0) -> Optional[bool]:
        v = self.raw(reply, base)
        if v is None:
            return None
        return v in self.values if self.values is not None else bool(v)

    def percent(self, reply, base: int = 0) -> Optional[int]:
        v = self.raw(reply, base)
        if v is None:
            return None
        if self.steps:
            # e.g. SteelSeries Aerox: 1..21 is a step on a 21-step scale, above that a percent
            if v > self.steps:
                return min(v, 100)
            return round((v - self.steps_from) * 100 / (self.steps - self.steps_from)) \
                if v >= self.steps_from else None
        if self.clamp:
            v = max(self.min, min(self.max, v))
        if self.max <= self.min or not self.min <= v <= self.max:
            return None
        return round((v - self.min) * 100 / (self.max - self.min))


@dataclass
class Checksum:
    """One byte computed over a range of the frame, e.g. Pulsar/ATK's
    "0x55 minus the sum of bytes 0..15" in byte 16."""
    at: int
    start: int = 0
    stop: int = 0
    base: int = 0
    op: str = "sub"                         # sub: base - sum; xor: base ^ xor of all bytes

    @classmethod
    def parse(cls, spec) -> Optional["Checksum"]:
        if not spec:
            return None
        at = _int(spec["at"])
        rng = spec.get("over", [0, at])
        return cls(at=at, start=_int(rng[0]), stop=_int(rng[1]), base=_int(spec.get("base", 0)),
                   op=spec.get("op", "sub"))

    def value(self, frame: Sequence[int]) -> int:
        part = frame[self.start:self.stop]
        if self.op == "xor":
            v = self.base
            for b in part:
                v ^= b
            return v & 0xFF
        return (self.base - sum(part)) & 0xFF

    def sign(self, frame: bytes) -> bytes:
        out = bytearray(frame)
        if len(out) <= self.at:
            out += bytes(self.at + 1 - len(out))
        out[self.at] = self.value(out)
        return bytes(out)

    def ok(self, frame: Sequence[int], base: int = 0) -> bool:
        f = list(frame[base:])
        return len(f) > self.at and f[self.at] == self.value(f)


SEND = ("output", "feature", "none")
RECEIVE = ("input", "feature", "none")


@dataclass
class Step:
    write: bytes
    expect: bytes = b""
    expect_at: int = 0
    feature: bool = False                  # use feature reports instead of output/input
    read_len: int = 64
    fields: Dict[str, Field] = field(default_factory=dict)
    alternatives: List[bytes] = field(default_factory=list)   # other accepted "expect" values
    anchor: bool = False                   # "expect" may sit anywhere; fields count from it
    send: str = "output"                   # output / feature / none (just read what the device pushes)
    receive: str = "input"                 # input / feature / none (fire and forget)
    tries: int = 1                         # write and read again until an answer is accepted
    delay_ms: int = 0                      # pause between writing and reading
    timeout_ms: int = 1000
    if_missing: str = ""                   # only run while this field is still unknown
    checksum: Optional[Checksum] = None    # signs the request...
    verify: bool = False                   # ...and, when set, checks the reply too
    require: List[Field] = field(default_factory=list)   # flags an answer must have (no echo)

    @classmethod
    def parse(cls, spec) -> "Step":
        data = _bytes(spec.get("write", ""))
        pad = _int(spec.get("pad_to", 0))
        if pad > len(data):
            data += bytes(pad - len(data))
        checksum = Checksum.parse(spec.get("checksum"))
        if checksum and data:
            data = checksum.sign(data)
        fields = {k: Field.parse(spec.get(k)) for k in ("level", "charging", "offline", "muted")}
        expects = spec.get("expect", "")
        expects = expects if isinstance(expects, list) else [expects]
        feature = bool(spec.get("feature", False))
        send = spec.get("send", "feature" if feature else ("output" if data else "none"))
        receive = spec.get("receive", "feature" if feature else "input")
        if send not in SEND or receive not in RECEIVE:
            raise ValueError(f"send must be one of {SEND}, receive one of {RECEIVE}")
        return cls(write=data, expect=_bytes(expects[0]) if expects else b"",
                   alternatives=[_bytes(e) for e in expects[1:]],
                   expect_at=_int(spec.get("expect_at", 0)), feature=feature,
                   read_len=_int(spec.get("read_len", 64)),
                   fields={k: v for k, v in fields.items() if v},
                   anchor=bool(spec.get("anchor", False)), send=send, receive=receive,
                   tries=max(1, _int(spec.get("tries", 1))), delay_ms=_int(spec.get("delay_ms", 0)),
                   timeout_ms=_int(spec.get("timeout_ms", 1000)), if_missing=spec.get("if_missing", ""),
                   checksum=checksum, verify=bool(spec.get("verify_checksum", False)),
                   require=[Field.parse(r) for r in (spec.get("require") or [])])

    @property
    def min_len(self) -> int:
        return max((f.byte + f.width for f in [*self.fields.values(), *self.require]), default=1)

    def locate(self, reply: Sequence[int]) -> Optional[int]:
        """Where the fields of an accepted reply are counted from, or None when the
        reply is not the answer (wrong echo, too short, bad checksum)."""
        if not reply:
            return None
        r = bytes(reply)
        expects = [e for e in [self.expect, *self.alternatives] if e]
        if not expects:
            base = 0
        elif self.anchor:
            hits = [i for i in (r.find(e) for e in expects) if i >= 0]
            if not hits:
                return None
            base = min(hits)
        elif any(r[self.expect_at:self.expect_at + len(e)] == e for e in expects):
            base = 0
        else:
            return None
        if len(r) < base + self.min_len:              # too short to hold every field
            return None
        if self.verify and self.checksum and not self.checksum.ok(r, 0 if not self.anchor else base):
            return None
        if not all(f.flag(r, base) for f in self.require):
            return None
        return base

    def accepts(self, reply: Sequence[int]) -> bool:
        return self.locate(reply) is not None


@dataclass
class Recipe:
    name: str
    vendor_id: int
    product_ids: List[int]
    steps: List[Step]
    kind: str = DEVICE
    match: Dict[str, object] = field(default_factory=dict)
    listen: List[Step] = field(default_factory=list)
    names: Dict[int, str] = field(default_factory=dict)   # product id -> its own name
    any_product: bool = False              # every product id of the vendor (MCHOSE)
    source: str = ""                       # where the protocol comes from (shown in diagnostics)
    off_when_named: List[str] = field(default_factory=list)   # product string says "no headset"

    @classmethod
    def parse(cls, spec: dict) -> "Recipe":
        pids = spec["product_ids"]
        any_product = pids == "any"
        match = {}
        for k, v in (spec.get("match") or {}).items():
            if k == "bluetooth":
                match[k] = bool(v)
            else:
                match[k] = [_int(x) for x in v] if isinstance(v, list) else _int(v)
        r = cls(name=spec["name"], vendor_id=_int(spec["vendor_id"]),
                product_ids=[] if any_product else [_int(p) for p in pids],
                steps=[Step.parse(s) for s in spec.get("steps", [])],
                kind=spec.get("kind", DEVICE), match=match,
                listen=[Step.parse(s) for s in spec.get("listen", [])],
                names={_int(k): v for k, v in (spec.get("names") or {}).items()},
                any_product=any_product, source=spec.get("source", ""),
                off_when_named=[str(x).lower() for x in spec.get("off_when_named", [])])
        if not r.steps and not r.listen:
            raise ValueError("a recipe needs steps or listen rules")
        return r

    @property
    def rules(self) -> List[Step]:
        """What incoming reports are matched against in listen mode."""
        return [s for s in self.steps if s.expect and s.fields] + self.listen

    def name_for(self, pid: int) -> str:
        return self.names.get(pid, self.name)

    def picks(self, info: dict) -> bool:
        keymap = {"usage_page": "usage_page", "usage": "usage", "interface": "interface_number"}
        for k, want in self.match.items():
            if k == "bluetooth":
                if is_bluetooth(info) != want:
                    return False
                continue
            have = info.get(keymap.get(k, k))
            if have not in (want if isinstance(want, list) else [want]):
                return False
        return True


def is_bluetooth(info: dict) -> bool:
    """A HID device connected over Bluetooth: its path names the Bluetooth HID
    service ({00001124-...}) or carries the "&0002" Bluetooth vendor-id source."""
    p = info.get("path") or b""
    text = (p.decode("ascii", "ignore") if isinstance(p, (bytes, bytearray)) else str(p)).lower()
    return "00001124-0000-1000-8000-00805f9b34fb" in text or "vid&0002" in text


def load(paths: Sequence[Path] = ()) -> List[Recipe]:
    """Bundled recipes plus any user files. A broken entry is logged and
    skipped rather than taking the others down with it."""
    by_key: Dict[tuple, Recipe] = {}
    vendor_wide: Dict[int, Recipe] = {}
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
            if r.any_product:
                vendor_wide[r.vendor_id] = r
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
    return out + list(vendor_wide.values())


def _close_all(handles) -> None:
    for h in handles:
        try:
            h.close()
        except Exception:
            pass


def evaluate(step: Step, reply: Sequence[int]) -> Dict[str, object]:
    base = step.locate(reply)
    if base is None:
        return {}
    out: Dict[str, object] = {}
    for name, f in step.fields.items():
        v = f.percent(reply, base) if name == "level" else f.flag(reply, base)
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
        handles, self.handles, self.writer = self.handles, [], None
        if not self.threaded:
            _close_all(handles)
        # Threaded: every reader closes its own handle when its read returns
        # (closing it under a blocked read can crash the process).

    def _reader(self, handle) -> None:
        try:
            while not self._stop.is_set():
                try:
                    report = handle.read(64, 1000)
                except (OSError, ValueError):
                    if not self._stop.is_set():
                        self.lost = True
                    return
                if report and not self._stop.is_set():
                    self.feed(report)
        finally:
            _close_all([handle])

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
            if not step.write or step.send == "none" or self.writer is None:
                continue
            try:
                if step.send == "feature":
                    self.writer.send_feature_report(step.write)
                else:
                    self.writer.write(step.write)
                if step.receive == "feature":
                    self.feed(self.writer.get_feature_report(step.write[0], step.read_len))
            except (OSError, ValueError):
                self.lost = True

    def reading(self) -> Reading:
        st = self.state
        online = not st.get("offline") and not self.lost
        pid = self.infos[0].get("product_id", 0) if self.infos else 0
        return Reading(self.key, self.recipe.name_for(pid), self.recipe.kind, st.get("level"),
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
        self.routes: Dict[tuple, object] = {}     # (vid, pid) -> the collection that answered
        self.sleep = time.sleep

    def set_recipes(self, recipes: Sequence[Recipe]) -> None:
        """Swap in a new recipe list (after the learn wizard saved one)."""
        self.close()
        self.recipes = list(recipes)

    def close(self) -> None:
        for lst in self.listeners.values():
            lst.close()
        self.listeners = {}

    reset = close          # after sleep: reopen listen-mode devices on the next poll

    def forget(self, key: str) -> None:
        self.last.pop(key, None)

    def readings(self) -> List[Reading]:
        return [lst.reading() for lst in self.listeners.values() if lst.state]

    @property
    def claimed(self) -> set:
        return {(r.vendor_id, p) for r in self.recipes for p in r.product_ids}

    @property
    def claimed_vendors(self) -> set:
        """Vendors every product of which a recipe reads (MCHOSE)."""
        return {r.vendor_id for r in self.recipes if r.any_product}

    def _run(self, handle, step: Step) -> Optional[List[int]]:
        """One step: send its request, return the first accepted reply (or [] for a
        step that expects none), None when nothing acceptable came."""
        for _ in range(step.tries):
            if step.send == "output":
                for _ in range(16):                      # drop stale input reports
                    if not handle.read(step.read_len, 0):
                        break
                handle.write(step.write)
            elif step.send == "feature":
                handle.send_feature_report(step.write)
            if step.delay_ms:
                self.sleep(step.delay_ms / 1000)
            if step.receive == "none":
                return []
            if step.receive == "feature":
                try:
                    reply = handle.get_feature_report(step.write[0] if step.write else 0, step.read_len)
                except (OSError, ValueError):
                    reply = None                         # some receivers "fail" until they have an answer
                if reply and step.accepts(reply):
                    return list(reply)
                continue
            deadline = self.clock() + step.timeout_ms / 1000
            while self.clock() < deadline:
                reply = handle.read(step.read_len, 100)
                if step.accepts(reply):
                    return list(reply)
        return None

    def _read(self, recipe: Recipe, info: dict) -> Optional[Reading]:
        pid = info["product_id"]
        key = f"hid-{recipe.vendor_id:04x}-{pid:04x}"
        name = recipe.name_for(pid)
        state: Dict[str, object] = {}
        handle = self.api.open(info["path"])
        try:
            for step in recipe.steps:
                if step.if_missing and state.get(step.if_missing) is not None:
                    continue
                reply = self._run(handle, step)
                if reply == [] and step.receive == "none":
                    continue
                self.log.append(f"{name}: {hexdump(reply, 12)}")
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
            return Reading(key, name, recipe.kind, prev.level if prev else None,
                           online=False, note="switched off")
        r = Reading(key, name, recipe.kind, state.get("level"),
                    bool(state.get("charging")), muted=bool(state.get("muted")))
        self.last[key] = r
        return r

    @staticmethod
    def _rank(recipe: Recipe, info: dict) -> tuple:
        # vendor collections first: they are where requests go
        return (info.get("usage_page", 0) < 0xFF00, info.get("interface_number", 0) or 0)

    def poll(self) -> List[Reading]:
        self.log = []
        out: List[Reading] = []
        by_vid: Dict[int, List[Recipe]] = {}
        for r in self.recipes:
            by_vid.setdefault(r.vendor_id, []).append(r)
        for vid, recipes in by_vid.items():
            infos = self.api.enumerate(vid)
            for recipe in recipes:
                pids = (sorted({d.get("product_id") for d in infos}) if recipe.any_product
                        else recipe.product_ids)
                for pid in pids:
                    if recipe.any_product and (vid, pid) in self._specific:
                        continue                         # a recipe for this very product wins
                    mine = [d for d in infos if d.get("product_id") == pid]
                    if recipe.listen:
                        r = self._listen(recipe, pid, mine)
                        if r:
                            out.append(r)
                        continue
                    candidates = sorted((d for d in mine if recipe.picks(d)),
                                        key=lambda d: self._rank(recipe, d))
                    r = self._read_first(recipe, pid, candidates)
                    if r:
                        out.append(r)
        return out

    @property
    def _specific(self) -> set:
        return {(r.vendor_id, p) for r in self.recipes if not r.any_product for p in r.product_ids}

    def _read_first(self, recipe: Recipe, pid: int, candidates: List[dict]) -> Optional[Reading]:
        """Try the collections in turn - the one that answered last time first - and
        keep the first real answer. Some dongles expose several vendor collections and
        only one of them talks."""
        key = (recipe.vendor_id, pid)
        product = " ".join(str(d.get("product_string") or "") for d in candidates).lower()
        if recipe.off_when_named and any(w in product for w in recipe.off_when_named):
            prev = self.last.get(f"hid-{recipe.vendor_id:04x}-{pid:04x}")
            if prev is None:
                return None
            return prev.with_(online=False, charging=False, muted=False, note="switched off")
        known = self.routes.get(key)
        candidates = sorted(candidates, key=lambda d: d.get("path") != known)
        fallback = None
        for info in candidates:
            try:
                r = self._read(recipe, info)
            except (OSError, ValueError) as e:
                self.log.append(f"{recipe.name_for(pid)}: {e}")
                continue
            if r is None:
                continue
            if r.online or r.note == "switched off":
                self.routes[key] = info.get("path")
                return r
            fallback = fallback or r
        return fallback

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
