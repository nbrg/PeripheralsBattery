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
