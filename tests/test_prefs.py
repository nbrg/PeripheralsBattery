import pytest

from peribatt import prefs
from peribatt.app import App
from peribatt.config import Store
from peribatt.model import KEYBOARD, MOUSE, Reading

from .test_app import FakeBackend


@pytest.fixture
def app(tmp_path):
    a = App(Store(tmp_path / "settings.json"), FakeBackend())
    a.apply([Reading("logi-1", "PRO Wireless", MOUSE, 76),
             Reading("bt-k8", "Keychron K8 Pro", KEYBOARD, 40, charging=True)])
    return a


def test_every_option_is_a_real_setting(app):
    values = prefs.current(app.store, autostart_enabled=lambda: True)
    assert values["autostart"] is True
    for o in prefs.options():
        assert o.key in values


def test_validate_numbers_choices_and_limits():
    clean, errors = prefs.validate({"low": "25%", "warn": " 40", "poll_seconds": "120",
                                    "notify_full": 0, "headsetcontrol": ' "C:\\hc.exe" '})
    assert errors == []
    assert clean == {"low": 25, "warn": 40, "poll_seconds": 120, "notify_full": False,
                     "headsetcontrol": "C:\\hc.exe"}
    _, errors = prefs.validate({"low": "abc"})
    assert "whole number" in errors[0]
    _, errors = prefs.validate({"low": 90})
    assert "between" in errors[0]
    _, errors = prefs.validate({"low": 30, "warn": 30})
    assert errors == ["The yellow limit must be above the red limit"]
    _, errors = prefs.validate({"poll_seconds": 7})
    assert "unknown choice" in errors[0]


def test_apply_changes_settings_and_autostart(app):
    calls = []
    prefs.apply(app, {"warn": 50, "autostart": False}, set_autostart=calls.append)
    assert app.store["warn"] == 50 and calls == [False]
    assert app.refresh_event.is_set()
    assert Store.load(app.store.path.parent)["warn"] == 50       # persisted


def test_device_rows_rename_hide_forget(app):
    rows = {r.key: r for r in prefs.device_rows(app)}
    assert rows["bt-k8"].status == "40%, charging" and not rows["bt-k8"].hidden
    app.rename("bt-k8", "My keyboard")
    assert app.backend.icons["bt-k8"][1].startswith("My keyboard: 40%")
    assert {r.key: r for r in prefs.device_rows(app)}["bt-k8"].original == "Keychron K8 Pro"
    app.rename("bt-k8", "  ")                                   # empty = back to original
    assert app.backend.icons["bt-k8"][1].startswith("Keychron K8 Pro")
    prefs.set_hidden(app, "bt-k8", True)
    assert "bt-k8" not in app.backend.icons
    prefs.set_hidden(app, "bt-k8", False)
    assert "bt-k8" in app.backend.icons
    assert prefs.forget(app, "logi-1") == "Only disconnected devices can be forgotten."
    app.apply([Reading("bt-k8", "Keychron K8 Pro", KEYBOARD, 40)])       # mouse went away
    assert prefs.forget(app, "logi-1") is None
    assert "logi-1" not in app.readings and "logi-1" not in app.store.devices
    assert prefs.forget(app, "nope") is None


def test_renamed_device_is_used_in_notifications(app):
    app.rename("logi-1", "Work mouse")
    app.update(Reading("logi-1", "PRO Wireless", MOUSE, 5))
    assert "Work mouse is at 5%" in app.backend.notes[-1][2]
