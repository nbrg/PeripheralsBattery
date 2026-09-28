"""Thin seam over the ``hidapi`` package, so the protocol code can be tested
against fake devices and the app still starts when hidapi is missing."""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Protocol, Tuple

log = logging.getLogger("peribatt")

# hidapi's enumerate() briefly opens every HID device on the system - the keyboard
# included - to read its attributes, even when asked for one vendor. While the
# device watcher (devwatch) tells us when devices come and go, the answers are
# cached until the next change; without it (other systems, Windows 7) every call
# goes to hidapi. A long maximum age covers a missed notification.
CACHE_MAX_AGE = 600.0

_generation = 0
_watching = False
_cache_lock = threading.Lock()
_cache: Dict[int, Tuple[int, float, List[dict]]] = {}
stats = {"hidapi": 0, "cached": 0}


def device_list_changed() -> None:
    """A HID device came or went (or the PC woke up): read the list afresh."""
    global _generation
    with _cache_lock:
        _generation += 1
        _cache.clear()


def set_watching(on: bool) -> None:
    global _watching
    with _cache_lock:
        _watching = on
        _cache.clear()


class Handle(Protocol):
    def write(self, data) -> int: ...
    def read(self, max_length: int, timeout_ms: int = 0) -> List[int]: ...
    def close(self) -> None: ...


class HidApi:
    """The real thing. Every call is guarded: a missing or broken hidapi means
    "no devices", never a crash."""

    def __init__(self):
        self.error = ""
        try:
            import hid  # noqa: F401
            self._hid = hid
        except Exception as e:  # pragma: no cover - depends on the machine
            log.error("hidapi unavailable: %s", e)
            self.error = f"{type(e).__name__}: {e}"
            self._hid = None

    def enumerate(self, vendor_id: int) -> List[dict]:
        if self._hid is None:
            return []
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(vendor_id) if _watching else None
            gen = _generation
        if hit is not None and hit[0] == gen and now - hit[1] < CACHE_MAX_AGE:
            stats["cached"] += 1
            return [dict(d) for d in hit[2]]
        try:
            found = list(self._hid.enumerate(vendor_id, 0))
        except Exception as e:  # pragma: no cover
            log.warning("hid enumerate %04x failed: %s", vendor_id, e)
            return []
        stats["hidapi"] += 1
        with _cache_lock:
            if _watching and gen == _generation:        # no change arrived meanwhile
                _cache[vendor_id] = (gen, now, found)
        return [dict(d) for d in found]

    def open(self, path) -> Handle:
        if self._hid is None:
            raise OSError("hidapi unavailable")
        dev = self._hid.device()
        dev.open_path(path)
        # cython-hidapi's read(n, 0) means "no timeout": it calls the *blocking*
        # hid_read, which waits forever on a device with nothing to say - the
        # "drop stale replies" loops hung the whole poll on a quiet receiver.
        # Non-blocking mode makes timeout 0 return at once; reads with a real
        # timeout go through hid_read_timeout and still wait as asked.
        try:
            dev.set_nonblocking(True)
        except Exception:
            dev.close()
            raise
        return dev


def hexdump(data, limit: int = 24) -> str:
    return " ".join(f"{b:02x}" for b in list(data or [])[:limit])
