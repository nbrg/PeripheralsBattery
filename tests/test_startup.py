"""Startup, the always-reachable app icon, crash logging and diagnostics."""
import logging
import threading
import time

from peribatt import __main__ as cli
from peribatt import diagnostics
from peribatt.app import NOTHING_FOUND, PLACEHOLDER, SEARCHING, App
from peribatt.config import Store
from peribatt.hyperx import HyperXSource
from peribatt.model import MOUSE, Reading

from .fakes import FakeApi, QueueHandle
from .test_app import FakeBackend


def make_app(tmp_path):
    return App(Store(tmp_path / "settings.json"), FakeBackend())


def test_icon_appears_before_the_first_search(tmp_path):
    app = make_app(tmp_path)
    app.start()
    assert list(app.backend.icons) == [PLACEHOLDER]
    assert app.backend.icons[PLACEHOLDER][1] == SEARCHING


def test_remembered_devices_show_at_start(tmp_path):
    store = Store(tmp_path / "settings.json")
    store.devices["logi-1"] = {"name": "PRO Wireless", "kind": MOUSE, "level": 64}
    app = App(store, FakeBackend())
    app.start()
    assert list(app.backend.icons) == ["logi-1"]


def test_placeholder_says_when_nothing_was_found(tmp_path):
    app = make_app(tmp_path)
    app.start()
    app.sources = []
    app.poll_once()
    assert app.backend.icons[PLACEHOLDER][1] == NOTHING_FOUND


def test_each_source_is_timed_and_logged(tmp_path, caplog):
    class Broken:
        name = "broken"

        def poll(self):
            raise RuntimeError("receiver exploded")

    class Good:
        name = "good"

        def poll(self):
            return [Reading("logi-1", "PRO Wireless", MOUSE, 70)]

    app = make_app(tmp_path)
    app.sources = [Broken(), Good()]
    with caplog.at_level(logging.INFO, logger="peribatt"):
        app.poll_once()
    assert app.source_status["good"]["count"] == 1
    assert "receiver exploded" in app.source_status["broken"]["error"]
    assert "good: 1 device(s)" in caplog.text and "broken poll failed" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="peribatt"):
        app.poll_once()                                  # unchanged: not logged again
    assert "good: 1 device(s)" not in caplog.text


def test_poll_loop_survives_a_crash(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    app.store["poll_seconds"] = 600
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("tray backend fell over")
        return []
    monkeypatch.setattr(app, "poll_once", flaky)
    monkeypatch.setattr(app.stop_event, "wait", lambda t=None: False)   # no 10 s back-off in tests
    t = threading.Thread(target=app.poll_loop, daemon=True)
    t.start()
    end = time.time() + 5
    while len(calls) < 2 and time.time() < end:
        time.sleep(0.01)
    app.stop_event.set()
    app.refresh_event.set()
    assert len(calls) >= 2


def test_uncaught_thread_errors_reach_the_log(caplog):
    old_thread, old_sys = threading.excepthook, __import__("sys").excepthook
    try:
        cli.install_crash_logging()
        with caplog.at_level(logging.CRITICAL, logger="peribatt"):
            t = threading.Thread(target=lambda: 1 / 0, name="poll")
            t.start()
            t.join()
        assert "uncaught error in thread poll" in caplog.text and "ZeroDivisionError" in caplog.text
    finally:
        threading.excepthook = old_thread
        __import__("sys").excepthook = old_sys


def test_diagnostics_report(tmp_path, monkeypatch):
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    (tmp_path / "peribatt.log").write_text("line one\nhyperx reader stopped: boom\n")
    api = FakeApi()
    api.add(0x046D, 0xC539, b"a", QueueHandle(), usage_page=0xFF00, usage=2, product="USB Receiver")
    api.add(0x1234, 0x0001, b"b", QueueHandle(), usage_page=0xFF00, product="Mystery")
    app = make_app(tmp_path)
    app.sources = [HyperXSource(api=api, threaded=False)]
    app.poll_once()
    text = diagnostics.report(app, api=api, is_supported=lambda v, p: v == 0x046D)
    assert "046d:c539" in text and "[supported]" in text and "1234:0001" in text
    assert "[hyperx] last poll: 0 device(s)" in text
    assert "hyperx reader stopped: boom" in text           # the log tail
    assert "Tray icons shown: __none__" in text


def test_diagnostics_when_hidapi_is_missing():
    class NoHid(FakeApi):
        error = "ImportError: DLL load failed"
    text = "\n".join(diagnostics.hid_section(NoHid()))
    assert "hidapi could not be loaded: ImportError" in text


def test_probe_prints_in_a_console(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "collect", lambda store: ([], []))
    assert cli.main(["--probe"]) == 0
    assert "diagnostics" in capsys.readouterr().out


def test_hyperx_reader_releases_its_own_handle():
    """close() must not free a handle another thread is reading from."""
    class Slow(QueueHandle):
        reading = threading.Event()

        def read(self, n, timeout_ms=0):
            Slow.reading.set()
            time.sleep(0.05)
            assert not self.closed, "handle closed during a read"
            return []

    api = FakeApi()
    h = Slow()
    api.add(0x0951, 0x16EA, b"v", h, usage_page=0xFF13)
    src = HyperXSource(api=api)
    src.poll()
    assert Slow.reading.wait(2)
    src.close()
    assert not h.closed                                     # still inside a read
    end = time.time() + 2
    while not h.closed and time.time() < end:
        time.sleep(0.01)
    assert h.closed
