"""HyperX headsets on the Kingston-era USB dongle: Cloud Flight S (0951:16EA,
0951:16EB) and Cloud II Wireless (0951:1718, 0951:0B92).

Protocol (as documented by the HyperHeadset project, MIT):

* request - output report on the dongle's control collection (sent as 16 bytes;
  hidapi pads it to the collection's report length)::

      06 00 02 00 9a 00 00 68 4a 8e 0a 00 00 00 bb <cmd> 00 ... 00

* reply   - input report ``0b 00 bb <cmd> <data...>``

      cmd 0x01  link status  byte 4: 1 or 4 = headset connected (other values
                differ between dongles and are not read as "off")
                (asking for 0x01 also makes the dongle report the mute state)
      cmd 0x02  battery      byte 7: percent
      cmd 0x03  charging     byte 4: 0 no, 1 charging, 2 full, other = error
      cmd 0x08  mic mute     byte 4: 1 = muted
      cmd 0x1A  auto power-off (get) byte 4: minutes;  cmd 0x18 sets it
      cmd 0x19  sidetone (set, payload 0/1), answered with byte 4 = the new state

Which collection takes the requests differs between dongles and operating
systems - usually the vendor page 0xFF13, but on Windows some dongles only take
them on another collection, and some only as *feature* reports ("Incorrect
function" for a plain write). So every collection that opens is tried, the
vendor page first, and once one of them answers only that one is written to.

The dongle also pushes these reports by itself (mute button, power switch), so
one reader thread per open collection listens all the time; the app sees a
mute press within a few hundred milliseconds instead of at the next poll.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable, Dict, List, Optional, Sequence

from .controls import CHOICE, TOGGLE, Control, Unavailable, find
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

CMD_STATUS, CMD_BATTERY, CMD_CHARGE, CMD_MUTE = 0x01, 0x02, 0x03, 0x08
CMD_SET_AUTO_OFF, CMD_SIDETONE, CMD_GET_AUTO_OFF = 0x18, 0x19, 0x1A
AUTO_OFF_CHOICES = (10, 20, 30)          # minutes, as NGENUITY offers them
ANSWER_WAIT = 1.5                        # seconds to wait for the headset to answer a setting
ANSWER_TIMEOUT = 1.0                     # per queued request, before the next one is sent
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


def packet(cmd: int, payload: Optional[int] = None) -> bytes:
    """The request, only as long as it needs to be: hidapi pads a short write to
    the collection's output report length, but refuses one that is *longer*.
    The Cloud II's report is 62 bytes, and a 62-byte packet was refused on a
    Cloud Flight S dongle, whose report is shorter - so it never got a request.
    (CubE135's Cloud Flight S monitor sends these same 16 bytes.)"""
    body = _BASE + bytes([cmd])
    return body if payload is None else body + bytes([payload])


def decode(report: Sequence[int]) -> Dict[str, object]:
    """One input report -> state changes such as ``{"level": 80}``. Anything
    that is not a well-formed reply decodes to ``{}``."""
    r = list(report)
    if len(r) < 8 or r[0] != REPLY_ID or r[2] != MAGIC:
        return {}
    cmd, value = r[3], r[4]
    if cmd == CMD_STATUS:
        # 1 or 4 means "connected" on every dongle. Other values do not mean "off"
        # everywhere: the Cloud Flight S answers the 3-second status query with
        # other values while it is on, which made the icon flip between on and
        # off. So only "connected" is taken from it (as CubE135's Flight S monitor
        # does); "off" comes from battery requests going unanswered.
        return {"online": True} if value in (1, 4) else {}
    if cmd == CMD_BATTERY:
        return {"online": True, "level": r[7]} if r[7] <= 100 else {}
    if cmd == CMD_CHARGE:
        return {"charging": value in (1, 2)}
    if cmd == CMD_MUTE:
        return {"muted": value == 1}
    if cmd == CMD_GET_AUTO_OFF:
        return {"auto_off": value}
    if cmd == CMD_SIDETONE:
        return {"sidetone": value == 1}
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
        self.auto_off: Optional[int] = None     # minutes; None until the headset said
        self.sidetone: Optional[bool] = None    # write-only: known once set from here

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
        if "auto_off" in changes:
            self.auto_off = int(changes["auto_off"])
        if "sidetone" in changes:
            self.sidetone = bool(changes["sidetone"])
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
        self._answered = threading.Condition(self._lock)   # a settings reply came in
        self._handles: List[object] = []
        self._writers: List[object] = []   # collections requests go to (narrowed on a reply)
        self._queue: deque = deque()       # commands waiting to be sent, one at a time
        self._replied: set = set()         # commands answered since they were last sent
        self._sender: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.log: deque = deque(maxlen=50)      # diagnostics; bounded, it is never cleared

    # -- device handling -------------------------------------------------
    def _open(self) -> bool:
        infos = [d for d in self.api.enumerate(HYPERX_VID) if d.get("product_id") in PRODUCTS]
        if not infos:
            return False
        self._stop = threading.Event()
        infos.sort(key=lambda d: (d.get("usage_page", 0) != VENDOR_PAGE,
                                  d.get("usage_page", 0) < 0xFF00))
        for d in infos:
            page, usage = d.get("usage_page", 0), d.get("usage", 0)
            try:
                h = self.api.open(d["path"])
            except OSError as e:
                # Keyboard/mouse-like collections belong to Windows: expected.
                self.log.append(f"open {page:04x}:{usage:04x}: {e}")
                continue
            self.log.append(f"opened {page:04x}:{usage:04x}")
            self._handles.append(h)
            self._writers.append(h)
            if self.threaded:
                threading.Thread(target=self._reader, args=(h, self._stop), daemon=True,
                                 name="hyperx-reader").start()
        if not self._handles:
            return False
        self.state.pid = infos[0]["product_id"]
        return True

    def close(self):
        self._stop.set()
        handles, self._handles, self._writers = self._handles, [], []
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
                    self.feed(report, handle)
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
    def feed(self, report: Sequence[int], handle=None) -> None:
        changes = decode(report)
        self.log.append(f"rx {hexdump(report, 12)}" + (f" -> {changes}" if changes else ""))
        if not changes:
            return
        log.debug("hyperx %s -> %s", hexdump(report, 10), changes)
        with self._lock:
            self._replied.add(report[3])             # the queued sender waits for this
            self._answered.notify_all()
            if handle is not None and handle in self._writers and len(self._writers) > 1:
                self._writers = [handle]             # this collection answers: use only it
                self.log.append("found the collection that answers")
            was_online = self.state.online
            changed = self.state.apply(changes)
            snap = self.state.reading()
            if "auto_off" in changes or "sidetone" in changes:
                self._answered.notify_all()
        if changes.get("online") and not was_online and "level" not in changes:
            self._send(CMD_BATTERY, CMD_CHARGE)      # just switched on: ask for its level
        if changed and self.on_change:
            self.on_change(snap)

    @staticmethod
    def _write(handle, data: bytes) -> None:
        try:
            handle.write(data)
            return
        except (OSError, ValueError) as e:
            send_feature = getattr(handle, "send_feature_report", None)
            if send_feature is None:
                raise
            try:
                send_feature(data)                   # dongles that only take feature reports
            except (OSError, ValueError):
                raise e from None

    def _send(self, *cmds: int) -> None:
        """Queue requests. The dongle handles one at a time: sent back to back, only
        the last one was answered (a Cloud Flight S never replied to the battery
        request between status and charging). So a sender thread writes one, waits
        for its reply (or ANSWER_TIMEOUT), then writes the next."""
        if not self.threaded:
            for cmd in cmds:                         # tests drive replies by hand
                self._write_all(packet(cmd))
            return
        with self._lock:
            for cmd in cmds:
                if cmd not in self._queue:
                    self._queue.append(cmd)
            if self._sender is None or not self._sender.is_alive():
                self._sender = threading.Thread(target=self._send_queued, daemon=True,
                                                name="hyperx-sender")
                self._sender.start()

    def _send_queued(self) -> None:
        while True:
            with self._lock:
                if not self._queue or not self._writers:
                    self._queue.clear()
                    return
                cmd = self._queue.popleft()
                self._replied.discard(cmd)
            if not self._write_all(packet(cmd)):
                return
            with self._lock:
                if not self._answered.wait_for(lambda c=cmd: c in self._replied, ANSWER_TIMEOUT):
                    self.log.append(f"no reply to command {cmd:#04x}")

    def _write_all(self, data: bytes) -> bool:
        """Writes to every collection still taking requests; False when none is left."""
        for writer in list(self._writers):
            try:
                self._write(writer, data)
            except (OSError, ValueError) as e:
                self.log.append(f"write: {e}")
                with self._lock:
                    if writer in self._writers:
                        self._writers.remove(writer)
                    gone = not self._writers
                if gone:
                    self._lost()                     # nothing takes requests: unplugged
                    return False
        return True

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
        if self._writers and self.state.online:
            self._send(CMD_STATUS)

    # -- headset settings ------------------------------------------------------------
    def has_controls(self, key: str) -> bool:
        return self.seen and key == self.state.key

    def _ask(self, cmd: int, payload: int, done) -> bool:
        """Sends one request and waits until ``done()`` holds (the reply was read)."""
        with self._lock:
            if not self._writers or not self.state.online:
                raise Unavailable("Switch the headset on first.")
        self._send_packet(packet(cmd, payload))
        with self._lock:
            return self._answered.wait_for(done, ANSWER_WAIT)

    def _send_packet(self, data: bytes) -> None:
        for writer in list(self._writers):
            try:
                self._write(writer, data)
            except (OSError, ValueError) as e:
                self.log.append(f"write: {e}")

    def _controls(self) -> List[Control]:
        st = self.state
        options = [(m, f"After {m} minutes") for m in AUTO_OFF_CHOICES]
        if st.auto_off is not None and st.auto_off not in AUTO_OFF_CHOICES:
            options.insert(0, (st.auto_off, "Never" if st.auto_off == 0 else f"After {st.auto_off} minutes"))
        return [
            Control("auto_off", "Turn off when idle", CHOICE, st.auto_off, options=options,
                    help="Switches the headset off after this long without sound, to save battery."),
            Control("sidetone", "Sidetone", TOGGLE, st.sidetone,
                    help="Hear your own voice in the headset while you talk. The headset does not "
                         "report this setting, so it shows once it has been set here."),
        ]

    def controls(self, key: str) -> List[Control]:
        if not self._ask(CMD_GET_AUTO_OFF, 0, lambda: self.state.auto_off is not None):
            raise Unavailable("The headset did not answer. Check that it is on, then try again.")
        return self._controls()

    def set_control(self, key: str, control_id: str, value) -> List[Control]:
        control = find(self._controls(), control_id)
        if control is None:
            raise ValueError("this headset has no such setting")
        value = control.check(value)
        if control_id == "auto_off":
            with self._lock:
                self.state.auto_off = None
            self._ask(CMD_SET_AUTO_OFF, value, lambda: True)
            ok = self._ask(CMD_GET_AUTO_OFF, 0, lambda: self.state.auto_off is not None)
            if not ok or self.state.auto_off != value:
                raise Unavailable("The headset did not take the new setting.")
        else:
            with self._lock:
                self.state.sidetone = None
            if not self._ask(CMD_SIDETONE, int(value), lambda: self.state.sidetone is not None):
                raise Unavailable("The headset did not answer. Check that it is on, then try again.")
        return self._controls()

    def readings(self) -> List[Reading]:
        with self._lock:
            return [self.state.reading()] if self.seen else []
