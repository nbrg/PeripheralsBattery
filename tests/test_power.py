"""Waking from sleep: the Windows callback, the poll loop's own detection, and
what each source does about it."""
import sys
import threading
import time

import pytest

from peribatt import app as app_mod
from peribatt import power
from peribatt.app import App
from peribatt.config import Store
from peribatt.hyperx import HyperXSource
from peribatt.micmute import MicMuteWatcher
from peribatt.model import MOUSE, Reading
from peribatt.recipes import Recipe, RecipeSource
from peribatt.sources import Switchable, Throttled

from .fakes import FakeApi, QueueHandle
from .test_app import FakeBackend

# --- the Windows callback -------------------------------------------------------


def test_callback_dispatches_resume_and_suspend():
    events = []
    w = power.PowerWatcher(lambda: events.append("resume"), lambda: events.append("suspend"))
    # through the real ctypes callback object Windows would call
    assert w._callback(None, power.PBT_APMSUSPEND, None) == 0
    assert w._callback(None, power.PBT_APMRESUMEAUTOMATIC, None) == 0
    assert w._callback(None, power.PBT_APMRESUMESUSPEND, None) == 0
    assert w._callback(None, 0x000A, None) == 0          # power status change: ignored
    assert events == ["suspend", "resume", "resume"]


def test_callback_never_raises_into_windows():
    def boom():
        raise RuntimeError("handler bug")
    w = power.PowerWatcher(boom)
    assert w._callback(None, power.PBT_APMRESUMEAUTOMATIC, None) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="other platforms only")
def test_no_registration_outside_windows():
    w = power.PowerWatcher(lambda: None)
    assert w.start() is False
    w.stop()


# --- the app's reaction -----------------------------------------------------------

class CountingSource:
    name = "counting"

    def __init__(self):
        self.polls = []
        self.resets = []

    def poll(self):
        self.polls.append(time.monotonic())
        return [Reading("logi-1", "PRO Wireless", MOUSE, 70)]

    def reset(self):
        self.resets.append(time.monotonic())


@pytest.fixture
def fast_resume(monkeypatch):
    monkeypatch.setattr(app_mod, "RESUME_SETTLE", 0.05)
    monkeypatch.setattr(app_mod, "RESUME_RECHECK", 0.4)


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_resume_resets_polls_and_rechecks(tmp_path, fast_resume):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    app.store["poll_seconds"] = 600                       # nothing would happen without the resume
    src = CountingSource()
    app.sources = [src]
    t = threading.Thread(target=app.poll_loop, daemon=True)
    t.start()
    assert wait_for(lambda: len(src.polls) == 1)

    resumed_at = time.monotonic()
    app.resumed()                                         # e.g. from the power callback's thread
    assert wait_for(lambda: len(src.polls) == 2)
    assert len(src.resets) == 1
    assert src.resets[0] - resumed_at >= 0.04             # waited for devices to settle first
    assert src.resets[0] <= src.polls[1]                  # handles dropped before polling
    assert wait_for(lambda: len(src.polls) == 3)          # the follow-up look
    assert src.polls[2] - resumed_at >= 0.35
    time.sleep(0.3)
    assert len(src.polls) == 3 and len(src.resets) == 1   # and then back to normal
    app.stop()
    t.join(2)
    assert not t.is_alive()


def test_stopping_during_the_settle_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "RESUME_SETTLE", 30)
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    src = CountingSource()
    app.sources = [src]
    app.resumed()
    t = threading.Thread(target=app.poll_loop, daemon=True)
    t.start()
    time.sleep(0.1)
    app.stop()
    t.join(2)
    assert not t.is_alive() and src.polls == []


def test_a_long_overrun_is_taken_as_sleep(tmp_path, monkeypatch):
    """No power callback (older Windows, other OS): the clock gives it away."""
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    clock = iter([1000.0, 1000.0 + 3 + 3600])            # the 3 s wait ended an hour later
    monkeypatch.setattr(app_mod.time, "time", lambda: next(clock))
    assert app._wait(0.01) is True
    assert app._resumed_at is not None


def test_a_normal_wait_is_not_sleep(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    assert app._wait(0.01) is False and app._resumed_at is None


# --- the sources -----------------------------------------------------------------

def test_hyperx_reopens_the_dongle_after_reset():
    api = FakeApi()
    old = QueueHandle()
    api.add(0x0951, 0x16EA, b"v", old, usage_page=0xFF13)
    src = HyperXSource(api=api, threaded=False)
    src.poll()
    new = QueueHandle()
    api.handles[b"v"] = new                              # after sleep the OS hands out a new handle
    src.reset()
    assert old.closed
    src.poll()
    assert new.written                                   # queries go to the fresh handle


def test_recipe_listeners_are_reopened_after_reset():
    recipe = Recipe.parse({"name": "X", "vendor_id": 1, "product_ids": [2],
                           "listen": [{"expect": "01", "level": {"byte": 1}}]})
    api = FakeApi()
    h = QueueHandle()
    api.add(1, 2, b"x", h, usage_page=0xFF00)
    src = RecipeSource([recipe], api=api, threaded=False)
    src.poll()
    assert src.listeners
    src.reset()
    assert h.closed and not src.listeners


def test_throttled_and_switchable_pass_reset_on():
    inner = CountingSource()
    now = {"t": 0.0}
    th = Throttled(Switchable(inner, lambda: True), 120, clock=lambda: now["t"])
    th.poll()
    th.poll()
    assert len(inner.polls) == 1                         # cached
    th.reset()
    th.poll()
    assert len(inner.polls) == 2 and len(inner.resets) == 1


def test_mic_watcher_looks_up_the_microphone_again():
    class Mic:
        resolves = 0

        def muted(self):
            return False

        def resolve(self):
            Mic.resolves += 1

    w = MicMuteWatcher(lambda m: None, interval=0.01, factory=Mic)
    w.start()
    time.sleep(0.05)
    w.resync()
    assert wait_for(lambda: Mic.resolves == 1)
    w.stop()
