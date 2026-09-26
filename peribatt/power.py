"""Notice when the PC wakes from sleep or hibernation.

Windows 8+ can call a function on suspend and resume
(``PowerRegisterSuspendResumeNotification`` with ``DEVICE_NOTIFY_CALLBACK``),
which needs no window and no message loop. The callback runs on a system
thread and must return quickly, so it only hands the event on.

The app also has a portable fallback (see ``App.poll_loop``): a wait that ends
far later than asked for means the machine was asleep in between.
"""
from __future__ import annotations

import ctypes
import logging
from typing import Callable, Optional

from .winapi import IS_WINDOWS

log = logging.getLogger("peribatt")

DEVICE_NOTIFY_CALLBACK = 2
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007       # resumed, a user is present
PBT_APMRESUMEAUTOMATIC = 0x0012     # resumed (always sent)
RESUME_EVENTS = (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC)

if IS_WINDOWS:
    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)
else:                               # only used for tests elsewhere
    CALLBACK = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)


class _SubscribeParams(ctypes.Structure):
    _fields_ = [("Callback", CALLBACK), ("Context", ctypes.c_void_p)]


class PowerWatcher:
    """Calls ``on_resume`` after sleep/hibernate and ``on_suspend`` before it."""

    def __init__(self, on_resume: Callable[[], None], on_suspend: Optional[Callable[[], None]] = None):
        self.on_resume = on_resume
        self.on_suspend = on_suspend
        self._handle = ctypes.c_void_p()
        # ctypes objects handed to Windows must outlive the registration.
        self._callback = CALLBACK(self._event)
        self._params = _SubscribeParams(self._callback, None)
        self.registered = False

    def _event(self, _context, event_type: int, _setting) -> int:
        try:
            if event_type in RESUME_EVENTS:
                self.on_resume()
            elif event_type == PBT_APMSUSPEND and self.on_suspend:
                self.on_suspend()
        except Exception:                       # never let an exception cross into Windows
            log.exception("power event handler failed")
        return 0

    def start(self) -> bool:
        if not IS_WINDOWS or self.registered:
            return self.registered
        try:
            powrprof = ctypes.WinDLL("powrprof")
            register = powrprof.PowerRegisterSuspendResumeNotification
            register.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            register.restype = ctypes.c_ulong
            err = register(DEVICE_NOTIFY_CALLBACK, ctypes.addressof(self._params), ctypes.byref(self._handle))
        except (OSError, AttributeError) as e:  # Windows 7, or powrprof missing
            log.info("sleep/resume notifications unavailable: %s", e)
            return False
        if err:
            log.info("PowerRegisterSuspendResumeNotification failed: %s", err)
            return False
        self.registered = True
        return True

    def stop(self) -> None:
        if not self.registered:
            return
        try:
            unregister = ctypes.WinDLL("powrprof").PowerUnregisterSuspendResumeNotification
            unregister.argtypes = [ctypes.c_void_p]
            unregister.restype = ctypes.c_ulong
            unregister(self._handle)
        except (OSError, AttributeError):
            pass
        self.registered = False
