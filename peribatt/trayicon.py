"""The Windows side of a tray icon, where pystray's defaults get in the way.

Three changes to pystray's Windows backend (0.19.x), all in its private methods:

* **A stable id per device.** Windows remembers whether the user pinned an icon to
  the taskbar or left it in the ^ overflow by the program's path and the icon's id.
  pystray uses ``id(self)``, a memory address that changes on every start, so a
  pinned icon went back to the overflow after each reboot. The id is now derived
  from the device key.
* **Icons built in memory.** pystray saves every new image to a temporary .ico
  file and loads it back - four files a second while a muted headset blinks. The
  icon is now made straight from the image's pixels (CreateIconIndirect, at the
  exact tray size, so nothing is rescaled), and the handles are kept, so a blink
  cycle is built once.
* **A failed NIM_ADD is noticed.** At logon the app can start before the taskbar is
  ready; Windows then refuses the icon and pystray ignores the refusal, leaving a
  running app with no icon. The add is retried for a while, and logged.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import zlib
from collections import OrderedDict
from typing import Callable, Optional

log = logging.getLogger("peribatt")

HANDLE_CACHE = 48          # icon handles kept per tray icon (a blink or theme is a few)
ADD_RETRIES = 20           # NIM_ADD attempts after a refusal...
ADD_RETRY_SECONDS = 3.0    # ...this far apart (a minute in all)


def icon_uid(key: str) -> int:
    """The id Windows knows the icon by: the same for a device on every start."""
    return zlib.crc32(f"peribatt:{key}".encode("utf-8")) & 0x7FFFFFFF or 1


def bgra_rows(image) -> bytes:
    """The image as 32-bit BGRA rows, top row first - a DIB section's layout."""
    rgba = image.convert("RGBA")
    r, g, b, a = rgba.split()
    from PIL import Image
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


class HandleCache:
    """image -> icon handle, most recently used last. The cache owns the handles:
    it destroys the ones it drops. It holds the images too, so an ``id`` is never
    reused for a different image while its entry exists."""

    def __init__(self, make: Callable[[object], int], destroy: Callable[[int], None],
                 size: int = HANDLE_CACHE):
        self.make, self.destroy, self.size = make, destroy, size
        self._items: "OrderedDict[int, tuple]" = OrderedDict()
        self.made = 0

    def get(self, image) -> int:
        hit = self._items.get(id(image))
        if hit is not None and hit[0] is image:
            self._items.move_to_end(id(image))
            return hit[1]
        handle = self.make(image)
        self.made += 1
        self._items[id(image)] = (image, handle)
        while len(self._items) > self.size:
            _, (_, old) = self._items.popitem(last=False)
            self._destroy(old)
        return handle

    def clear(self) -> None:
        items, self._items = self._items, OrderedDict()
        for _, handle in items.values():
            self._destroy(handle)

    def _destroy(self, handle: int) -> None:
        try:
            self.destroy(handle)
        except Exception as e:                      # pragma: no cover - Windows only
            log.debug("destroy icon: %s", e)


# --- Win32 (Windows only) ---------------------------------------------------------

def _win32():                                       # pragma: no cover - Windows only
    from ctypes import wintypes
    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    user32.CreateIconIndirect.restype = wintypes.HICON
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
    gdi32.CreateBitmap.restype = wintypes.HBITMAP
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    return user32, gdi32, BITMAPINFOHEADER, ICONINFO


_W32 = None


def create_hicon(image) -> int:                     # pragma: no cover - Windows only
    """An HICON with the image's exact pixels and alpha, without a file."""
    global _W32
    if _W32 is None:
        _W32 = _win32()
    user32, gdi32, BITMAPINFOHEADER, ICONINFO = _W32
    w, h = image.size
    header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                              biPlanes=1, biBitCount=32, biCompression=0)       # BI_RGB, top-down
    bits = ctypes.c_void_p()
    hdc = user32.GetDC(None)
    try:
        color = gdi32.CreateDIBSection(hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    finally:
        user32.ReleaseDC(None, hdc)
    if not color:
        raise ctypes.WinError()
    mask_bits = ctypes.create_string_buffer(((w + 15) // 16) * 2 * h)             # all 0: alpha decides
    mask = gdi32.CreateBitmap(w, h, 1, 1, mask_bits)
    try:
        data = bgra_rows(image)
        ctypes.memmove(bits, data, len(data))
        info = ICONINFO(fIcon=True, hbmMask=mask, hbmColor=color)
        hicon = user32.CreateIconIndirect(ctypes.byref(info))
        if not hicon:
            raise ctypes.WinError()
        return hicon
    finally:
        gdi32.DeleteObject(color)
        gdi32.DeleteObject(mask)


def destroy_hicon(handle: int) -> None:             # pragma: no cover - Windows only
    user32 = _W32[0] if _W32 else ctypes.WinDLL("user32")
    user32.DestroyIcon(handle)


def pystray_supported(pystray_module) -> bool:
    """The private methods overridden below are pystray 0.19's; on another version the
    plain pystray icon is used (it works, just without these improvements)."""
    try:
        from pystray import _info
        return sys.platform == "win32" and tuple(_info.__version__[:2]) == (0, 19)
    except Exception:
        return False


_classes: dict = {}


def icon_class(base: type) -> type:
    """pystray's Windows icon with the three changes above."""
    cls = _classes.get(base)
    if cls is not None:
        return cls

    class TrayIcon(base):                           # pragma: no cover - Windows only
        _pb_uid: Optional[int] = None
        _pb_handles: Optional[HandleCache] = None
        _pb_retries = 0

        def _message(self, code, flags, **kwargs):
            if self._pb_uid is None:
                return super()._message(code, flags, **kwargs)
            from pystray._util import win32
            ok = win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
                cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=self._hwnd, hID=self._pb_uid,
                uFlags=flags, **kwargs))
            if code == win32.NIM_ADD:
                if ok:
                    if self._pb_retries:
                        log.info("tray icon added after %d retries", self._pb_retries)
                    self._pb_retries = 0
                else:
                    self._retry_add()
            return ok

        def _retry_add(self):
            if self._pb_retries >= ADD_RETRIES:
                log.error("Windows refused the tray icon %d times; giving up", ADD_RETRIES)
                return
            self._pb_retries += 1
            if self._pb_retries == 1:
                log.warning("Windows refused the tray icon (taskbar not ready?); retrying")

            def again():
                if self.visible and self._running:
                    try:
                        self._show()
                    except Exception as e:
                        log.debug("tray icon retry: %s", e)
            t = threading.Timer(ADD_RETRY_SECONDS, again)
            t.daemon = True
            t.start()

        def _assert_icon_handle(self):
            if self._pb_handles is None:
                return super()._assert_icon_handle()
            if not self._icon_handle:
                self._icon_handle = self._pb_handles.get(self.icon)

        def _release_icon(self):
            if self._pb_handles is None:
                return super()._release_icon()
            self._icon_handle = None                    # the cache keeps (and later frees) it

        def release_handles(self):
            if self._pb_handles is not None:
                self._pb_handles.clear()

    _classes[base] = TrayIcon
    return TrayIcon


def make_icon(pystray_module, key: str, *args, **kwargs):
    """A pystray icon for ``key`` - improved on Windows with pystray 0.19, plain otherwise."""
    base = pystray_module.Icon
    if not pystray_supported(pystray_module):
        return base(*args, **kwargs)
    icon = icon_class(base)(*args, **kwargs)        # pragma: no cover - Windows only
    icon._pb_uid = icon_uid(key)
    icon._pb_handles = HandleCache(create_hicon, destroy_hicon)
    return icon
