"""Thin seam over the ``hidapi`` package, so the protocol code can be tested
against fake devices and the app still starts when hidapi is missing."""
from __future__ import annotations

import logging
from typing import List, Protocol

log = logging.getLogger("peribatt")


class Handle(Protocol):
    def write(self, data) -> int: ...
    def read(self, max_length: int, timeout_ms: int = 0) -> List[int]: ...
    def close(self) -> None: ...


class HidApi:
    """The real thing. Every call is guarded: a missing or broken hidapi means
    "no devices", never a crash."""

    def __init__(self):
        try:
            import hid  # noqa: F401
            self._hid = hid
        except Exception as e:  # pragma: no cover - depends on the machine
            log.error("hidapi unavailable: %s", e)
            self._hid = None

    def enumerate(self, vendor_id: int) -> List[dict]:
        if self._hid is None:
            return []
        try:
            return list(self._hid.enumerate(vendor_id, 0))
        except Exception as e:  # pragma: no cover
            log.warning("hid enumerate %04x failed: %s", vendor_id, e)
            return []

    def open(self, path) -> Handle:
        if self._hid is None:
            raise OSError("hidapi unavailable")
        dev = self._hid.device()
        dev.open_path(path)
        return dev


def hexdump(data, limit: int = 24) -> str:
    return " ".join(f"{b:02x}" for b in list(data or [])[:limit])
