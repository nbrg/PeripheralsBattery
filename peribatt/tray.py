"""pystray front end: one tray icon per device, a shared menu, and the
threads that keep them up to date."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections import OrderedDict
from typing import Dict

from . import DISPLAY_NAME, __version__, winshell
from .app import PLACEHOLDER, App
from .config import app_dir
from .model import HEADSET

log = logging.getLogger("peribatt")

HICON_CACHE = 96


class PystrayBackend:
    """Creates pystray icons on demand. On Windows, icon images are turned into
    HICONs once and cached, so the 4 Hz mute blink costs no disk I/O (pystray
    itself writes a temporary .ico for every image change)."""

    def __init__(self, icon_size: int):
        import pystray
        self.pystray = pystray
        self.icon_size = icon_size
        self.app: App = None                 # set by run()
        self.icons: Dict[str, object] = {}
        self._hicons: "OrderedDict[tuple, int]" = OrderedDict()
        self._lock = threading.Lock()

    # -- Backend protocol -------------------------------------------------
    def show(self, key, image, title):
        icon = self.pystray.Icon(f"peribatt-{key}", image, title, menu=self._menu(key))

        def setup(ic):
            ic.visible = True

        with self._lock:
            self.icons[key] = icon
        icon.run_detached(setup=setup) if sys.platform == "win32" else \
            threading.Thread(target=icon.run, daemon=True).start()

    def set_image(self, key, image, cache_key):
        icon = self.icons.get(key)
        if icon is None:
            return
        if sys.platform == "win32" and self._set_hicon(icon, image, cache_key):
            return
        icon.icon = image

    def set_title(self, key, title):
        icon = self.icons.get(key)
        if icon is not None:
            icon.title = title

    def remove(self, key):
        with self._lock:
            icon = self.icons.pop(key, None)
        if icon is not None:
            if getattr(icon, "_icon_handle", None) in self._hicons.values():
                icon._icon_handle = None      # cached handle: ours to free, not pystray's
            try:
                icon.stop()
            except Exception as e:
                log.debug("stop icon: %s", e)

    def notify(self, key, title, message):
        icon = self.icons.get(key) or next(iter(self.icons.values()), None)
        if icon is not None:
            try:
                icon.notify(message, title)
            except Exception as e:
                log.debug("notify: %s", e)

    def refresh_menus(self):
        for icon in list(self.icons.values()):
            try:
                icon.update_menu()
            except Exception:
                pass

    # -- Windows icon cache -------------------------------------------------
    def _set_hicon(self, icon, image, cache_key) -> bool:
        try:
            from pystray._util import serialized_image, win32
        except ImportError:
            return False
        if not getattr(icon, "_hwnd", None):
            return False
        try:
            h = self._hicons.get(cache_key)
            if h is None:
                with serialized_image(image, "ICO") as path:
                    h = win32.LoadImage(None, path, win32.IMAGE_ICON, self.icon_size,
                                        self.icon_size, win32.LR_LOADFROMFILE)
                self._hicons[cache_key] = h
                while len(self._hicons) > HICON_CACHE:
                    _, old = self._hicons.popitem(last=False)
                    if not any(getattr(i, "_icon_handle", None) == old for i in self.icons.values()):
                        win32.DestroyIcon(old)
            else:
                self._hicons.move_to_end(cache_key)
            # pystray owns (and destroys) whatever sits in _icon_handle: free its own
            # initial handle once, then park a cached one there.
            old = getattr(icon, "_icon_handle", None)
            if old and old not in self._hicons.values():
                try:
                    win32.DestroyIcon(old)
                except OSError:
                    pass
            icon._icon_handle = h
            icon._message(win32.NIM_MODIFY, win32.NIF_ICON, hIcon=h)
            return True
        except Exception as e:
            log.debug("hicon path failed, falling back: %s", e)
            return False

    # -- menu -------------------------------------------------------------
    def _menu(self, key):
        p = self.pystray
        Item, Menu = p.MenuItem, p.Menu
        app = self.app
        s = app.store

        def radio(setting, value, label):
            return Item(label, lambda: app.set_setting(setting, value),
                        checked=lambda _i: s[setting] == value, radio=True)

        def toggle(setting, label):
            return Item(label, lambda: app.set_setting(setting, not s[setting]),
                        checked=lambda _i: bool(s[setting]))

        def refresh():
            app.refresh_event.set()

        def items():
            # Rebuilt every time the menu is refreshed, so the status lines
            # at the top always match the icons.
            out = [Item(line, None, enabled=False) for line in (app.menu_status() or ["No devices yet"])]
            out.append(Menu.SEPARATOR)
            # Left-click runs the default item: mic toggle on headsets, else the settings window.
            reading = app.readings.get(key)
            mic = reading is not None and reading.kind == HEADSET and app.mic_toggle is not None
            if mic:
                out.append(Item("Toggle mic mute", lambda: app.mic_toggle(), default=True))
            if app.open_settings:
                out.append(Item("Settings…", lambda: app.open_settings(), default=not mic))
            if app.open_learn:
                out.append(Item("Learn a new device…", lambda: app.open_learn()))
            out.append(Item("Refresh now", refresh,
                            default=not mic and app.open_settings is None))
            out += [
                Item("Poll every", Menu(*(radio("poll_seconds", v, lbl) for v, lbl in
                                          ((30, "30 seconds"), (60, "1 minute"),
                                           (120, "2 minutes"), (300, "5 minutes"))))),
                Item("Low battery alert", Menu(*(radio("alert_at", v, lbl) for v, lbl in
                                                 ((0, "Off"), (10, "10%"), (15, "15%"),
                                                  (20, "20%"), (25, "25%"))))),
                Item("Display", Menu(
                    toggle("show_number", "Show percentage instead of picture"),
                    toggle("flash_on_mute", "Blink headset while mic is muted"),
                    toggle("windows_mute", "Count Windows mic mute as muted"),
                    toggle("notify_full", "Notify when fully charged"))),
                Item("Sources", Menu(
                    toggle("bluetooth", "Windows Bluetooth devices"),
                    toggle("xinput", "Xbox-compatible controllers"))),
                Menu.SEPARATOR,
            ]
            if key != PLACEHOLDER:
                out.append(Item("Hide this device", lambda: app.hide(key)))
            if s["hidden"]:
                out.append(Item("Show hidden devices", lambda: app.unhide_all()))
            out += [
                Item("Forget disconnected devices", lambda: app.forget_offline()),
                Item("Open data folder", lambda: _open_folder()),
            ]
            if sys.platform == "win32":
                out.append(Item("Start with Windows",
                                lambda: winshell.set_autostart(not winshell.autostart_enabled()),
                                checked=lambda _i: winshell.autostart_enabled()))
            out += [Menu.SEPARATOR,
                    Item(f"{DISPLAY_NAME} {__version__}", None, enabled=False),
                    Item("Exit", lambda: app.stop())]
            return out

        return Menu(items)


def _open_folder():
    path = app_dir()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening our own data folder
    else:
        subprocess.Popen(["xdg-open", str(path)])
