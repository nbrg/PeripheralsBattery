"""Regression tests for bugs found in review (one test per bug)."""
import json
import threading
import time

import pytest

from peribatt import micmute, web
from peribatt.app import ALL_HIDDEN, PLACEHOLDER, App
from peribatt.bluetooth import to_readings
from peribatt.config import DEFAULTS, Store
from peribatt.hidpp import LogitechSource
from peribatt.hsc import HeadsetControlSource
from peribatt.model import MOUSE, Reading
from peribatt.razer import RazerSource
from peribatt.sources import Throttled

from .fakes import FakeApi, FakeClock, FakeHidppChannel, FakeLogiDevice, FakeRazer, QueueHandle
from .test_app import FakeBackend

MOUSE_R = Reading("logi-1", "PRO Wireless", MOUSE, 70)


# --- settings file ------------------------------------------------------------

def test_saving_while_other_threads_change_things(tmp_path):
    """Saving from one thread while another adds devices used to raise
    'dictionary changed size during iteration' and abort the poll."""
    store = Store(tmp_path / "settings.json")
    stop = threading.Event()
    errors = []

    def churn():
        i = 0
        while not stop.is_set():
            store.devices[f"d{i % 500}"] = {"name": "x", "kind": "mouse", "level": i % 100}
            store.logitech_slots[f"c539:{i % 7}"] = {"key": "k", "name": "n", "kind": "mouse"}
            if i % 3 == 0:
                store.devices.pop(f"d{(i + 250) % 500}", None)
            i += 1

    def saver():
        try:
            for _ in range(60):
                store.save()
        except Exception as e:                     # pragma: no cover - the bug
            errors.append(e)

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    savers = [threading.Thread(target=saver) for _ in range(3)]
    for s in savers:
        s.start()
    for s in savers:
        s.join()
    stop.set()
    t.join()
    assert errors == []
    json.loads((tmp_path / "settings.json").read_text())      # always a whole, valid file


def test_unchanged_settings_are_not_rewritten(tmp_path, monkeypatch):
    store = Store(tmp_path / "settings.json")
    writes = []
    real_replace = __import__("os").replace
    monkeypatch.setattr("peribatt.config.os.replace", lambda a, b: (writes.append(b), real_replace(a, b)))
    store.save()
    store.save()
    store.save()
    assert len(writes) == 1
    store["low"] = 25
    store.save()
    assert len(writes) == 2


@pytest.mark.parametrize("content", ["[]", "42", '"text"', '{"settings": [1, 2]}',
                                     '{"settings": {}, "devices": ["bad"]}',
                                     '{"devices": {"k": "not a dict"}}'])
def test_odd_settings_files_do_not_crash_startup(tmp_path, content):
    (tmp_path / "settings.json").write_text(content)
    store = Store.load(tmp_path)
    assert store.settings == DEFAULTS
    assert all(isinstance(v, dict) for v in store.devices.values())


def test_booleans_are_not_accepted_as_numbers(tmp_path):
    (tmp_path / "settings.json").write_text('{"settings": {"poll_seconds": true, "low": 25}}')
    store = Store.load(tmp_path)
    assert store["poll_seconds"] == DEFAULTS["poll_seconds"] and store["low"] == 25


# --- forgetting devices ---------------------------------------------------------

class StubbornSource:
    """Keeps reporting a device it has seen before - like the Logitech source does."""
    name = "stubborn"

    def __init__(self):
        self.reading = MOUSE_R.with_(online=False, note="switched off")
        self.forgotten = []

    def poll(self):
        return [self.reading]

    def forget(self, key):
        self.forgotten.append(key)


def test_forgetting_a_device_sticks(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    src = StubbornSource()
    app.sources = [src]
    app.apply([MOUSE_R])                              # seen online once
    app.poll_once()                                   # now reported switched off
    assert not app.readings["logi-1"].online
    app.forget_offline()
    assert src.forgotten == ["logi-1"]
    app.poll_once()                                   # the source still reports it...
    assert "logi-1" not in app.readings               # ...but it stays forgotten
    assert "logi-1" not in app.backend.icons
    src.reading = MOUSE_R                             # switched on again: back it comes
    app.poll_once()
    assert app.readings["logi-1"].online and "logi-1" in app.backend.icons


def test_logitech_source_lets_go_of_a_forgotten_mouse():
    src = LogitechSource(api=FakeApi(), known={"c539:1": {"key": "logi-x", "name": "PRO", "kind": "mouse"}})
    assert src.poll()
    src.forget("logi-x")
    assert src.poll() == [] and src.known == {}


def test_razer_source_lets_go_of_a_forgotten_device():
    dev = FakeRazer(raw=102)
    api = FakeApi()
    api.add(0x1532, 0x00AA, b"r", dev, product="Razer Basilisk V3 Pro")
    src = RazerSource(api=api, sleep=lambda s: None)
    src.poll()
    dev.asleep = True
    assert not src.poll()[0].online
    src.forget("razer-00aa")
    api.infos.clear()                                  # dongle unplugged
    assert src.poll() == []


# --- notifications and the app icon ----------------------------------------------

def test_hidden_devices_do_not_send_notifications(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    app.store["hidden"] = ["logi-1"]
    app.update(MOUSE_R.with_(level=5))
    assert app.backend.notes == []


def test_app_icon_says_when_everything_is_hidden(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    app.apply([MOUSE_R])
    app.hide("logi-1")
    assert app.backend.icons[PLACEHOLDER][1] == ALL_HIDDEN


def test_app_icon_follows_the_taskbar_theme(tmp_path):
    light = {"v": False}
    app = App(Store(tmp_path / "settings.json"), FakeBackend(), light_taskbar=lambda: light["v"])
    app.start()
    before = app.backend.icons[PLACEHOLDER][0].tobytes()
    light["v"] = True
    app.poll_once()
    assert app.backend.icons[PLACEHOLDER][0].tobytes() != before


def test_changing_a_setting_makes_cached_sources_look_again(tmp_path):
    calls = []

    class Slow:
        name = "bluetooth"

        def poll(self):
            calls.append(1)
            return []
    now = {"t": 0.0}
    th = Throttled(Slow(), 120, clock=lambda: now["t"])
    app = App(Store(tmp_path / "settings.json"), FakeBackend(), sources=[th])
    app.poll_once()
    app.poll_once()
    assert len(calls) == 1                             # cached
    app.set_setting("bluetooth", True)
    app.poll_once()
    assert len(calls) == 2


# --- device sources ------------------------------------------------------------------

def test_logitech_backs_off_from_slots_that_never_answer():
    class Silent(QueueHandle):
        def on_write(self, data):
            pass                                       # this receiver ignores unused slots

    api = FakeApi()
    api.add(0x046D, 0xC539, b"long", Silent(), 0xFF00, 0x0002)
    clock = FakeClock(step=0.05)
    src = LogitechSource(api=api, clock=clock)
    src.poll()
    first = clock.t
    src.poll()
    assert clock.t - first < 1                         # no second round of timeouts
    assert len(src.silent) == 6


def test_logitech_known_mouse_is_never_backed_off():
    mouse = FakeLogiDevice()
    api = FakeApi()
    long_h = FakeHidppChannel({1: mouse})
    api.add(0x046D, 0xC539, b"long", long_h, 0xFF00, 0x0002)
    src = LogitechSource(api=api, clock=FakeClock())
    src.poll()
    mouse.online = False
    long_h.short = QueueHandle()                        # its error replies now go unseen: timeouts
    src.poll()
    assert "c539:1" not in src.silent                   # still asked every poll


def test_razer_probes_again_when_the_known_route_stops_working():
    dev = FakeRazer(tid=0x1F, raw=153)
    api = FakeApi()
    api.add(0x1532, 0x00B7, b"r", dev, product="Razer DeathAdder V3 Pro")
    src = RazerSource(api=api, sleep=lambda s: None)
    assert src.poll()[0].online
    dev.tid = 0x3F                                      # e.g. after a firmware update
    src.poll()                                          # the cached route fails...
    assert 0x00B7 not in src.working
    assert src.poll()[0].level == 60                    # ...and the next poll finds the new one


def test_bluetooth_prefers_the_real_device_name():
    records = [("{A}", "Bluetooth LE Generic Attribute Service", 55, True),
               ("{A}", "Keychron K8 Pro", 55, True),
               ("{A}", "HID", 55, True)]
    [r] = to_readings(records)
    assert r.name == "Keychron K8 Pro"


def test_headsetcontrol_path_setting_applies_without_restart():
    path = {"v": ""}
    calls = []
    src = HeadsetControlSource(lambda: path["v"], run=lambda args: calls.append(args) or "{}")
    assert src.exe in ("", __import__("shutil").which("headsetcontrol"))   # nothing configured
    path["v"] = ' "C:\\Tools\\headsetcontrol.exe" '
    src.poll()
    assert calls == [["C:\\Tools\\headsetcontrol.exe", "-b", "-o", "json"]]


# --- microphone ----------------------------------------------------------------

def test_a_vanished_microphone_is_not_left_muted(monkeypatch):
    monkeypatch.setattr(micmute, "FAILURE_BACKOFF", 0.2)
    calls = {"muted": 0}

    class Mic:
        present = True

        def resolve(self):
            if not Mic.present:
                raise OSError("no microphone")

        def muted(self):
            calls["muted"] += 1
            if not Mic.present:
                raise OSError("no microphone")
            return True

    seen = []
    w = micmute.MicMuteWatcher(seen.append, interval=0.01, factory=Mic)
    w.start()
    end = time.time() + 2
    while seen != [True] and time.time() < end:
        time.sleep(0.01)
    Mic.present = False                                 # headset unplugged while muted
    while seen[-1] is not False and time.time() < end:
        time.sleep(0.01)
    assert seen[-1] is False                            # no endless blinking
    before = calls["muted"]
    time.sleep(0.15)
    assert calls["muted"] - before <= 1                 # backs off instead of retrying every 10 ms
    w.stop()


# --- settings window -------------------------------------------------------------

def test_reopening_the_window_keeps_the_server_alive(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    ui = web.WebUi(app, api=FakeApi(), opener=lambda u: None, idle_seconds=0.6)
    ui.open()
    time.sleep(0.45)
    ui.open()                                            # just before it would idle out
    time.sleep(0.4)
    assert ui.running
    assert ui.stopped.wait(3)                            # and it still stops once idle
    ui.stop()


# --- a quiet receiver hung the whole poll (v0.6.0) ------------------------------------

def test_hid_devices_are_opened_non_blocking():
    """cython-hidapi's read(n, 0) blocks forever unless the device is non-blocking."""
    from peribatt.hidio import HidApi

    class Dev:
        nonblocking = False

        def open_path(self, path):
            pass

        def set_nonblocking(self, on):
            Dev.nonblocking = bool(on)

        def close(self):
            pass

    class Hid:
        device = Dev
    api = HidApi()
    api._hid = Hid
    api.open(b"x")
    assert Dev.nonblocking


def test_a_hanging_source_does_not_stop_the_others(tmp_path):
    release = threading.Event()

    class Hangs:
        name = "logitech"

        def poll(self):
            release.wait(5)
            return []

    class Fine:
        name = "hyperx"

        def poll(self):
            return [Reading("hx", "HyperX Cloud Flight S", "headset", 60)]

    app = App(Store(tmp_path / "settings.json"), FakeBackend(), sources=[Hangs(), Fine()])
    app.source_timeout = 0.2
    app.poll_once()
    assert "hx" in app.readings and app.source_status["logitech"]["error"] == "not answering"
    started = time.monotonic()
    app.poll_once()                                     # still stuck: skipped, no second wait
    assert time.monotonic() - started < 0.2
    release.set()
    time.sleep(0.1)
    app.poll_once()
    assert app.source_status["logitech"]["error"] == ""
