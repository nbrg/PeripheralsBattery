"""HyperX headsets on the Kingston-era USB dongle: Cloud Flight S (0951:16EA,
0951:16EB) and Cloud II Wireless (0951:1718, 0951:0B92).

Protocol (as documented by the HyperHeadset project, MIT):

* request - 62-byte output report on the vendor collection (usage page 0xFF13)::

      06 00 02 00 9a 00 00 68 4a 8e 0a 00 00 00 bb <cmd> 00 ... 00

* reply   - input report ``0b 00 bb <cmd> <data...>``

      cmd 0x01  link status  byte 4: 1 or 4 = headset connected, 2 = pairing
                (asking for 0x01 also makes the dongle report the mute state)
      cmd 0x02  battery      byte 7: percent
      cmd 0x03  charging     byte 4: 0 no, 1 charging, 2 full, other = error
      cmd 0x08  mic mute     byte 4: 1 = muted

The dongle also pushes these reports by itself (mute button, power switch), so
one reader thread per open collection listens all the time; the app sees a
mute press within a few hundred milliseconds instead of at the next poll.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable, Dict, List, Optional, Sequence

from .hidio import HidApi, hexdump
from .model import HEADSET, Reading

log = logging.getLogger("peribatt")

HYPERX_VID = 0x0951
PRODUCTS = {
    0x16EA: "HyperX Cloud Flight S",
    0x16EB: "HyperX Cloud Flight S",
    0x1718: "HyperX Cloud II Wireless",
    0x0B92: "HyperX Cloud II Wireless",
}
VENDOR_PAGE = 0xFF13
PACKET_LEN = 62

CMD_STATUS, CMD_BATTERY, CMD_CHARGE, CMD_MUTE = 0x01, 0x02, 0x03, 0x08
REPLY_ID, MAGIC = 0x0B, 0xBB

_BASE = bytes([0x06, 0x00, 0x02, 0x00, 0x9A, 0x00, 0x00, 0x68,
               0x4A, 0x8E, 0x0A, 0x00, 0x00, 0x00, MAGIC])

MISSED_POLLS_OFFLINE = 2
READ_MS = 1000              # reader wake-up interval; also how soon a closed handle is released


def _close_all(handles) -> None:
    for h in handles:
        try:
            h.close()
        except Exception:
            pass


def packet(cmd: int, payload: int = 0) -> bytes:
    body = _BASE + bytes([cmd, payload])
    return body + bytes(PACKET_LEN - len(body))


def decode(report: Sequence[int]) -> Dict[str, object]:
    """One input report -> state changes such as ``{"level": 80}``. Anything
    that is not a well-formed reply decodes to ``{}``."""
    r = list(report)
    if len(r) < 8 or r[0] != REPLY_ID or r[2] != MAGIC:
        return {}
    cmd, value = r[3], r[4]
    if cmd == CMD_STATUS:
        return {"online": value in (1, 4)}
    if cmd == CMD_BATTERY:
        return {"online": True, "level": r[7]} if r[7] <= 100 else {}
    if cmd == CMD_CHARGE:
        return {"charging": value in (1, 2)}
    if cmd == CMD_MUTE:
        return {"muted": value == 1}
    return {}


class HeadsetState:
    def __init__(self, pid: int = 0x16EA):
        self.pid = pid
        self.present = False          # dongle plugged in
        self.online = False           # headset switched on and linked
        self.level: Optional[int] = None
        self.charging = False
        self.muted = False
        self.missed = 0               # polls without any battery answer

    @property
    def name(self) -> str:
        return PRODUCTS.get(self.pid, "HyperX headset")

    @property
    def key(self) -> str:
        return f"hyperx-{self.pid:04x}"

    def apply(self, changes: Dict[str, object]) -> bool:
        before = self.reading()
        if "online" in changes:
            self.online = bool(changes["online"])
        if "level" in changes:
            self.level = int(changes["level"])
            self.missed = 0
        if "charging" in changes:
            self.charging = bool(changes["charging"])
        if "muted" in changes:
            self.muted = bool(changes["muted"])
        return self.reading() != before

    def reading(self) -> Reading:
        online = self.present and self.online
        note = "" if online else ("switched off" if self.present else "dongle unplugged")
        return Reading(self.key, self.name, HEADSET, self.level, self.charging and online,
                       online=online, muted=self.muted and online, note=note)


class HyperXSource:
    """Event-driven source. :meth:`poll` (re)opens the dongle when needed and
    asks for status, battery and charging; :meth:`poll_fast` only asks for the
    status/mute report and is cheap enough to run every few seconds."""

    name = "hyperx"
    vendor_ids = {HYPERX_VID: set(PRODUCTS)}

    def __init__(self, api=None, on_change: Optional[Callable[[Reading], None]] = None,
                 threaded: bool = True):
        self.api = api or HidApi()
        self.on_change = on_change
        self.threaded = threaded
        self.state = HeadsetState()
        self.seen = False               # the dongle has been found at least once
        self._lock = threading.RLock()
        self._handles: List[object] = []
        self._writer = None
        self._stop = threading.Event()
        self.log: deque = deque(maxlen=50)      # diagnostics; bounded, it is never cleared

    # -- device handling -------------------------------------------------
    def _open(self) -> bool:
        infos = [d for d in self.api.enumerate(HYPERX_VID) if d.get("product_id") in PRODUCTS]
        if not infos:
            return False
        self._stop = threading.Event()
        for d in infos:
            page = d.get("usage_page", 0)
            if page != VENDOR_PAGE:
                continue
            try:
                h = self.api.open(d["path"])
            except OSError as e:
                self.log.append(f"open {page:04x}: {e}")
                continue
            self._handles.append(h)
            self._writer = self._writer or h
            if self.threaded:
                threading.Thread(target=self._reader, args=(h, self._stop), daemon=True,
                                 name="hyperx-reader").start()
        if self._writer is None:
            return False
        self.state.pid = infos[0]["product_id"]
        return True

    def close(self):
        self._stop.set()
        handles, self._handles, self._writer = self._handles, [], None
        if not self.threaded:
            _close_all(handles)
        # Threaded: each reader closes its own handle once its read returns. Closing a
        # handle while another thread is inside hid_read on it can crash the process.

    def _reader(self, handle, stop: threading.Event):
        try:
            while not stop.is_set():
                try:
                    report = handle.read(64, READ_MS)
                except (OSError, ValueError) as e:
                    if not stop.is_set():
                        log.info("hyperx reader stopped: %s", e)
                        self._lost()
                    return
                if report and not stop.is_set():
                    self.feed(report)
        finally:
            _close_all([handle])

    def _lost(self):
        with self._lock:
            self.close()
            was = self.state.present
            self.state.present = False
            snap = self.state.reading()
        if was and self.on_change:
            self.on_change(snap)

    # -- state -----------------------------------------------------------
    def feed(self, report: Sequence[int]) -> None:
        changes = decode(report)
        if not changes:
            return
        log.debug("hyperx %s -> %s", hexdump(report, 10), changes)
        with self._lock:
            was_online = self.state.online
            changed = self.state.apply(changes)
            snap = self.state.reading()
        if changes.get("online") and not was_online and "level" not in changes:
            self._send(CMD_BATTERY, CMD_CHARGE)      # just switched on: ask for its level
        if changed and self.on_change:
            self.on_change(snap)

    def _send(self, *cmds: int) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            for cmd in cmds:
                writer.write(packet(cmd))
        except (OSError, ValueError) as e:
            self.log.append(f"write: {e}")
            self._lost()

    def poll(self) -> List[Reading]:
        with self._lock:
            if not self._handles:
                self.state.present = self._open()
            if self.state.present:
                self.seen = True
                self.state.missed += 1
                if self.state.missed > MISSED_POLLS_OFFLINE:
                    self.state.online = False
        self._send(CMD_STATUS, CMD_BATTERY, CMD_CHARGE)
        return self.readings()

    def reset(self) -> None:
        """After sleep the dongle's handles can be dead without saying so:
        close them, and the next poll opens it afresh."""
        with self._lock:
            self.close()
            self.state.missed = 0

    def poll_fast(self) -> None:
        # Only while the headset is on: a switched-off headset has no mute state.
        if self._writer is not None and self.state.online:
            self._send(CMD_STATUS)

    def readings(self) -> List[Reading]:
        with self._lock:
            return [self.state.reading()] if self.seen else []
