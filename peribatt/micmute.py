"""Windows microphone mute state through Core Audio, without pywin32/comtypes.

The headset's own capture endpoint is preferred (matched by name, e.g.
"HyperX"); otherwise the default communications microphone is used. This
catches a mic muted in Windows / from the keyboard, complementing the
hardware mute button that the headset protocol reports. It can also *toggle*
the mute, which the tray uses for a left-click on a headset icon.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable, Optional, Sequence

from .winapi import GUID, IS_WINDOWS, PROPERTYKEY, check, com_method, release

log = logging.getLogger("peribatt")

CLSID_MMDeviceEnumerator = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMMDeviceEnumerator = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_IAudioEndpointVolume = "5CDF2C82-841E-4546-9722-0CF74078229A"
PKEY_FriendlyName = ("a45c254e-df1c-4efd-8020-67d146a850e0", 14)
E_CAPTURE, E_COMMUNICATIONS, DEVICE_STATE_ACTIVE = 1, 2, 1
CLSCTX_ALL, STGM_READ, VT_LPWSTR = 0x17, 0, 31

# vtable slots
ENUM_ENDPOINTS, ENUM_DEFAULT = 3, 4
COLL_COUNT, COLL_ITEM = 3, 4
DEV_ACTIVATE, DEV_PROPS = 3, 4
PROPS_GETVALUE = 5
VOL_SETMUTE, VOL_GETMUTE = 14, 15


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


class CoreAudioMic:
    """Holds an IAudioEndpointVolume pointer; must be used from one thread."""

    def __init__(self, prefer: Sequence[str] = ("hyperx", "cloud flight")):
        self.prefer = [p.lower() for p in prefer]
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoInitializeEx(None, 0)            # MTA; S_FALSE if already done
        self.vol = ctypes.c_void_p()
        self.name = ""

    def _enumerator(self) -> ctypes.c_void_p:
        enum = ctypes.c_void_p()
        hr = self.ole32.CoCreateInstance(ctypes.byref(GUID.of(CLSID_MMDeviceEnumerator)), None,
                                         CLSCTX_ALL, ctypes.byref(GUID.of(IID_IMMDeviceEnumerator)),
                                         ctypes.byref(enum))
        check(hr, "CoCreateInstance(MMDeviceEnumerator)")
        return enum

    def _name(self, device) -> str:
        store = ctypes.c_void_p()
        if com_method(device, DEV_PROPS, ctypes.c_uint, ctypes.c_void_p)(STGM_READ, ctypes.byref(store)) < 0:
            return ""
        try:
            pv = _PropVariant()
            key = PROPERTYKEY.of(*PKEY_FriendlyName)
            hr = com_method(store, PROPS_GETVALUE, ctypes.c_void_p, ctypes.c_void_p)(
                ctypes.byref(key), ctypes.byref(pv))
            if hr < 0 or pv.vt != VT_LPWSTR or not pv.ptr:
                return ""
            name = ctypes.wstring_at(pv.ptr)
            self.ole32.PropVariantClear(ctypes.byref(pv))
            return name
        finally:
            release(store)

    def _pick(self, enum) -> ctypes.c_void_p:
        coll = ctypes.c_void_p()
        check(com_method(enum, ENUM_ENDPOINTS, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p)(
            E_CAPTURE, DEVICE_STATE_ACTIVE, ctypes.byref(coll)), "EnumAudioEndpoints")
        try:
            count = ctypes.c_uint()
            com_method(coll, COLL_COUNT, ctypes.c_void_p)(ctypes.byref(count))
            for i in range(count.value):
                dev = ctypes.c_void_p()
                if com_method(coll, COLL_ITEM, ctypes.c_uint, ctypes.c_void_p)(i, ctypes.byref(dev)) < 0:
                    continue
                name = self._name(dev)
                if any(p in name.lower() for p in self.prefer):
                    self.name = name
                    return dev
                release(dev)
        finally:
            release(coll)
        dev = ctypes.c_void_p()
        check(com_method(enum, ENUM_DEFAULT, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)(
            E_CAPTURE, E_COMMUNICATIONS, ctypes.byref(dev)), "GetDefaultAudioEndpoint")
        self.name = self._name(dev)
        return dev

    def resolve(self) -> None:
        release(self.vol)
        self.vol = ctypes.c_void_p()
        enum = self._enumerator()
        try:
            dev = self._pick(enum)
            try:
                vol = ctypes.c_void_p()
                check(com_method(dev, DEV_ACTIVATE, ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_void_p, ctypes.c_void_p)(
                    ctypes.byref(GUID.of(IID_IAudioEndpointVolume)), CLSCTX_ALL, None,
                    ctypes.byref(vol)), "Activate(IAudioEndpointVolume)")
                self.vol = vol
            finally:
                release(dev)
        finally:
            release(enum)

    def muted(self) -> bool:
        if not self.vol:
            self.resolve()
        flag = ctypes.c_int()
        check(com_method(self.vol, VOL_GETMUTE, ctypes.c_void_p)(ctypes.byref(flag)), "GetMute")
        return bool(flag.value)

    def set_muted(self, on: bool) -> None:
        if not self.vol:
            self.resolve()
        check(com_method(self.vol, VOL_SETMUTE, ctypes.c_int, ctypes.c_void_p)(int(on), None),
              "SetMute")


class MicMuteWatcher:
    """Polls the mute flag once a second on its own thread (COM objects stay
    on the thread that made them) and reports changes through ``on_change``.
    Toggle requests are handed to that same thread."""

    def __init__(self, on_change: Callable[[bool], None], interval: float = 1.0,
                 factory: Optional[Callable[[], object]] = None,
                 wanted: Callable[[], bool] = lambda: True):
        self.on_change = on_change
        self.wanted = wanted                  # skip the check while nobody needs it
        self.interval = interval
        self.factory = factory or (CoreAudioMic if IS_WINDOWS else None)
        self.state: Optional[bool] = None
        self._toggle = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def supported(self) -> bool:
        return self.factory is not None

    def start(self) -> None:
        if self.supported and self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="micmute")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def toggle(self) -> None:
        self._toggle.set()

    def _run(self) -> None:
        try:
            mic = self.factory()
        except OSError as e:
            log.warning("mic mute unavailable: %s", e)
            return
        last_resolve = time.monotonic()
        while not self._stop.is_set():
            if not self.wanted() and not self._toggle.is_set():
                self._toggle.wait(self.interval * 5)
                continue
            try:
                if time.monotonic() - last_resolve > 30:     # follow device changes
                    mic.resolve()
                    last_resolve = time.monotonic()
                if self._toggle.is_set():
                    self._toggle.clear()
                    mic.set_muted(not mic.muted())
                self.step(mic.muted())
            except OSError as e:
                log.debug("mic mute: %s", e)
                try:
                    mic.resolve()
                except OSError:
                    pass
            self._toggle.wait(self.interval)

    def step(self, muted: bool) -> None:
        if muted != self.state:
            self.state = muted
            self.on_change(muted)
