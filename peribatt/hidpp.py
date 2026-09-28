"""Logitech mice over HID++ 2.0 - e.g. the G PRO Wireless on its LIGHTSPEED
receiver (046D:C539) or on its USB cable (046D:C088).

Wire format (long report, 20 bytes, on the vendor collection FF00:0002):

    11 <device index> <feature index> <function << 4 | software id> <params...>

* device index: 1..6 behind a receiver, 0xFF for a device on its cable.
* feature index 0 is the root feature; its function 0 turns a feature *id*
  into the per-device feature *index* used for every other call.
* Errors: the receiver answers ``10 <idx> 8F <feat> <fn> <code>`` (HID++ 1.0,
  short report) - code 0x08 means "no device paired in that slot", 0x09 means
  "paired but unreachable" (switched off / asleep). The device itself answers
  ``11 <idx> FF <feat> <fn> <code>`` (HID++ 2.0).

Battery features, the first one a device offers is used:

    0x1004 unified battery, fn 1: <percent> <level flags> <charge status> <ext power>
    0x1000 battery status,  fn 0: <percent> <next level> <status>
    0x1001 battery voltage, fn 0: <mV hi> <mV lo> <flags, bit 7 = external power>

The G PRO Wireless reports *voltage*, so the percentage is estimated from a
Li-ion discharge curve.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import logi_controls
from .controls import Control, Unavailable
from .hidio import HidApi, hexdump
from .model import DEVICE, HEADSET, KEYBOARD, MOUSE, Reading

log = logging.getLogger("peribatt")

LOGITECH_VID = 0x046D
VENDOR_PAGE = 0xFF00
USAGE_SHORT, USAGE_LONG = 0x0001, 0x0002

SHORT, LONG = 0x10, 0x11
LONG_LEN = 20
SW_ID = 0x0B
# The software id is rotated per request, so a late reply to an earlier request
# (one that timed out) can never be taken for the answer to the current one.
SW_IDS = (0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F)
WIRED = 0xFF
RECEIVER_SLOTS = range(1, 7)
SILENT_BACKOFF = 600.0      # a slot that never answered is skipped for 10 minutes
WAKE_TIMEOUT = 2.0          # a dozing LIGHTSPEED radio can take ~0.5 s to answer at first

ERR_V1, ERR_V2 = 0x8F, 0xFF
ERR_UNKNOWN_DEVICE = 0x08
ERR_UNREACHABLE = 0x09

F_ROOT = 0x0000
FN_PING = 1
F_DEVICE_INFO = 0x0003
F_NAME = 0x0005
F_UNIFIED = 0x1004
F_STATUS = 0x1000
F_VOLTAGE = 0x1001
F_ADC = 0x1F20              # headsets: battery voltage, bit 0 connected, bit 1 charging
BATTERY_FEATURES = (F_UNIFIED, F_STATUS, F_VOLTAGE, F_ADC)

# Logitech headsets that speak HID++ on their own dongle (HeadsetControl's list); the
# battery comes from feature 0x1F20. The value is the name when the headset has none.
HEADSETS = {
    0x0A66: "Logitech G533", 0x0AC4: "Logitech G535", 0x0A5C: "Logitech G633",
    0x0A89: "Logitech G635", 0x0A5B: "Logitech G933", 0x0A87: "Logitech G935",
    0x0AB5: "Logitech G733", 0x0AFE: "Logitech G733", 0x0B1F: "Logitech G733",
    0x0AA7: "Logitech G PRO", 0x0AAA: "Logitech G PRO X", 0x0ABA: "Logitech G PRO X",
    0x0AFB: "Logitech G PRO X 2", 0x0AFC: "Logitech G PRO X 2",
}
HEADSET_COLLECTION = {0x0AC4: (0x000C, 0x0001)}      # others: (0xFF43, 0x0202)

# Unifying / Bolt / LIGHTSPEED receivers. Anything else with a HID++ collection
# is treated as a device on its own cable.
RECEIVERS = {0xC52B, 0xC52F, 0xC531, 0xC532, 0xC534, 0xC539, 0xC53A, 0xC53D,
             0xC53F, 0xC541, 0xC545, 0xC547, 0xC548, 0xC54D}

RECEIVER_WORD = "receiver"   # a receiver not in the list above still names itself one

# 0x0005 function 2 device type
_TYPES = {0: KEYBOARD, 2: KEYBOARD, 3: MOUSE, 4: MOUSE, 5: MOUSE}

# Typical single-cell Li-ion curve (mV, %), the same points Solaar uses.
VOLTAGE_CURVE: Tuple[Tuple[int, int], ...] = (
    (4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60), (3811, 50),
    (3778, 40), (3751, 30), (3717, 20), (3671, 10), (3646, 5), (3579, 2), (3500, 0))


class HidppError(Exception):
    def __init__(self, code: int, legacy: bool):
        super().__init__(f"HID++{' 1.0' if legacy else ' 2.0'} error 0x{code:02x}")
        self.code = code
        self.legacy = legacy


def build_request(index: int, feature_index: int, function: int,
                  params: Sequence[int] = (), swid: int = SW_ID) -> List[int]:
    msg = [LONG, index, feature_index, ((function & 0x0F) << 4) | swid, *params]
    if len(msg) > LONG_LEN:
        raise ValueError("too many parameters for a long report")
    return msg + [0] * (LONG_LEN - len(msg))


def match_reply(report: Sequence[int], index: int, feature_index: int,
                function: int, swid: int = SW_ID) -> Optional[List[int]]:
    """Params of ``report`` when it answers our request, ``None`` when it is
    unrelated traffic, and :class:`HidppError` when it is an error reply."""
    if len(report) < 5 or report[1] != index:
        return None
    fn = ((function & 0x0F) << 4) | swid
    if report[2] in (ERR_V1, ERR_V2) and report[3] == feature_index and report[4] == fn:
        code = report[5] if len(report) > 5 else 0
        raise HidppError(code, legacy=report[2] == ERR_V1)
    if report[0] == LONG and report[2] == feature_index and report[3] == fn:
        params = list(report[4:])
        return params + [0] * (16 - len(params))
    return None


def voltage_to_percent(mv: int) -> int:
    if mv >= VOLTAGE_CURVE[0][0]:
        return 100
    for (v_hi, p_hi), (v_lo, p_lo) in zip(VOLTAGE_CURVE, VOLTAGE_CURVE[1:], strict=False):
        if mv >= v_lo:
            return round(p_lo + (mv - v_lo) * (p_hi - p_lo) / (v_hi - v_lo))
    return 0


def battery_function(feature: int) -> int:
    return 1 if feature == F_UNIFIED else 0


def decode_battery(feature: int, p: Sequence[int]) -> Tuple[Optional[int], bool]:
    """(percent or None, on external power) from a battery reply.
    A full battery that is still plugged in counts as charging (green frame)."""
    if feature == F_UNIFIED:
        level = p[0] if 0 < p[0] <= 100 else _approx_from_flags(p[1])
        return level, p[2] in (1, 2, 3, 4)          # 4 = slow charging
    if feature == F_STATUS:
        level = p[0] if 0 < p[0] <= 100 else None
        return level, p[2] in (1, 2, 3, 4)
    if feature == F_VOLTAGE:
        mv = (p[0] << 8) | p[1]
        if mv < 2500:            # nonsense / not measured yet
            return None, bool(p[2] & 0x80)
        return voltage_to_percent(mv), bool(p[2] & 0x80)
    if feature == F_ADC:
        mv = (p[0] << 8) | p[1]
        if not p[2] & 0x01 or mv < 2500:    # the headset is not connected to its dongle
            return None, False
        return voltage_to_percent(mv), bool(p[2] & 0x02)
    return None, False


def _approx_from_flags(flags: int) -> Optional[int]:
    for bit, pct in ((8, 90), (4, 50), (2, 20), (1, 5)):
        if flags & bit:
            return pct
    return None


def instance(path) -> str:
    r"""The device instance in a Windows HID path, without its collection number:
    ``\\?\HID#VID_046D&PID_C539&MI_02&Col02#7&2b1f0a3&0&0001#{...}`` -> ``7&2b1f0a3&0``.
    A receiver's short and long collections differ only in that last number, so
    they share this; a second receiver of the same kind does not. Empty elsewhere."""
    text = path.decode("ascii", "ignore") if isinstance(path, (bytes, bytearray)) else str(path or "")
    parts = text.split("#")
    return parts[2].lower().rsplit("&", 1)[0] if len(parts) > 2 else ""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "device"


class Channel:
    """Request/response over the long collection. The short collection, when it
    can be opened, is read too, because the receiver's errors arrive there."""

    def __init__(self, long_handle, short_handle=None,
                 clock: Callable[[], float] = time.monotonic):
        self.handles = [h for h in (long_handle, short_handle) if h is not None]
        self.long = long_handle
        self.clock = clock
        self._swid = 0

    def close(self):
        for h in self.handles:
            try:
                h.close()
            except Exception:
                pass

    def _drain(self):
        for h in self.handles:
            for _ in range(32):
                if not h.read(64, 0):
                    break

    def call(self, index: int, feature_index: int, function: int,
             params: Sequence[int] = (), timeout: float = 1.0) -> List[int]:
        self._drain()
        self._swid = (self._swid + 1) % len(SW_IDS)
        swid = SW_IDS[self._swid]
        self.long.write(build_request(index, feature_index, function, params, swid))
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            for i, h in enumerate(self.handles):
                report = h.read(64, 40 if i == 0 else 0)
                if report:
                    params_out = match_reply(report, index, feature_index, function, swid)
                    if params_out is not None:
                        return params_out
        raise TimeoutError(f"no reply from device {index:#x}")

    def ping(self, index: int, timeout: float = WAKE_TIMEOUT) -> None:
        """Is a device in this slot, and awake? Raises like :meth:`call`. The first
        request after a pause wakes the radio, so it gets the long timeout; a HID++ 2.0
        error still means the device answered."""
        try:
            self.call(index, F_ROOT, FN_PING, (0, 0, 0x5A), timeout=timeout)
        except HidppError as e:
            if e.legacy:
                raise

    def feature_index(self, index: int, feature_id: int) -> Optional[int]:
        try:
            p = self.call(index, 0, 0, (feature_id >> 8, feature_id & 0xFF))
        except HidppError as e:
            if e.legacy:
                raise
            return None
        return p[0] or None


@dataclass
class Profile:
    """What we learned about a device the first time it answered."""
    key: str
    name: str
    kind: str
    feature: int
    feature_index: int


def discover(ch: Channel, index: int) -> Optional[Profile]:
    battery = None
    for feature in BATTERY_FEATURES:
        fi = ch.feature_index(index, feature)
        if fi:
            battery = (feature, fi)
            break
    if battery is None:
        return None
    name, kind = "Logitech device", DEVICE
    fi = ch.feature_index(index, F_NAME)
    if fi:
        length = ch.call(index, fi, 0)[0]
        raw = b""
        while len(raw) < length:
            chunk = bytes(ch.call(index, fi, 1, (len(raw),))[:16])
            if not chunk.strip(b"\0"):
                break
            raw += chunk
        name = raw[:length].decode("utf-8", "replace").strip("\0 ") or name
        kind = _TYPES.get(ch.call(index, fi, 2)[0], DEVICE)
    unit = ""
    fi = ch.feature_index(index, F_DEVICE_INFO)
    if fi:
        p = ch.call(index, fi, 0)
        if any(p[1:5]):
            unit = bytes(p[1:5]).hex()
    key = f"logi-{unit}" if unit else f"logi-{slug(name)}"
    return Profile(key, name, kind, battery[0], battery[1])


class LogitechSource:
    """Polled source: every :meth:`poll` returns the current readings."""

    name = "logitech"

    def __init__(self, api=None, known: Optional[Dict[str, dict]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.api = api or HidApi()
        self.clock = clock
        self.profiles: Dict[str, Profile] = {}   # slot id -> profile (this session)
        # slot id -> {"key", "name", "kind"}; persisted so a mouse that is off at
        # start-up still gets its (grey) icon.
        self.known: Dict[str, dict] = known if known is not None else {}
        self.silent: Dict[str, float] = {}       # slot id -> skip until (monotonic time)
        self.log: List[str] = []
        self.where: Dict[str, Tuple[int, str, int]] = {}   # key -> (pid, instance, slot), seen online
        self.caps: Dict[str, Dict[str, Optional[int]]] = {}  # key -> settings feature indexes
        self._io = threading.RLock()             # polling and device settings share the receiver
        self.receivers = set(RECEIVERS)

    @staticmethod
    def slot_id(pid: int, index: int, instance: str = "") -> str:
        return f"{pid:04x}:{index}" + (f"@{instance}" if instance else "")

    def _collections(self) -> Dict[Tuple[int, str], Dict[int, bytes]]:
        """(product id, receiver instance) -> {usage: path}. Two receivers of the same
        kind share a product id (every Unifying receiver is C52B), so the instance
        from the path keeps them apart; it is left empty when a product id appears
        only once, which keeps the keys of the usual single receiver short."""
        groups: Dict[Tuple[int, str], Dict[int, bytes]] = {}
        headsets: Dict[Tuple[int, str], bytes] = {}
        for d in self.api.enumerate(LOGITECH_VID):
            pid, page, usage = d["product_id"], d.get("usage_page"), d.get("usage")
            if RECEIVER_WORD in str(d.get("product_string") or "").lower():
                self.receivers.add(pid)
            if page == VENDOR_PAGE and usage in (USAGE_SHORT, USAGE_LONG):
                groups.setdefault((pid, instance(d.get("path"))), {})[usage] = d["path"]
            elif pid in HEADSETS and (page, usage) == HEADSET_COLLECTION.get(pid, (0xFF43, 0x0202)):
                headsets[(pid, instance(d.get("path")))] = d["path"]
        for k, path in headsets.items():              # only a long channel on these
            groups.setdefault(k, {}).setdefault(USAGE_LONG, path)
        groups = {k: g for k, g in groups.items() if USAGE_LONG in g}
        pids = [pid for pid, _ in groups]
        return {(pid, inst if pids.count(pid) > 1 else ""): g for (pid, inst), g in groups.items()}

    def _open(self, paths: Dict[int, bytes]) -> Channel:
        long_h = self.api.open(paths[USAGE_LONG])
        short_h = None
        if USAGE_SHORT in paths:
            try:
                short_h = self.api.open(paths[USAGE_SHORT])
            except OSError:
                pass
        return Channel(long_h, short_h, self.clock)

    def _read(self, ch: Channel, pid: int, index: int, inst: str = "") -> Optional[Reading]:
        sid = self.slot_id(pid, index, inst)
        try:
            ch.ping(index)
            prof = self.profiles.get(sid)
            if prof is None:
                prof = discover(ch, index)
                if prof is None:
                    self.log.append(f"{sid}: no battery feature")
                    return None
                if pid in HEADSETS:
                    name = prof.name if prof.name != "Logitech device" else HEADSETS[pid]
                    prof = Profile(prof.key, name, HEADSET, prof.feature, prof.feature_index)
                self.profiles[sid] = prof
                self.known[sid] = {"key": prof.key, "name": prof.name, "kind": prof.kind}
            p = ch.call(index, prof.feature_index, battery_function(prof.feature))
        except HidppError as e:
            self.profiles.pop(sid, None)
            if e.legacy and e.code == ERR_UNKNOWN_DEVICE:
                self.known.pop(sid, None)       # slot is empty
            elif e.legacy and e.code == ERR_UNREACHABLE:
                self.log.append(f"{sid}: paired, switched off or out of range")
                return None
            self.log.append(f"{sid}: {e}")
            return None
        except TimeoutError as e:
            self.log.append(f"{sid}: {e}")
            if sid not in self.known:
                # Some receivers never answer for unused slots: stop paying a
                # one-second timeout for each of them on every poll.
                self.silent[sid] = self.clock() + SILENT_BACKOFF
            return None
        level, charging = decode_battery(prof.feature, p)
        if prof.feature == F_ADC and level is None:
            self.log.append(f"{sid} {prof.name}: headset not connected")
            return None                          # shown greyed as "switched off"
        self.where[prof.key] = (pid, inst, index)
        self.log.append(f"{sid} {prof.name}: {hexdump(p, 4)} -> {level}% charging={charging}")
        return Reading(prof.key, prof.name, prof.kind, level, charging, online=True)

    def poll(self) -> List[Reading]:
        with self._io:
            return self._poll()

    def _poll(self) -> List[Reading]:
        self.log = []
        out: List[Reading] = []
        groups = self._collections()
        if not groups:
            self.log.append("no Logitech HID++ collection (FF00:0002) found")
        for (pid, inst), paths in groups.items():
            where = f"{pid:04x}" + (f"@{inst}" if inst else "")
            self.log.append(f"{where}: collections {sorted(paths)}")
            try:
                ch = self._open(paths)
            except OSError as e:
                self.log.append(f"{where}: open failed: {e}")
                continue
            try:
                for index in (RECEIVER_SLOTS if pid in self.receivers else (WIRED,)):
                    if self.silent.get(self.slot_id(pid, index, inst), 0) > self.clock():
                        continue
                    r = self._read(ch, pid, index, inst)
                    if r:
                        out.append(r)
            except OSError as e:
                self.log.append(f"{where}: {e}")
            finally:
                ch.close()
        present = {pid for pid, _ in groups}
        online = {r.key for r in out}
        for sid, info in list(self.known.items()):
            if info["key"] not in online:
                pid = int(sid.split(":")[0], 16)
                note = "switched off" if pid in present else "not connected"
                out.append(Reading(info["key"], info["name"], info.get("kind", DEVICE),
                                   None, False, online=False, note=note))
        return out

    # -- device settings (DPI, polling rate...) ------------------------------------
    def has_controls(self, key: str) -> bool:
        caps = self.caps.get(key)
        return key in self.where and (caps is None or any(caps.values()))

    def _on_device(self, key: str, action):
        """Opens the device's receiver, wakes the device and runs ``action(ch, slot, caps)``."""
        with self._io:
            where = self.where.get(key)
            if where is None:
                raise Unavailable("Switch the device on first: it has not answered since the app started.")
            pid, inst, index = where
            paths = self._collections().get((pid, inst))
            if not paths:
                raise Unavailable("Its receiver or cable is not plugged in.")
            try:
                ch = self._open(paths)
            except OSError as e:
                raise Unavailable(f"Could not open its receiver: {e}") from None
            try:
                ch.ping(index)
                caps = self.caps.get(key)
                if caps is None:
                    caps = self.caps[key] = logi_controls.capabilities(ch, index)
                return action(ch, index, caps)
            except HidppError as e:
                if e.legacy:
                    raise Unavailable("The device is switched off or out of range.") from None
                raise Unavailable(f"The device refused the request ({e}).") from None
            except TimeoutError:
                raise Unavailable("The device did not answer. Move it or press a key to wake it up, "
                                  "then try again.") from None
            except OSError as e:
                raise Unavailable(f"Lost the connection to the receiver: {e}") from None
            finally:
                ch.close()

    def controls(self, key: str) -> List[Control]:
        return self._on_device(key, logi_controls.read)

    def set_control(self, key: str, control_id: str, value) -> List[Control]:
        return self._on_device(key, lambda ch, i, caps: logi_controls.write(ch, i, caps, control_id, value))

    def forget(self, key: str) -> None:
        for sid, info in list(self.known.items()):
            if info.get("key") == key:
                self.known.pop(sid, None)
                self.profiles.pop(sid, None)
        self.where.pop(key, None)
        self.caps.pop(key, None)
