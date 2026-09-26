"""Windows shell integration: start with Windows, one instance only, the tray
icon size for the current DPI, and whether the taskbar is light or dark."""
from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

from . import APP_NAME
from .winapi import IS_WINDOWS

log = logging.getLogger("peribatt")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
SM_CXSMICON = 49
ERROR_ALREADY_EXISTS = 183

_mutex = None


def launch_command() -> str:
    if getattr(sys, "frozen", False):            # PyInstaller build
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    launcher = Path(__file__).resolve().parent.parent / "peribatt.pyw"
    return f'"{pythonw if pythonw.exists() else exe}" "{launcher}"'


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def taskbar_is_light() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def single_instance() -> bool:
    """False when another copy is already running."""
    global _mutex
    if not IS_WINDOWS:
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}-single-instance")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def tray_icon_size() -> int:
    """The small-icon size for the current DPI (16 at 100%, 24 at 150%, ...),
    so the icon is drawn pixel-exact instead of being rescaled by Windows."""
    if not IS_WINDOWS:
        return 32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # system DPI aware
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
    size = ctypes.windll.user32.GetSystemMetrics(SM_CXSMICON)
    return size if 12 <= size <= 128 else 16
