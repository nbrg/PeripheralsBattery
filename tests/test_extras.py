"""Settings file backup, autostart safety, update check, per-device picture, icon
colour and removing switched-off devices."""
import io
import json

import pytest

from peribatt import updates, winshell
from peribatt.app import ALL_OFF, PLACEHOLDER, App
from peribatt.config import Store
from peribatt.model import HEADSET, MOUSE, Reading

from .test_app import FakeBackend

MOUSE_R = Reading("logi-1", "PRO Wireless", MOUSE, 70)


# --- settings file ------------------------------------------------------------------

def test_a_damaged_settings_file_is_kept_as_bad(tmp_path):
    (tmp_path / "settings.json").write_text("{ not json")
    store = Store.load(tmp_path)
    assert store["poll_seconds"] == 60
    assert (tmp_path / "settings.json.bad").read_text() == "{ not json"
    assert not (tmp_path / "settings.json").exists()


def test_float_settings_survive_a_round_trip(tmp_path):
    (tmp_path / "settings.json").write_text('{"settings": {"update_last": 0}}')
    assert Store.load(tmp_path)["update_last"] == 0.0


# --- autostart ----------------------------------------------------------------------

def test_a_copy_in_the_temp_folder_is_recognised(tmp_path):
    temp = tmp_path / "Temp"
    exe = temp / "Rar$EXa1234" / "PeripheralsBattery" / "PeripheralsBattery.exe"
    assert winshell.running_from_temp(str(exe), {str(temp)})
    assert not winshell.running_from_temp(str(tmp_path / "Apps" / "pb.exe"), {str(temp)})
    assert not winshell.running_from_temp(str(tmp_path / "Temperature.exe"), {str(temp)})


def test_autostart_from_a_temp_folder_is_refused(monkeypatch):
    monkeypatch.setattr(winshell, "IS_WINDOWS", True)
    monkeypatch.setattr(winshell, "running_from_temp", lambda *a: True)
    with pytest.raises(winshell.TemporaryFolder, match="Extract the zip"):
        winshell.set_autostart(True)


# --- update check -------------------------------------------------------------------

def test_version_comparison():
    assert updates.newer("v1.0.0", "0.9.9")
    assert updates.newer("0.6.1", "0.6")
    assert not updates.newer("0.6.0", "0.6.0")
    assert not updates.newer("v0.6.0-beta", "0.5.0")            # not a plain release tag


def test_fetch_reads_the_release():
    body = json.dumps({"tag_name": "v9.1.0", "html_url": "https://example/rel"}).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    seen = {}

    def opener(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        return Resp(body)
    assert updates.fetch_latest(opener) == ("9.1.0", "https://example/rel")
    assert seen["ua"].startswith("PeripheralsBattery/")


def checker(tmp_path, version="99.0.0", on=True):
    store = Store(tmp_path / "settings.json")
    store["update_check"] = on
    found = []
    now = {"t": 1_000_000.0}
    c = updates.UpdateChecker(store, lambda v, u, announce: found.append((v, announce)),
                              fetch=lambda: (version, "https://x"), clock=lambda: now["t"])
    return c, store, found, now


def test_a_newer_release_is_announced_once(tmp_path):
    c, store, found, now = checker(tmp_path)
    c.check_if_due()
    now["t"] += updates.EVERY
    c.check_if_due()
    assert found == [("99.0.0", True), ("99.0.0", False)]
    assert store["update_told"] == "99.0.0"


def test_the_check_runs_once_a_day_and_only_when_on(tmp_path):
    c, store, found, now = checker(tmp_path)
    c.check_if_due()
    c.check_if_due()                                  # the same day: not asked again
    assert len(found) == 1
    c2, _, found2, _ = checker(tmp_path, on=False)
    c2.check_if_due()
    assert found2 == []


def test_no_news_is_no_news(tmp_path):
    c, _, found, _ = checker(tmp_path, version="0.0.1")
    c.check_if_due()
    assert found == [] and c.latest is None


def test_an_offline_check_is_quiet(tmp_path):
    c, _, found, _ = checker(tmp_path)

    def fail():
        raise OSError("no network")
    c.fetch = fail
    c.check_if_due()
    assert found == [] and "no network" in c.error


def test_the_app_tells_about_an_update_once(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    app.found_update("99.0.0", "https://x", announce=True)
    app.found_update("99.0.0", "https://x", announce=False)
    assert app.update_available == ("99.0.0", "https://x")
    assert len(app.backend.notes) == 1


# --- per-device picture and icon colour -----------------------------------------------

def test_a_device_can_be_given_another_picture(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    app.apply([MOUSE_R])
    before = app.backend.icons["logi-1"][0].tobytes()
    app.set_kind("logi-1", HEADSET)
    assert app.effective(app.readings["logi-1"]).kind == HEADSET
    assert app.backend.icons["logi-1"][0].tobytes() != before
    app.set_kind("logi-1", "")
    assert app.backend.icons["logi-1"][0].tobytes() == before
    with pytest.raises(ValueError):
        app.set_kind("logi-1", "toaster")


def test_a_fixed_icon_colour_beats_the_taskbar_theme(tmp_path):
    app = App(Store(tmp_path / "settings.json"), FakeBackend(), light_taskbar=lambda: True)
    assert app.light is True
    app.set_setting("icon_colour", "white")
    assert app.light is False
    app.set_setting("icon_colour", "black")
    assert app.light is True
    app.poll_once()                                    # the theme probe does not undo it
    assert app.light is True


# --- switched-off devices ---------------------------------------------------------------

def test_switched_off_devices_leave_the_tray_after_a_while(tmp_path):
    now = {"t": 1000.0}
    app = App(Store(tmp_path / "settings.json"), FakeBackend(), clock=lambda: now["t"])
    app.store["hide_off_after"] = 5
    app.apply([MOUSE_R])
    app.apply([MOUSE_R.with_(online=False, note="switched off")])
    now["t"] += 4 * 60
    app.apply([MOUSE_R.with_(online=False, note="switched off")])
    assert "logi-1" in app.backend.icons                # still grey
    now["t"] += 2 * 60
    app.apply([MOUSE_R.with_(online=False, note="switched off")])
    assert "logi-1" not in app.backend.icons
    assert app.backend.icons[PLACEHOLDER][1] == ALL_OFF
    app.apply([MOUSE_R])                                # switched on again
    assert "logi-1" in app.backend.icons and PLACEHOLDER not in app.backend.icons


def test_by_default_switched_off_devices_stay(tmp_path):
    now = {"t": 1000.0}
    app = App(Store(tmp_path / "settings.json"), FakeBackend(), clock=lambda: now["t"])
    app.apply([MOUSE_R.with_(online=False)])
    now["t"] += 10 ** 6
    app.apply([MOUSE_R.with_(online=False)])
    assert "logi-1" in app.backend.icons
