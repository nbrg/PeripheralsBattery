"""Xbox-compatible controllers and the headsets plugged into them, through
XInput. That covers Xbox pads, most 2.4 GHz third-party pads that pretend to
be one, and Xbox headsets (Turtle Beach, Razer Kaira, ...) on a controller.

XInput only reports four coarse levels, so the tooltip says "about".
"""
from __future__ import annotations

import ctypes
import logging
from typing import Callable, List, Optional, Tuple

from .model import HEADSET, Reading
from .winapi import IS_WINDOWS

log = logging.getLogger("peribatt")

DEVTYPE_GAMEPAD, DEVTYPE_HEADSET = 0, 1
TYPE_DISCONNECTED, TYPE_WIRED, TYPE_ALKALINE, TYPE_NIMH, TYPE_UNKNOWN = 0, 1, 2, 3, 0xFF
LEVELS = {0: 5, 1: 25, 2: 60, 3: 100}          # empty / low / medium / full
GAMEPAD = "gamepad"


class _Info(ctypes.Structure):
    _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]


def _load():
    for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            lib = ctypes.WinDLL(dll)
            fn = lib.XInputGetBatteryInformation
            fn.argtypes = [ctypes.c_uint, ctypes.c_ubyte, ctypes.POINTER(_Info)]
            fn.restype = ctypes.c_uint
            return fn
        except (OSError, AttributeError):
            continue
    return None


def to_reading(user: int, devtype: int, btype: int, blevel: int) -> Optional[Reading]:
    if btype in (TYPE_DISCONNECTED, TYPE_WIRED):
        return None                      # nothing plugged in / no battery to report
    what = "Controller" if devtype == DEVTYPE_GAMEPAD else "Controller headset"
    return Reading(f"xinput-{user}-{devtype}", f"{what} {user + 1}",
                   GAMEPAD if devtype == DEVTYPE_GAMEPAD else HEADSET,
                   LEVELS.get(blevel), note="approximate")


class XInputSource:
    name = "xinput"

    def __init__(self, query: Optional[Callable[[int, int], Optional[Tuple[int, int]]]] = None):
        self._query = query
        if query is None and IS_WINDOWS:
            fn = _load()
            if fn:
                def q(user, devtype, fn=fn):
                    info = _Info()
                    if fn(user, devtype, ctypes.byref(info)) != 0:
                        return None
                    return info.BatteryType, info.BatteryLevel
                self._query = q

    def poll(self) -> List[Reading]:
        if self._query is None:
            return []
        out = []
        for user in range(4):
            for devtype in (DEVTYPE_GAMEPAD, DEVTYPE_HEADSET):
                res = self._query(user, devtype)
                if res:
                    r = to_reading(user, devtype, *res)
                    if r:
                        out.append(r)
        return out


