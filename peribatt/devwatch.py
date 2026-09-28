"""Notice HID devices being plugged in or removed - without polling for them.

Windows 8+ calls a function whenever a device interface of a class arrives or
goes (``CM_Register_Notification`` with a device-interface filter for the HID
class). The app uses that for two things:

* the list of HID devices only has to be read again after such an event, so
  the per-poll ``hid.enumerate()`` calls - which briefly open every HID device
  on the system, the keyboard included - are answered from a cache
  (see :mod:`peribatt.hidio`);
* a receiver plugged in, or a mouse put on its cable, shows up a moment later
  instead of at the next scheduled poll.

The callback runs on a system thread and must return quickly: it bumps a
counter and arms a short timer (a dongle brings several interfaces in a burst,
and Windows needs a moment to finish setting them up).
"""
from __future__ import annotations

import ctypes
import logging
import threading
from typing import Callable, Optional

from . import hidio
from .winapi import GUID, IS_WINDOWS

log = logging.getLogger("peribatt")

GUID_DEVINTERFACE_HID = "4D1E55B2-F16F-11CF-88CB-001111000030"
# Bluetooth headphones bring no HID interface when they connect, but an audio one.
KSCATEGORY_AUDIO = "6994AD04-93EF-11D0-A3CC-00A0C9223196"
WATCHED = (GUID_DEVINTERFACE_HID, KSCATEGORY_AUDIO)
CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE = 0
ACTION_ARRIVAL, ACTION_REMOVAL = 0, 1
MAX_DEVICE_ID_LEN = 200
SETTLE = 1.5                  # seconds after the last event before the app looks

if IS_WINDOWS:
    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                  ctypes.c_void_p, ctypes.c_ulong)
else:                         # only used by tests elsewhere
    CALLBACK = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                ctypes.c_void_p, ctypes.c_ulong)


class _FilterUnion(ctypes.Union):
    _fields_ = [("ClassGuid", GUID), ("hTarget", ctypes.c_void_p),
                ("InstanceId", ctypes.c_uint16 * MAX_DEVICE_ID_LEN)]     # WCHAR[200]


class CM_NOTIFY_FILTER(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("Flags", ctypes.c_uint32),          # DWORDs
                ("FilterType", ctypes.c_uint32), ("Reserved", ctypes.c_uint32),
                ("u", _FilterUnion)]


class DeviceWatcher:
    """Calls ``on_change`` (from a timer thread) once a burst of HID plug/unplug
    events has settled. Every event also invalidates the HID device list cache."""

    def __init__(self, on_change: Callable[[], None], settle: float = SETTLE):
        self.on_change = on_change
        self.settle = settle
        self.events = 0
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._handles: list = []
        self._callback = CALLBACK(self._event)      # must outlive the registration
        self.registered = False

    def _event(self, _notify, _context, action, _data, _size) -> int:
        try:
            if action in (ACTION_ARRIVAL, ACTION_REMOVAL):
                self.changed()
        except Exception:                           # never let an exception cross into Windows
            log.exception("device event handler failed")
        return 0

    def changed(self) -> None:
        """A HID device came or went (also callable directly, e.g. from tests)."""
        hidio.device_list_changed()
        with self._lock:
            self.events += 1
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.settle, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            self.on_change()
        except Exception:
            log.exception("device change handler failed")

    def start(self) -> bool:
        if not IS_WINDOWS or self.registered:
            return self.registered
        try:
            cfgmgr = ctypes.WinDLL("cfgmgr32")
            register = cfgmgr.CM_Register_Notification
            register.argtypes = [ctypes.POINTER(CM_NOTIFY_FILTER), ctypes.c_void_p, CALLBACK,
                                 ctypes.POINTER(ctypes.c_void_p)]
            register.restype = ctypes.c_ulong
        except (OSError, AttributeError) as e:      # Windows 7
            log.info("device notifications unavailable: %s", e)
            return False
        for guid in WATCHED:
            flt = CM_NOTIFY_FILTER(cbSize=ctypes.sizeof(CM_NOTIFY_FILTER),
                                   FilterType=CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE)
            flt.u.ClassGuid = GUID.of(guid)
            handle = ctypes.c_void_p()
            err = register(ctypes.byref(flt), None, self._callback, ctypes.byref(handle))
            if err:
                log.info("CM_Register_Notification(%s) failed: %s", guid, err)
            else:
                self._handles.append(handle)
        if not self._handles:
            return False
        self.registered = True
        hidio.set_watching(True)                    # the cache can trust the events now
        return True

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
        if not self.registered:
            return
        hidio.set_watching(False)
        try:
            unregister = ctypes.WinDLL("cfgmgr32").CM_Unregister_Notification
            unregister.argtypes = [ctypes.c_void_p]
            unregister.restype = ctypes.c_ulong
            for handle in self._handles:
                unregister(handle)
        except (OSError, AttributeError):
            pass
        self._handles = []
        self.registered = False
