"""The one data type every part of the app agrees on."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

MOUSE = "mouse"
HEADSET = "headset"
KEYBOARD = "keyboard"
DEVICE = "device"


@dataclass(frozen=True)
class Reading:
    """A snapshot of one device. One tray icon is shown per distinct ``key``."""

    key: str                      # stable id, e.g. "logi-1a2b3c4d" or "hyperx-cfs"
    name: str                     # what the user sees in the tooltip
    kind: str = DEVICE            # mouse / headset / keyboard / device
    level: Optional[int] = None   # 0..100, None when unknown
    charging: bool = False
    online: bool = True           # False: switched off, asleep or receiver unplugged
    muted: bool = False           # headset microphone muted
    note: str = ""                # short extra detail for the tooltip ("receiver unplugged")

    def with_(self, **changes) -> "Reading":
        return replace(self, **changes)


def merge(readings) -> list:
    """Collapse readings that share a key (the same mouse seen on the cable and
    through its receiver). An online copy beats an offline one, and a charging
    copy beats a discharging one."""
    best = {}
    for r in readings:
        cur = best.get(r.key)
        if cur is None or (r.online, r.charging, r.level is not None) > (
                cur.online, cur.charging, cur.level is not None):
            best[r.key] = r
    return list(best.values())
