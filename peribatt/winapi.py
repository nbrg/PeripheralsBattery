"""Small ctypes helpers shared by the Windows-only modules. Importing this on
another OS is fine; only calling into it is not."""
from __future__ import annotations

import ctypes
import sys
import uuid

IS_WINDOWS = sys.platform == "win32"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "GUID":
        u = uuid.UUID(text)
        g = cls(u.time_low, u.time_mid, u.time_hi_version)
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g

    def __str__(self) -> str:
        tail = bytes(self.Data4)
        return str(uuid.UUID(fields=(self.Data1, self.Data2, self.Data3,
                                     tail[0], tail[1], int.from_bytes(tail[2:], "big"))))


class PROPERTYKEY(ctypes.Structure):
    """Also DEVPROPKEY: the two have the same layout."""
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    @classmethod
    def of(cls, fmtid: str, pid: int) -> "PROPERTYKEY":
        return cls(GUID.of(fmtid), pid)


def com_method(obj: ctypes.c_void_p, index: int, *argtypes, restype=ctypes.c_long):
    """Bound COM method number ``index`` of interface pointer ``obj``. With the
    default HRESULT-as-long restype the caller checks the result itself."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = proto(vtable[index])
    return lambda *args: fn(obj, *args)


def release(obj) -> None:
    if obj:
        com_method(obj, 2, restype=ctypes.c_ulong)()


def check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08x}")
