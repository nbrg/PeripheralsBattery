"""Optional bridge to HeadsetControl (https://github.com/Sapd/HeadsetControl),
which knows the battery protocol of 100+ headsets (Logitech G-series, Corsair,
SteelSeries, Roccat, Audeze, ...). When ``headsetcontrol`` is on the PATH, or
its path is set in the settings, it is run as ``headsetcontrol -b -o json`` and
its devices are shown like any other.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from typing import Callable, Iterable, List, Optional, Set, Tuple, Union

from .model import HEADSET, Reading

log = logging.getLogger("peribatt")

CREATE_NO_WINDOW = 0x08000000


def _int(v) -> int:
    try:
        return int(v, 16) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        return 0


def parse(output: str, skip: Iterable[Tuple[int, int]] = ()) -> List[Reading]:
    try:
        doc = json.loads(output)
    except ValueError:
        return []
    skip = set(skip)
    out = []
    for d in doc.get("devices", []):
        vid, pid = _int(d.get("id_vendor")), _int(d.get("id_product"))
        if (vid, pid) in skip:
            continue                 # a native provider already reads this one
        bat = d.get("battery") or {}
        status = bat.get("status", "")
        level = bat.get("level", -1)
        name = d.get("device") or d.get("product") or "Headset"
        key = f"hsc-{vid:04x}-{pid:04x}"
        if status == "BATTERY_UNAVAILABLE":
            out.append(Reading(key, name, HEADSET, None, online=False, note="switched off"))
        elif status in ("BATTERY_AVAILABLE", "BATTERY_CHARGING"):
            out.append(Reading(key, name, HEADSET, level if 0 <= level <= 100 else None,
                               charging=status == "BATTERY_CHARGING"))
    return out


class HeadsetControlSource:
    name = "headsetcontrol"

    def __init__(self, exe: Union[str, Callable[[], str]] = "",
                 skip: Set[Tuple[int, int]] = frozenset(),
                 run: Optional[Callable[[List[str]], str]] = None):
        # A callable is read on every poll, so changing the path in the settings
        # works without restarting the app.
        self._exe = exe
        self.skip = set(skip)
        self._run = run or self._subprocess

    @property
    def exe(self) -> str:
        configured = self._exe() if callable(self._exe) else self._exe
        return (configured or "").strip().strip('"') or shutil.which("headsetcontrol") or ""

    @exe.setter
    def exe(self, value: str) -> None:
        self._exe = value

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def _subprocess(self, args: List[str]) -> str:
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(args, capture_output=True, text=True, timeout=15,
                             creationflags=flags)
        return res.stdout

    def poll(self) -> List[Reading]:
        if not self.available:
            return []
        try:
            return parse(self._run([self.exe, "-b", "-o", "json"]), self.skip)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("headsetcontrol: %s", e)
            return []
