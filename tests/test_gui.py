"""The Tk windows, for real. Needs a display (Windows, or Xvfb on Linux);
skipped where Tk cannot start."""
import gc
import json
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")

from peribatt import learn_ui, settings_ui  # noqa: E402
from peribatt.app import App  # noqa: E402
from peribatt.config import Store  # noqa: E402
from peribatt.model import HEADSET, MOUSE, Reading  # noqa: E402
from peribatt.ui import UiThread  # noqa: E402

from .fakes import FakeApi, QueueHandle  # noqa: E402
from .test_app import FakeBackend  # noqa: E402


def _tk_available():
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except tk.TclError:
        return False


pytestmark = pytest.mark.skipif(not _tk_available(), reason="no display for Tk")


@pytest.fixture
def root():
    r = tk.Tk()
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass
    del r
    gc.collect()        # finalise Tk objects on this thread, not on some later one


def pump(root, until=lambda: False, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until():
            return True
        time.sleep(0.005)
    return until()


def widgets(w):
    yield w
    for child in w.winfo_children():
        yield from widgets(child)


def button(win, text):
    return next(w for w in widgets(win) if w.winfo_class() == "TButton" and w.cget("text") == text)


def labels_text(win):
    return " | ".join(str(w.cget("text")) for w in widgets(win) if w.winfo_class() == "TLabel")


@pytest.fixture
def app(tmp_path):
    a = App(Store(tmp_path / "settings.json"), FakeBackend())
    a.apply([Reading("logi-1", "PRO Wireless", MOUSE, 76), Reading("hx", "Cloud Flight S", HEADSET, 40)])
    return a


def test_settings_window_saves_and_validates(root, app):
    calls = []
    win = settings_ui.open_settings(root, app, open_learn=lambda: calls.append("learn"),
                                    autostart_enabled=lambda: False, set_autostart=calls.append)
    pump(root, timeout=0.2)
    w = win._widgets
    assert set(w["tree"].get_children()) == {"logi-1", "hx"}
    w["vars"]["warn"].set("10")                      # below red: rejected
    w["save"]()
    assert "yellow limit" in w["error"].cget("text") and win.winfo_exists()
    w["vars"]["warn"].set("45")
    w["vars"]["poll_seconds"].set("5 minutes")
    w["vars"]["autostart"].set(True)
    button(win, "Learn a new device…").invoke()
    w["save"]()
    assert app.store["warn"] == 45 and app.store["poll_seconds"] == 300
    assert calls == ["learn", True]
    assert not win.winfo_exists()


def test_settings_device_actions(root, app):
    win = settings_ui.open_settings(root, app, autostart_enabled=lambda: False)
    tree = win._widgets["tree"]
    tree.selection_set("logi-1")
    button(win, "Hide / show").invoke()
    assert "logi-1" in app.store["hidden"] and tree.set("logi-1", "shown") == "no"
    win.destroy()


class MysteryDongle(QueueHandle):
    """An unsupported headset: answers the HP-HyperX battery query with 57%."""

    def on_write(self, data):
        if data[:4] == bytes([0x06, 0xFF, 0xBB, 0x02]):
            self.inbox.append([0x06, 0xFF, 0xBB, 0x02, 0, 0x0F, 0x10, 57] + [0] * 12)


def test_learn_wizard_end_to_end(root, tmp_path, monkeypatch):
    api = FakeApi()
    dongle = MysteryDongle()
    api.add(0x1234, 0x0001, b"v", dongle, usage_page=0xFF00, product="Mystery Headset")
    api.add(0x046D, 0xC539, b"l", QueueHandle(), usage_page=0xFF00, product="USB Receiver")
    saved = []
    wiz = learn_ui.Wizard(root, api=api, recipes_path=tmp_path / "recipes.json",
                          is_supported=lambda v, p: v == 0x046D, on_saved=saved.append, tick_ms=2)
    monkeypatch.setattr(learn_ui, "BATTERY_SECONDS", 1500)     # 3 s at 2 ms per "second"
    # page 1: only the unsupported device is offered
    assert len(wiz.tree.get_children()) == 1
    wiz.tree.selection_set("0")
    button(wiz.win, "Next").invoke()
    # page 2: type 57, listen (known queries are sent in the background)
    entry = next(w for w in widgets(wiz.win) if w.winfo_class() == "TEntry")
    entry.insert(0, "57")
    button(wiz.win, "Listen").invoke()
    assert pump(root, lambda: "Battery:" in labels_text(wiz.win), timeout=10)
    assert wiz.findings.level is not None and wiz.findings.level.byte == 7
    button(wiz.win, "Next").invoke()
    # page 3: the headset pushes a mute report whenever the phase says so
    button(wiz.win, "Start").invoke()

    def push():
        ph = wiz.capture.phase
        if ph in ("unmuted", "unmuted2", "muted"):
            wiz.capture.feed(0, [0x0B, 0, 0xBB, 0x08, 1 if ph == "muted" else 0, 0, 0, 0])
        return "Mute:" in labels_text(wiz.win)
    assert pump(root, push, timeout=10)
    assert wiz.findings.muted.spec == {"byte": 4, "in": [1]}
    button(wiz.win, "Next").invoke()
    button(wiz.win, "Skip").invoke()               # charging
    # page 5: save
    assert "Mystery Headset" in wiz.saved_text.get("1.0", "end")
    button(wiz.win, "Save").invoke()
    [recipe] = json.loads((tmp_path / "recipes.json").read_text())
    assert saved == [recipe]
    assert recipe["steps"][0]["write"] == "06 ff bb 02" and recipe["kind"] == HEADSET
    assert recipe["listen"][0]["muted"] == {"byte": 4, "in": [1]}
    assert dongle.closed


def test_wizard_reports_when_nothing_is_found(root, tmp_path):
    api = FakeApi()
    api.add(0x1234, 0x0001, b"v", QueueHandle(), usage_page=0xFF00, product="Silent")
    wiz = learn_ui.Wizard(root, api=api, recipes_path=tmp_path / "r.json", tick_ms=1)
    wiz.tree.selection_set("0")
    button(wiz.win, "Next").invoke()
    button(wiz.win, "Skip").invoke()
    button(wiz.win, "Skip").invoke()
    button(wiz.win, "Skip").invoke()
    assert "Nothing recognised" in labels_text(wiz.win)
    button(wiz.win, "Close").invoke()
    assert not (tmp_path / "r.json").exists()


def test_ui_thread_lives_only_while_windows_are_open():
    gc.collect()
    ui = UiThread()
    opened = threading.Event()

    def factory(root):
        win = tk.Toplevel(root)
        opened.set()
        win.after(200, win.destroy)
        return win
    ui.show("w", factory)
    assert opened.wait(5) and ui.running
    end = time.time() + 5
    while ui.running and time.time() < end:
        time.sleep(0.05)
    assert not ui.running
    opened.clear()
    ui.show("w", factory)                             # a second life works too
    assert opened.wait(5)
