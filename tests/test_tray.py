"""The pystray front end and the whole app, driven through a fake pystray."""
import sys
import threading
import time
import types

import pytest

from peribatt.model import HEADSET, MOUSE, Reading
from peribatt.sources import Throttled


class FakeIcon:
    all = []

    def __init__(self, name, icon=None, title="", menu=None):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.visible = False
        self.stopped = False
        self.menu_updates = 0
        self.notes = []
        FakeIcon.all.append(self)

    def run_detached(self, setup=None):
        (setup or (lambda i: setattr(i, "visible", True)))(self)

    def run(self, setup=None):
        self.run_detached(setup)

    def stop(self):
        self.stopped = True

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title=None):
        self.notes.append((title, message))


class FakeItem:
    def __init__(self, text, action=None, checked=None, radio=False, default=False,
                 enabled=True, visible=True):
        self.text, self.action, self.checked = text, action, checked
        self.default, self.enabled = default, enabled


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self._items = items

    @property
    def items(self):
        if len(self._items) == 1 and callable(self._items[0]):
            return list(self._items[0]())
        return list(self._items)


@pytest.fixture
def fake_pystray(monkeypatch, tmp_path):
    mod = types.ModuleType("pystray")
    mod.Icon, mod.MenuItem, mod.Menu = FakeIcon, FakeItem, FakeMenu
    monkeypatch.setitem(sys.modules, "pystray", mod)
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    FakeIcon.all = []
    return mod


def labels(menu):
    return [i.text for i in menu.items if isinstance(i, FakeItem)]


def find(menu, text):
    return next(i for i in menu.items if isinstance(i, FakeItem) and i.text == text)


def make_app(tmp_path):
    from peribatt.app import App
    from peribatt.config import Store
    from peribatt.tray import PystrayBackend
    backend = PystrayBackend(24)
    app = App(Store(tmp_path / "s.json"), backend)
    backend.app = app
    return app, backend


def test_menu_contents_and_actions(fake_pystray, tmp_path):
    app, backend = make_app(tmp_path)
    app.apply([Reading("logi-1", "PRO Wireless", MOUSE, 76)])
    icon = backend.icons["logi-1"]
    assert icon.visible and icon.title == "PRO Wireless: 76%"
    names = labels(icon.menu)
    assert names[0] == "PRO Wireless: 76%"
    for expected in ("Refresh now", "Poll every", "Low battery alert", "Display", "Sources",
                     "Hide this device", "Forget disconnected devices", "Exit"):
        assert expected in names
    assert find(icon.menu, "Refresh now").default
    find(icon.menu, "Refresh now").action()
    assert app.refresh_event.is_set()
    find(icon.menu, "Hide this device").action()
    assert "logi-1" not in backend.icons and icon.stopped
    assert "logi-1" in app.store["hidden"]


def test_headset_menu_default_toggles_mic(fake_pystray, tmp_path):
    app, backend = make_app(tmp_path)
    toggled = []
    app.mic_toggle = lambda: toggled.append(1)
    app.apply([Reading("hx", "Cloud Flight S", HEADSET, 50)])
    item = find(backend.icons["hx"].menu, "Toggle mic mute")
    assert item.default
    item.action()
    assert toggled == [1]


def test_settings_radio_items(fake_pystray, tmp_path):
    app, backend = make_app(tmp_path)
    app.apply([Reading("logi-1", "PRO Wireless", MOUSE, 76)])
    poll = find(backend.icons["logi-1"].menu, "Poll every").action
    two_min = find(poll, "2 minutes")
    two_min.action()
    assert app.store["poll_seconds"] == 120 and two_min.checked(two_min)


def test_menus_refresh_only_on_change(fake_pystray, tmp_path):
    app, backend = make_app(tmp_path)
    r = Reading("logi-1", "PRO Wireless", MOUSE, 76)
    app.apply([r])
    icon = backend.icons["logi-1"]
    before = icon.menu_updates
    app.apply([r])
    assert icon.menu_updates == before
    app.apply([r.with_(level=75)])
    assert icon.menu_updates == before + 1


def test_notifications_reach_the_icon(fake_pystray, tmp_path):
    app, backend = make_app(tmp_path)
    app.apply([Reading("logi-1", "PRO Wireless", MOUSE, 5)])
    assert backend.icons["logi-1"].notes[0][0] == "Low battery"


def test_whole_app_starts_and_exits_from_the_menu(fake_pystray, monkeypatch):
    from peribatt import winshell
    from peribatt.__main__ import main
    autostart = []
    # never touch the real registry / mutex of the machine running the tests
    monkeypatch.setattr(winshell, "set_autostart", autostart.append)
    monkeypatch.setattr(winshell, "single_instance", lambda: True)
    monkeypatch.setattr(winshell, "tray_icon_size", lambda: 24)
    from peribatt import web
    windows = []
    monkeypatch.setattr(web, "open_window", windows.append)      # never a real browser in tests
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", main([])), daemon=True)
    t.start()
    deadline = time.time() + 10
    while not FakeIcon.all and time.time() < deadline:
        time.sleep(0.02)
    assert FakeIcon.all, "no tray icon appeared"
    placeholder = FakeIcon.all[0]
    assert placeholder.title.startswith("Peripherals Battery:")          # searching / nothing found
    assert placeholder.notes and "^" in placeholder.notes[0][1]          # first-launch welcome
    end = time.time() + 10                                             # ...and the settings window
    while not windows and time.time() < end:
        time.sleep(0.02)
    assert windows and windows[0].startswith("http://127.0.0.1:")
    find(placeholder.menu, "Exit").action()
    t.join(10)
    assert result.get("rc") == 0 and placeholder.stopped
    assert autostart == [True]            # switched on at the very first launch


def test_throttled_source():
    calls = []
    now = {"t": 0.0}

    class S:
        name = "s"

        def poll(self):
            calls.append(now["t"])
            return [len(calls)]
    th = Throttled(S(), 100, clock=lambda: now["t"])
    assert th.poll() == [1]
    now["t"] = 50
    assert th.poll() == [1]
    now["t"] = 100
    assert th.poll() == [2] and calls == [0.0, 100]


def test_mic_watcher_idles_when_not_wanted():
    from peribatt.micmute import MicMuteWatcher

    class Mic:
        checks = 0

        def muted(self):
            Mic.checks += 1
            return False

        def resolve(self):
            pass

    w = MicMuteWatcher(lambda m: None, interval=0.01, factory=Mic, wanted=lambda: False)
    w.start()
    time.sleep(0.2)
    w.stop()
    assert Mic.checks == 0
