"""pystray front end: one tray icon per device, a shared menu, and the
threads that keep them up to date."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Dict

from . import DISPLAY_NAME, __version__, trayicon, winshell
from .app import PLACEHOLDER, App
from .config import app_dir
from .model import HEADSET

log = logging.getLogger("peribatt")


class PystrayBackend:
    """Creates pystray icons on demand, through pystray's public API only.

    Anything that goes wrong inside pystray's own threads is logged: the packaged
    app has no console, so an unlogged error there means an icon silently never
    appears."""

    def __init__(self, icon_size: int):
        import pystray
        self.pystray = pystray
        self.icon_size = icon_size
        self.app: App = None                 # set by run()
        self.icons: Dict[str, object] = {}
        self._ready: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # -- Backend protocol -------------------------------------------------
    def show(self, key, image, title):
        icon = trayicon.make_icon(self.pystray, key, f"peribatt-{len(self.icons)}", image, title,
                                  menu=self._menu(key))

        ready = threading.Event()

        def setup(ic):
            try:
                ic.visible = True
                log.info("tray icon shown: %s", key)
            except Exception:
                log.exception("could not show the tray icon for %s", key)
            finally:
                ready.set()

        with self._lock:
            self.icons[key] = icon
            self._ready[key] = ready
        def run():
            try:
                icon.run(setup=setup)
            finally:
                release = getattr(icon, "release_handles", None)
                if release is not None:
                    release()                   # the icon's window is gone: free its images

        # Our own daemon thread (pystray's run_detached uses a non-daemon one): an icon
        # thread must never keep the process alive after Exit.
        threading.Thread(target=run, daemon=True, name=f"tray-{key}").start()

    def set_image(self, key, image, cache_key):
        icon = self.icons.get(key)
        if icon is not None:
            try:
                icon.icon = image
            except Exception:
                log.exception("could not update the tray icon for %s", key)

    def set_title(self, key, title):
        icon = self.icons.get(key)
        if icon is not None:
            try:
                icon.title = title
            except Exception:
                log.exception("could not update the tooltip for %s", key)

    def remove(self, key):
        with self._lock:
            icon = self.icons.pop(key, None)
            ready = self._ready.pop(key, None)
        if icon is not None:
            # pystray ignores stop() until its loop is up: an icon removed right after
            # it was created (the "searching" icon, when devices turn up fast) would
            # otherwise stay in the tray with a thread that never ends.
            if ready is not None and not ready.wait(5):
                log.warning("tray icon %s did not start in time", key)
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
                log.exception("could not rebuild a tray menu")

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
            if app.open_diagnostics:
                out.append(Item("Diagnostics…", lambda: app.open_diagnostics()))
            out += [
                Item("Forget disconnected devices", lambda: app.forget_offline()),
                Item("Open data folder", lambda: _open_folder()),
            ]
            if sys.platform == "win32":
                out.append(Item("Start with Windows", lambda: _toggle_autostart(),
                                checked=lambda _i: winshell.autostart_enabled()))
            out += [Menu.SEPARATOR,
                    Item(f"{DISPLAY_NAME} {__version__}", None, enabled=False),
                    Item("Exit", lambda: app.stop())]
            return out

        return Menu(items)


def _toggle_autostart():
    try:
        winshell.set_autostart(not winshell.autostart_enabled())
    except OSError:
        log.exception("could not change Start with Windows")


def _open_folder():
    path = app_dir()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening our own data folder
    else:
        subprocess.Popen(["xdg-open", str(path)])
