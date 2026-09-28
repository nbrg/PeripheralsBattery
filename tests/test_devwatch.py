"""Plug/unplug notices and the HID device list cache."""
import threading

import pytest

from peribatt import devwatch, hidio


class FakeHid:
    def __init__(self):
        self.calls = 0
        self.devices = [{"vendor_id": 0x046D, "product_id": 0xC539, "path": b"a"}]

    def enumerate(self, vid, pid):
        self.calls += 1
        return [d for d in self.devices if vid in (0, d["vendor_id"])]


@pytest.fixture
def api():
    a = hidio.HidApi()
    a._hid = FakeHid()
    hidio.set_watching(True)
    yield a
    hidio.set_watching(False)


def test_device_list_is_read_once_until_something_changes(api):
    for _ in range(5):
        assert len(api.enumerate(0x046D)) == 1
    assert api._hid.calls == 1
    api._hid.devices.append({"vendor_id": 0x046D, "product_id": 0xC088, "path": b"b"})
    hidio.device_list_changed()                         # the mouse was put on its cable
    assert len(api.enumerate(0x046D)) == 2 and api._hid.calls == 2


def test_callers_cannot_spoil_the_cache(api):
    api.enumerate(0x046D)[0]["path"] = b"changed"
    assert api.enumerate(0x046D)[0]["path"] == b"a"


def test_without_the_watcher_every_call_asks_hidapi(api):
    hidio.set_watching(False)
    api.enumerate(0x046D)
    api.enumerate(0x046D)
    assert api._hid.calls == 2


def test_old_answers_expire(api, monkeypatch):
    api.enumerate(0x046D)
    monkeypatch.setattr(hidio, "CACHE_MAX_AGE", 0.0)
    api.enumerate(0x046D)
    assert api._hid.calls == 2


def test_a_burst_of_events_triggers_one_early_poll(api):
    fired = threading.Event()
    count = []
    w = devwatch.DeviceWatcher(lambda: (count.append(1), fired.set()), settle=0.1)
    api.enumerate(0x046D)
    for _ in range(4):                                   # a dongle brings several interfaces
        w.changed()
    assert fired.wait(2)
    assert count == [1] and w.events == 4
    api.enumerate(0x046D)
    assert api._hid.calls == 2                           # the cache was dropped


def test_the_callback_never_raises_into_windows():
    w = devwatch.DeviceWatcher(lambda: 1 / 0, settle=0.01)
    assert w._event(None, None, devwatch.ACTION_ARRIVAL, None, 0) == 0
    w.stop()


def test_app_polls_at_once_after_a_plug_event(tmp_path):
    from peribatt.app import App
    from peribatt.config import Store

    from .test_app import FakeBackend
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    app.devices_changed()
    assert app.refresh_event.is_set()


def test_filter_struct_layout():
    import ctypes
    # cbSize, Flags, FilterType, Reserved, then a union as large as InstanceId[200]
    assert ctypes.sizeof(devwatch.CM_NOTIFY_FILTER) == 416
