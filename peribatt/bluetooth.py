"""Bluetooth devices whose battery level Windows already knows - headphones
using the hands-free profile and Bluetooth LE mice/keyboards with a battery
service. This is the level shown in Settings > Bluetooth & devices.

Windows stores it as a device property, DEVPKEY_Bluetooth_Battery
({104EA319-6EE2-4701-BD47-8DDBF425BBE5}, 2), on one of the device's PnP nodes.
Those nodes are listed with SetupAPI; nodes that belong to the same physical
device share a container id, so each device is reported once.

Whether a device is connected *right now*, and what it is, comes from the Win32
Bluetooth API (``BluetoothFindFirstDevice`` without an inquiry, so no radio scan):
for classic devices it reports ``fConnected`` and the Class of Device (headset,
keyboard, mouse, gamepad). Devices are matched to their PnP nodes by the MAC
address in the node's instance id. Bluetooth LE devices are not in that list;
for them the node's "started" state is used.
"""
from __future__ import annotations

import ctypes
import logging
import re
from ctypes import wintypes
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .model import DEVICE, HEADSET, KEYBOARD, MOUSE, Reading
from .render import GAMEPAD
from .winapi import GUID, IS_WINDOWS, PROPERTYKEY

log = logging.getLogger("peribatt")

KEY_BATTERY = PROPERTYKEY.of("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)
KEY_FRIENDLY = PROPERTYKEY.of("a45c254e-df1c-4efd-8020-67d146a850e0", 14)
KEY_CONTAINER = PROPERTYKEY.of("8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c", 2)
KEY_DN_STATUS = PROPERTYKEY.of("4340a6c5-93fa-4706-972c-7b648008a5a7", 2)
KEY_INSTANCE_ID = PROPERTYKEY.of("78c34fc8-104a-4aca-9ea4-524d52996e57", 256)
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


# Each raw record: (container id, friendly name, battery %, started[, MAC address])
Record = Tuple[str, str, int, bool]

# BTHENUM\DEV_A0B1C2D3E4F5\..., ...&0&A0B1C2D3E4F5_C00000000, BTHLEDEVICE\..._a0b1c2d3e4f5\...
# (and not the last group of a GUID, which is preceded by "-")
_MAC = re.compile(r"(?:DEV_|&|_)([0-9A-F]{12})(?=[_\\]|$)", re.I)


def mac_of(instance_id: str) -> str:
    m = _MAC.search(instance_id or "")
    return m.group(1).upper() if m else ""


def kind_from_class(cod: int) -> str:
    """Bluetooth Class of Device -> a picture. Major class 4 is audio/video,
    5 is a peripheral whose minor bits say keyboard / pointing device / gamepad."""
    major, minor = (cod >> 8) & 0x1F, (cod >> 2) & 0x3F
    if major == 0x04:
        return HEADSET
    if major == 0x05:
        if minor & 0x0F in (0x01, 0x02):                 # joystick, gamepad
            return GAMEPAD
        return {1: KEYBOARD, 2: MOUSE, 3: KEYBOARD}.get(minor >> 4, DEVICE)
    return DEVICE


class Classic:
    """What the Bluetooth API says about one classic device."""
    __slots__ = ("connected", "cod", "name")

    def __init__(self, connected: bool, cod: int, name: str = ""):
        self.connected, self.cod, self.name = connected, cod, name


GENERIC_NAME = re.compile(r"bluetooth|generic|gatt|attribute|service|hands-?free|avrcp|"
                          r"hid device|hid-compliant|le device|audio gateway|^(hid|le|bt)$", re.I)


def _rank(rec: Record) -> tuple:
    """Which of a device's nodes names it best: connected first, then a real name
    ("Keychron K8 Pro") over a generic one ("Bluetooth LE Generic Attribute Service"),
    then the shorter one."""
    _, name, _, started = rec
    return (started, not GENERIC_NAME.search(clean_name(name)), -len(name))


def to_readings(records: Iterable[Record], classic: Optional[Dict[str, Classic]] = None) -> List[Reading]:
    classic = classic or {}
    best: Dict[str, tuple] = {}
    macs: Dict[str, str] = {}
    for rec in records:
        cid, name, level, started = rec[:4]
        mac = rec[4] if len(rec) > 4 else ""
        if mac:
            macs.setdefault(cid, mac)
        if not 0 <= level <= 100:
            continue
        cur = best.get(cid)
        if cur is None or _rank(rec[:4]) > _rank(cur):
            best[cid] = rec[:4]
    out = []
    for cid, (_, name, level, started) in best.items():
        nice = clean_name(name)
        info = classic.get(macs.get(cid, ""))
        online = info.connected if info is not None else started
        kind = kind_from_class(info.cod) if info is not None else DEVICE
        if kind == DEVICE:
            kind = guess_kind(nice)
        out.append(Reading(f"bt-{cid.strip('{}').lower()}", nice, kind, level,
                           online=online, note="" if online else "disconnected"))
    return out


def _words(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def same_device(a: str, b: str) -> bool:
    """"Keychron K8 Pro" vs "K8 Pro": the same thing? Equal names always are; a name
    inside another only when both are long enough not to match by accident
    ("G Pro" must not swallow "Logitech G Pro X Wireless")."""
    a, b = _words(a), _words(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 6 and (a in b or b in a)


def drop_twins(readings: List[Reading]) -> List[Reading]:
    """A device read over USB/HID *and* listed by Windows' Bluetooth battery (a
    mouse paired over Bluetooth while its dongle is also plugged in, a headset on
    both links): keep the HID reading - it knows charging and mute - and drop the
    Bluetooth copy. Only online HID readings count."""
    hid_names = [r.name for r in readings if not r.key.startswith("bt-") and r.online]
    return [r for r in readings
            if not (r.key.startswith("bt-") and any(same_device(r.name, n) for n in hid_names))]


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
                mac = mac_of(self._prop(hdev, data, KEY_INSTANCE_ID) or "")
                out.append((cid, name, int(level), bool(status & DN_STARTED), mac))
        finally:
            self.api.SetupDiDestroyDeviceInfoList(hdev)
        return out


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("fReturnAuthenticated", ctypes.c_int32),
                ("fReturnRemembered", ctypes.c_int32), ("fReturnUnknown", ctypes.c_int32),
                ("fReturnConnected", ctypes.c_int32), ("fIssueInquiry", ctypes.c_int32),
                ("cTimeoutMultiplier", ctypes.c_ubyte), ("hRadio", ctypes.c_void_p)]


class _SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint16) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay",
                                                  "wHour", "wMinute", "wSecond", "wMilliseconds")]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("Address", ctypes.c_uint64),
                ("ulClassofDevice", ctypes.c_uint32), ("fConnected", ctypes.c_int32),
                ("fRemembered", ctypes.c_int32), ("fAuthenticated", ctypes.c_int32),
                ("stLastSeen", _SYSTEMTIME), ("stLastUsed", _SYSTEMTIME),
                ("szName", ctypes.c_uint16 * 248)]                                   # WCHAR[248]


def classic_devices() -> Dict[str, Classic]:  # pragma: no cover - Windows only
    """MAC -> Classic for every paired classic Bluetooth device. No radio inquiry."""
    try:
        api = ctypes.WinDLL("bthprops.cpl")
    except OSError:
        return {}
    api.BluetoothFindFirstDevice.argtypes = [ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS),
                                             ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
    api.BluetoothFindFirstDevice.restype = ctypes.c_void_p
    api.BluetoothFindNextDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
    api.BluetoothFindNextDevice.restype = ctypes.c_int32
    api.BluetoothFindDeviceClose.argtypes = [ctypes.c_void_p]
    params = BLUETOOTH_DEVICE_SEARCH_PARAMS(dwSize=ctypes.sizeof(BLUETOOTH_DEVICE_SEARCH_PARAMS),
                                            fReturnAuthenticated=1, fReturnRemembered=1,
                                            fReturnConnected=1, fIssueInquiry=0)
    info = BLUETOOTH_DEVICE_INFO(dwSize=ctypes.sizeof(BLUETOOTH_DEVICE_INFO))
    out: Dict[str, Classic] = {}
    handle = api.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
    if not handle:
        return out                                   # no radio, or nothing paired
    try:
        while True:
            name = bytes(info.szName).decode("utf-16-le", "replace").split("\0", 1)[0]
            out[f"{info.Address:012X}"] = Classic(bool(info.fConnected), info.ulClassofDevice, name)
            info = BLUETOOTH_DEVICE_INFO(dwSize=ctypes.sizeof(BLUETOOTH_DEVICE_INFO))
            if not api.BluetoothFindNextDevice(handle, ctypes.byref(info)):
                break
    finally:
        api.BluetoothFindDeviceClose(handle)
    return out


class BluetoothSource:
    name = "bluetooth"

    def __init__(self, records: Optional[Callable[[], List[Record]]] = None,
                 classic: Optional[Callable[[], Dict[str, Classic]]] = None):
        self._records = records
        self._classic = classic
        if self._records is None and IS_WINDOWS:
            try:
                self._records = _SetupApi().records
                self._classic = classic or classic_devices
            except OSError as e:  # pragma: no cover - Windows only
                log.warning("bluetooth: %s", e)

    def poll(self) -> List[Reading]:
        if self._records is None:
            return []
        try:
            records = self._records()
        except OSError as e:  # pragma: no cover
            log.warning("bluetooth poll: %s", e)
            return []
        classic: Dict[str, Classic] = {}
        if self._classic is not None:
            try:
                classic = self._classic()
            except (OSError, ValueError) as e:  # pragma: no cover
                log.info("bluetooth connection state: %s", e)
        return to_readings(records, classic)
