"""Calls the real Windows APIs (ctypes) - runs on the Windows CI runner only.
These catch wrong signatures or struct layouts, which would crash rather than fail."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows APIs")


def test_setupapi_bluetooth_enumeration_runs():
    from peribatt.bluetooth import BluetoothSource, _SetupApi
    records = _SetupApi().records()
    assert isinstance(records, list)
    for _cid, name, level, started in records:
        assert isinstance(name, str) and isinstance(level, int) and isinstance(started, bool)
    assert isinstance(BluetoothSource().poll(), list)


def test_xinput_runs():
    from peribatt.xinput import XInputSource
    assert isinstance(XInputSource().poll(), list)


def test_core_audio_mic():
    from peribatt.micmute import CoreAudioMic
    mic = CoreAudioMic()
    try:
        assert mic.muted() in (True, False)
    except OSError:
        pass                     # CI runners usually have no microphone at all


def test_shell_helpers():
    from peribatt import winshell
    assert isinstance(winshell.taskbar_is_light(), bool)
    assert 12 <= winshell.tray_icon_size() <= 128
    assert winshell.launch_command().startswith('"')
    assert isinstance(winshell.autostart_enabled(), bool)


def test_real_hidapi_enumerates():
    from peribatt.hidio import HidApi
    assert isinstance(HidApi().enumerate(0), list)


def test_power_notifications_register_and_unregister():
    from peribatt.power import PowerWatcher
    w = PowerWatcher(lambda: None)
    assert w.start() is True
    w.stop()
    assert not w.registered


def test_real_tray_backend(tmp_path, caplog):
    """The real pystray backend on Windows: create, update, notify, menus, remove."""
    import logging
    import time

    from peribatt import style
    from peribatt.app import App
    from peribatt.config import Store
    from peribatt.model import MOUSE, Reading
    from peribatt.render import render
    from peribatt.tray import PystrayBackend

    backend = PystrayBackend(16)
    app = App(Store(tmp_path / "settings.json"), backend)
    backend.app = app
    with caplog.at_level(logging.WARNING, logger="peribatt"):
        app.start()
        app.apply([Reading("logi-1", "PRO Wireless", MOUSE, 70)])
        time.sleep(1.0)                                  # let pystray's threads run
        app.apply([Reading("logi-1", "PRO Wireless", MOUSE, 10)])
        backend.set_image("logi-1", render(16, MOUSE, 5, style.RED), ())
        backend.notify("logi-1", "Low battery", "test")
        backend.refresh_menus()
        time.sleep(0.5)
        for key in list(backend.icons):
            backend.remove(key)
    assert "could not" not in caplog.text and "Traceback" not in caplog.text
    # every icon thread ends - including the "searching" icon, removed right after it was made
    import threading
    end = time.time() + 10
    while any(t.name.startswith("tray-") for t in threading.enumerate()) and time.time() < end:
        time.sleep(0.1)
    assert not [t.name for t in threading.enumerate() if t.name.startswith("tray-")]


def test_icons_are_built_in_memory_with_stable_ids(tmp_path):
    """The real Win32 icon path: no temp .ico files, a stable id, one handle per frame."""
    import tempfile
    import time
    from pathlib import Path

    from peribatt import style, trayicon
    from peribatt.app import App
    from peribatt.config import Store
    from peribatt.model import HEADSET, Reading
    from peribatt.render import render
    from peribatt.tray import PystrayBackend

    h = trayicon.create_hicon(render(32, HEADSET, 60, style.WHITE))
    assert h
    trayicon.destroy_hicon(h)

    before = set(Path(tempfile.gettempdir()).glob("*.ico"))
    backend = PystrayBackend(16)
    app = App(Store(tmp_path / "settings.json"), backend)
    backend.app = app
    app.apply([Reading("hx", "Headset", HEADSET, 50)])
    time.sleep(1.0)
    icon = backend.icons["hx"]
    assert icon._pb_uid == trayicon.icon_uid("hx")
    frames = [render(16, HEADSET, 50, c) for c in (style.WHITE, style.GREY)]
    for _ in range(10):                                   # a mute blink
        for f in frames:
            backend.set_image("hx", f, ())
    time.sleep(0.5)
    assert icon._pb_handles.made <= 4                     # the two frames, plus the first image
    assert set(Path(tempfile.gettempdir()).glob("*.ico")) - before == set()
    for key in list(backend.icons):
        backend.remove(key)
