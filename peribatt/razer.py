"""Razer wireless mice, keyboards and headsets (HyperSpeed / Pro dongles).

Razer's control protocol is one 90-byte feature report (report id 0), the one
OpenRazer and Synapse use::

    [0] status   0x00 request / 0x02 ok / 0x01 busy / 0x03 fail / 0x04 no response / 0x05 unsupported
    [1] transaction id (device dependent: 0x1F, 0x3F, 0xFF, ...)
    [2..3] remaining packets, [4] protocol type, [5] data size
    [6] command class (0x07 = power)   [7] command id (0x80 battery, 0x84 charging)
    [8..87] arguments (the answer is in arg 1)   [88] XOR of bytes 2..87   [89] 0

Rather than shipping a table of every Razer product id, each Razer device is
probed once: the collections and transaction ids are tried until one answers,
and the winning combination is remembered. Devices that answer "unsupported"
(wired keyboards, mouse mats) are skipped from then on, so the probing costs
nothing after the first poll.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .hidio import HidApi
from .model import DEVICE, HEADSET, KEYBOARD, MOUSE, Reading

log = logging.getLogger("peribatt")

RAZER_VID = 0x1532
REPORT_LEN = 90
TRANSACTION_IDS = (0x1F, 0x3F, 0xFF, 0x9F, 0x08)
CLASS_POWER, CMD_BATTERY, CMD_CHARGING = 0x07, 0x80, 0x84
ST_OK, ST_BUSY, ST_FAIL, ST_NO_RESPONSE, ST_UNSUPPORTED = 0x02, 0x01, 0x03, 0x04, 0x05


def build_report(tid: int, cmd_class: int, cmd_id: int, data_size: int = 0x02,
                 args: Sequence[int] = ()) -> bytes:
    msg = bytearray(REPORT_LEN)
    msg[1], msg[5], msg[6], msg[7] = tid, data_size, cmd_class, cmd_id
    msg[8:8 + len(args)] = bytes(args)
    crc = 0
    for b in msg[2:88]:
        crc ^= b
    msg[88] = crc
    return bytes(msg)


def parse_report(data: Sequence[int], cmd_class: int, cmd_id: int
                 ) -> Tuple[Optional[int], Optional[int]]:
    """(status, arg1) of a reply to ``cmd_class/cmd_id``. The report id byte
    that hidapi puts in front is stripped when present."""
    d = list(data)
    if len(d) == REPORT_LEN + 1:
        d = d[1:]
    if len(d) < REPORT_LEN:
        return None, None
    if d[6] != cmd_class or d[7] != cmd_id:
        return d[0], None
    return d[0], d[9]


def raw_to_percent(raw: int) -> int:
    return round(raw * 100 / 255)


def guess_kind(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ("blackshark", "barracuda", "kraken", "nari", "hammerhead", "headset")):
        return HEADSET
    if any(w in n for w in ("keyboard", "blackwidow", "huntsman", "ornata", "deathstalker")):
        return KEYBOARD
    if any(w in n for w in ("viper", "basilisk", "deathadder", "naga", "orochi", "cobra",
                            "pro click", "atheris", "mamba", "lancehead", "mouse")):
        return MOUSE
    return DEVICE


class RazerSource:
    name = "razer"

    def __init__(self, api=None, sleep: Callable[[float], None] = time.sleep):
        self.api = api or HidApi()
        self.sleep = sleep
        self.working: Dict[int, Tuple[int, int]] = {}   # pid -> (collection #, tid)
        self.unsupported: set = set()
        self.last: Dict[int, Reading] = {}
        self.log: List[str] = []

    def _exchange(self, dev, tid: int, cmd_id: int) -> Tuple[Optional[int], Optional[int]]:
        dev.send_feature_report(b"\x00" + build_report(tid, CLASS_POWER, cmd_id))
        for _ in range(3):
            self.sleep(0.05)
            status, value = parse_report(dev.get_feature_report(0x00, REPORT_LEN + 1),
                                         CLASS_POWER, cmd_id)
            if status != ST_BUSY:
                return status, value
        return ST_BUSY, None

    def _query(self, path, tid: int) -> Tuple[Optional[int], Optional[int], bool]:
        dev = self.api.open(path)
        try:
            status, raw = self._exchange(dev, tid, CMD_BATTERY)
            charging = False
            if status == ST_OK:
                st2, chg = self._exchange(dev, tid, CMD_CHARGING)
                charging = st2 == ST_OK and bool(chg)
            return status, raw, charging
        finally:
            dev.close()

    def poll(self) -> List[Reading]:
        self.log = []
        by_pid: Dict[int, List[dict]] = {}
        for d in self.api.enumerate(RAZER_VID):
            by_pid.setdefault(d["product_id"], []).append(d)
        out: List[Reading] = []
        for pid, infos in by_pid.items():
            if pid in self.unsupported:
                continue
            infos.sort(key=lambda d: (d.get("interface_number", 0), d.get("usage_page", 0)))
            name = (infos[0].get("product_string") or f"Razer device {pid:04x}").strip()
            reading = self._read(pid, name, infos)
            if reading:
                out.append(reading)
        return out

    def _read(self, pid: int, name: str, infos: List[dict]) -> Optional[Reading]:
        key = f"razer-{pid:04x}"
        tries = [self.working[pid]] if pid in self.working else [
            (i, tid) for i in range(len(infos)) for tid in TRANSACTION_IDS]
        saw_asleep = False
        for i, tid in tries:
            if i >= len(infos):
                continue
            try:
                status, raw, charging = self._query(infos[i]["path"], tid)
            except (OSError, ValueError) as e:
                self.log.append(f"{pid:04x}#{i}: {e}")
                continue
            if status == ST_OK and raw is not None:
                self.working[pid] = (i, tid)
                r = Reading(key, name, guess_kind(name), raw_to_percent(raw), charging)
                self.last[pid] = r
                return r
            if status == ST_NO_RESPONSE:
                # The dongle is there but the device is off or asleep.
                self.working[pid] = (i, tid)
                saw_asleep = True
                break
        if saw_asleep or pid in self.last:
            prev = self.last.get(pid)
            return Reading(key, name, prev.kind if prev else guess_kind(name),
                           prev.level if prev else None, online=False, note="asleep or off")
        if pid not in self.working:
            self.unsupported.add(pid)
        return None
