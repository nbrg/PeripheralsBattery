"""Windows shell integration: start with Windows, one instance only, the tray
icon size for the current DPI, and whether the taskbar is light or dark."""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

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


class TemporaryFolder(OSError):
    """"Start with Windows" was refused: this copy runs from a temporary folder."""


TEMP_FOLDER_MESSAGE = ("The app is running from a temporary folder - probably straight from the "
                       "zip. Extract the zip to a folder of its own (or use the installer), start "
                       "it from there, then turn on Start with Windows.")


def running_from_temp(exe: Optional[str] = None, temp_dirs=None) -> bool:
    """True for a copy started from a temporary folder: Explorer or 7-Zip open a
    program inside a zip by unpacking it into %TEMP% and delete it later, so an
    autostart entry pointing there would lead nowhere after the next reboot."""
    exe = exe or (sys.executable if getattr(sys, "frozen", False) else str(Path(__file__).resolve()))
    me = os.path.normcase(os.path.abspath(exe))
    dirs = temp_dirs if temp_dirs is not None else {
        tempfile.gettempdir(), os.environ.get("TEMP", ""), os.environ.get("TMP", "")}
    for d in dirs:
        if d:
            d = os.path.normcase(os.path.abspath(d)).rstrip("\\/")
            if me.startswith(d + os.sep):
                return True
    return False


def autostart_command() -> Optional[str]:
    """The command the autostart entry runs now, or None when it is off."""
    if not IS_WINDOWS:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _ = winreg.QueryValueEx(k, APP_NAME)
        return str(value)
    except OSError:
        return None


def refresh_autostart() -> bool:
    """"Start with Windows" is on but points at another copy of the app - the
    folder was moved, or an older copy set it: point it at this one. Never at a
    copy in a temporary folder, and only for the packaged app."""
    if not getattr(sys, "frozen", False) or running_from_temp():
        return False
    current = autostart_command()
    if current is None or current.strip().lower() == launch_command().lower():
        return False
    try:
        set_autostart(True)
        log.info("autostart now points at %s (was %s)", launch_command(), current)
        return True
    except OSError as e:
        log.warning("could not update autostart: %s", e)
        return False


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
    """Raises :class:`TemporaryFolder` rather than point autostart at a temporary copy."""
    if not IS_WINDOWS:
        return
    if on and running_from_temp():
        raise TemporaryFolder(TEMP_FOLDER_MESSAGE)
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
