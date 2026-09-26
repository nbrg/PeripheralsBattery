"""Bluetooth devices whose battery level Windows already knows - headphones
using the hands-free profile and Bluetooth LE mice/keyboards with a battery
service. This is the level shown in Settings > Bluetooth & devices.

Windows stores it as a device property, DEVPKEY_Bluetooth_Battery
({104EA319-6EE2-4701-BD47-8DDBF425BBE5}, 2), on one of the device's PnP nodes.
Those nodes are listed with SetupAPI; nodes that belong to the same physical
device share a container id, so each device is reported once.
"""
from __future__ import annotations

import ctypes
import logging
import re
from ctypes import wintypes
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .model import DEVICE, HEADSET, KEYBOARD, MOUSE, Reading
from .winapi import GUID, IS_WINDOWS, PROPERTYKEY

log = logging.getLogger("peribatt")

KEY_BATTERY = PROPERTYKEY.of("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)
KEY_FRIENDLY = PROPERTYKEY.of("a45c254e-df1c-4efd-8020-67d146a850e0", 14)
KEY_CONTAINER = PROPERTYKEY.of("8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c", 2)
KEY_DN_STATUS = PROPERTYKEY.of("4340a6c5-93fa-4706-972c-7b648008a5a7", 2)
DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
DN_STARTED = 0x8
TYPE_BYTE, TYPE_UINT32, TYPE_GUID, TYPE_STRING = 0x03, 0x07, 0x0D, 0x12

_SUFFIXES = re.compile(r"\s+(hands-?free( ag)?( audio)?|stereo|avrcp transport|a2dp( sink)?|"
                       r"le)$", re.I)


def clean_name(name: str) -> str:
    prev = None
    while prev != name:
        prev, name = name, _SUFFIXES.sub("", name).strip()
    return name or "Bluetooth device"


KEYBOARD_WORDS = ("keyboard", "keychron", "nuphy", "mx keys", "k380", "k780", "k585", "k860",
                  "keys", "magic keyboard", "hhkb", "epomaker", "akko", "royal kludge", "rk61",
                  "rk84", "lofree", "ducky", "varmilo", "anne pro")
MOUSE_WORDS = ("mouse", "mx master", "mx anywhere", "mx ergo", "trackball", "pebble", "m720",
               "m590", "magic mouse", "trackpad", "viper", "basilisk", "deathadder")
HEADSET_WORDS = ("head", "bud", "ear", "pods", "wh-", "wf-", "jabra", "bose", "sony", "jbl",
                 "audio", "sound", "turtle", "stealth", "arctis", "hyperx", "sennheiser",
                 "momentum", "soundcore", "beats")


def guess_kind(name: str) -> str:
    n = name.lower()
    # Keyboards first: "Keychron K8 Pro" must not match a headset word by accident.
    for words, kind in ((KEYBOARD_WORDS, KEYBOARD), (MOUSE_WORDS, MOUSE), (HEADSET_WORDS, HEADSET)):
        if any(w in n for w in words):
            return kind
    if re.search(r"\bk\d{1,2}\b", n):          # Keychron-style model numbers: K2, K8, K10...
        return KEYBOARD
    return DEVICE


# Each raw record: (container id, friendly name, battery %, started)
Record = Tuple[str, str, int, bool]


def to_readings(records: Iterable[Record]) -> List[Reading]:
    best: Dict[str, Record] = {}
    for rec in records:
        cid, name, level, started = rec
        if not 0 <= level <= 100:
            continue
        cur = best.get(cid)
        if cur is None or (started, len(name) < len(cur[1])) > (cur[3], False):
            best[cid] = rec
    out = []
    for cid, (_, name, level, started) in best.items():
        nice = clean_name(name)
        out.append(Reading(f"bt-{cid.strip('{}').lower()}", nice, guess_kind(nice), level,
                           online=started, note="" if started else "disconnected"))
    return out


class _SetupApi:
    def __init__(self):
        api = ctypes.WinDLL("setupapi", use_last_error=True)

        class SP_DEVINFO_DATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
                        ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

        self.SP_DEVINFO_DATA = SP_DEVINFO_DATA
        api.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                             wintypes.HWND, wintypes.DWORD]
        api.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        api.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(SP_DEVINFO_DATA)]
        api.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
        api.SetupDiGetDevicePropertyW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(PROPERTYKEY),
            ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        api.SetupDiGetDevicePropertyW.restype = wintypes.BOOL
        api.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        self.api = api

    def _prop(self, hdev, data, key: PROPERTYKEY):
        ptype = wintypes.ULONG()
        buf = ctypes.create_string_buffer(512)
        size = wintypes.DWORD()
        ok = self.api.SetupDiGetDevicePropertyW(hdev, ctypes.byref(data), ctypes.byref(key),
                                                ctypes.byref(ptype), buf, len(buf),
                                                ctypes.byref(size), 0)
        if not ok:
            return None
        t = ptype.value
        if t == TYPE_BYTE:
            return buf.raw[0]
        if t == TYPE_UINT32:
            return int.from_bytes(buf.raw[:4], "little")
        if t == TYPE_STRING:
            return ctypes.wstring_at(buf)
        if t == TYPE_GUID:
            return str(GUID.from_buffer_copy(buf.raw[:ctypes.sizeof(GUID)]))
        return None

    def records(self) -> List[Record]:
        hdev = self.api.SetupDiGetClassDevsW(None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
        if not hdev or hdev == ctypes.c_void_p(-1).value:
            return []
        out: List[Record] = []
        try:
            data = self.SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(self.SP_DEVINFO_DATA)
            i = 0
            while self.api.SetupDiEnumDeviceInfo(hdev, i, ctypes.byref(data)):
                i += 1
                level = self._prop(hdev, data, KEY_BATTERY)
                if level is None:
                    continue
                name = self._prop(hdev, data, KEY_FRIENDLY) or "Bluetooth device"
                cid = self._prop(hdev, data, KEY_CONTAINER) or f"node{i}"
                status = self._prop(hdev, data, KEY_DN_STATUS) or 0
                out.append((cid, name, int(level), bool(status & DN_STARTED)))
        finally:
            self.api.SetupDiDestroyDeviceInfoList(hdev)
        return out


class BluetoothSource:
    name = "bluetooth"

    def __init__(self, records: Optional[Callable[[], List[Record]]] = None):
        self._records = records
        if self._records is None and IS_WINDOWS:
            try:
                self._records = _SetupApi().records
            except OSError as e:  # pragma: no cover - Windows only
                log.warning("bluetooth: %s", e)

    def poll(self) -> List[Reading]:
        if self._records is None:
            return []
        try:
            return to_readings(self._records())
        except OSError as e:  # pragma: no cover
            log.warning("bluetooth poll: %s", e)
            return []
