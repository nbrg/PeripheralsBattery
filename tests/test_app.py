import pytest

from peribatt import style
from peribatt.app import PLACEHOLDER, App, describe
from peribatt.config import Store
from peribatt.model import HEADSET, KEYBOARD, MOUSE, Reading


class FakeBackend:
    icon_size = 24

    def __init__(self):
        self.icons = {}          # key -> (image, title)
        self.notes = []
        self.image_changes = 0

    def show(self, key, image, title):
        assert key not in self.icons
        self.icons[key] = [image, title]

    def set_image(self, key, image, cache_key):
        self.icons[key][0] = image
        self.image_changes += 1

    def set_title(self, key, title):
        self.icons[key][1] = title

    def remove(self, key):
        del self.icons[key]

    def notify(self, key, title, message):
        self.notes.append((key, title, message))

    def refresh_menus(self):
        pass


@pytest.fixture
def app(tmp_path):
    return App(Store(tmp_path / "settings.json"), FakeBackend())


MOUSE_R = Reading("logi-1", "PRO Wireless", MOUSE, 76)
HEADSET_R = Reading("hyperx-16ea", "HyperX Cloud Flight S", HEADSET, 45)
K8 = Reading("bt-k8", "Keychron K8 Pro", KEYBOARD, 60)


def colour_of(image):
    """Frame colour: the pixel in the middle of the left edge's frame."""
    return image.getpixel((0, image.height // 2))[:3]


def near(a, b, tol=12):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def test_placeholder_until_first_device(app):
    app.apply([])
    assert list(app.backend.icons) == [PLACEHOLDER]
    app.apply([MOUSE_R])
    assert list(app.backend.icons) == ["logi-1"]


def test_one_icon_per_device_with_tooltips(app):
    app.apply([MOUSE_R, HEADSET_R, K8])
    icons = app.backend.icons
    assert set(icons) == {"logi-1", "hyperx-16ea", "bt-k8"}
    assert icons["logi-1"][1] == "PRO Wireless: 76%"
    assert icons["bt-k8"][1] == "Keychron K8 Pro: 60%"


@pytest.mark.parametrize("reading,colour", [
    (MOUSE_R.with_(level=10), style.RED),
    (MOUSE_R.with_(level=25), style.YELLOW),
    (MOUSE_R.with_(level=76), style.WHITE),
    (MOUSE_R.with_(level=10, charging=True), style.GREEN),
])
def test_frame_colours(app, reading, colour):
    app.apply([reading])
    assert near(colour_of(app.backend.icons["logi-1"][0]), colour)


def test_missing_device_turns_grey_and_keeps_level(app):
    app.apply([MOUSE_R])
    app.apply([])
    r = app.readings["logi-1"]
    assert not r.online and r.level == 76
    img, title = app.backend.icons["logi-1"]
    red, green, blue = colour_of(img)
    assert red == green == blue
    assert title == "PRO Wireless: not found, last 76%"


def test_offline_reading_without_level_keeps_last_level(app):
    app.apply([MOUSE_R])
    app.apply([MOUSE_R.with_(online=False, level=None, note="switched off")])
    assert app.backend.icons["logi-1"][1] == "PRO Wireless: switched off, last 76%"


def test_known_devices_restored_grey_at_startup(tmp_path):
    store = Store(tmp_path / "s.json")
    store.devices["logi-1"] = {"name": "PRO Wireless", "kind": MOUSE, "level": 50}
    a = App(store, FakeBackend())
    a.apply([])
    assert "logi-1" in a.backend.icons and not a.readings["logi-1"].online


def test_muted_headset_blinks(app):
    app.apply([HEADSET_R.with_(muted=True)])
    assert app.flashing(app.readings["hyperx-16ea"])
    frames = set()
    for _ in range(4):
        assert app.tick_flash() is True
        frames.add(app.backend.icons["hyperx-16ea"][0].tobytes())
    assert len(frames) == 2                           # frame on / frame off
    assert "mic muted" in app.backend.icons["hyperx-16ea"][1]
    app.update(HEADSET_R.with_(muted=False))
    app.tick_flash()
    assert app.tick_flash() is False and app.flash_on


def test_blink_can_be_disabled(app):
    app.store["flash_on_mute"] = False
    app.apply([HEADSET_R.with_(muted=True)])
    assert app.tick_flash() is False


def test_windows_mute_applies_to_headsets_only(app):
    app.apply([MOUSE_R, HEADSET_R])
    app.set_windows_muted(True)
    assert app.flashing(app.readings["hyperx-16ea"])
    assert not app.flashing(app.readings["logi-1"])
    app.store["windows_mute"] = False
    assert not app.flashing(app.readings["hyperx-16ea"])


def test_low_battery_alert_once_and_rearms_after_charging(app):
    app.store["alert_at"] = 15
    for level in (16, 15, 14, 12):
        app.update(MOUSE_R.with_(level=level))
    assert [n[1] for n in app.backend.notes] == ["Low battery"]
    app.update(MOUSE_R.with_(level=12, charging=True))
    app.update(MOUSE_R.with_(level=11))
    assert [n[1] for n in app.backend.notes] == ["Low battery", "Low battery"]


def test_full_notification(app):
    app.update(MOUSE_R.with_(level=99, charging=True))
    app.update(MOUSE_R.with_(level=100, charging=True))
    app.update(MOUSE_R.with_(level=100, charging=True))
    assert [n[1] for n in app.backend.notes] == ["Fully charged"]


def test_hide_and_unhide(app):
    app.apply([MOUSE_R, K8])
    app.hide("bt-k8")
    assert "bt-k8" not in app.backend.icons
    app.apply([MOUSE_R, K8])
    assert "bt-k8" not in app.backend.icons
    app.unhide_all()
    assert "bt-k8" in app.backend.icons


def test_switching_off_a_source_hides_its_devices(app):
    app.apply([MOUSE_R, K8])
    app.set_setting("bluetooth", False)
    assert set(app.backend.icons) == {"logi-1"}


def test_forget_offline(app):
    app.apply([MOUSE_R, K8])
    app.apply([K8])
    app.forget_offline()
    assert set(app.backend.icons) == {"bt-k8"} and "logi-1" not in app.store.devices


def test_unchanged_state_does_not_redraw(app):
    app.apply([MOUSE_R])
    app.apply([MOUSE_R])
    assert app.backend.image_changes == 0


def test_poll_once_survives_a_broken_source(app):
    class Broken:
        name = "broken"

        def poll(self):
            raise RuntimeError("boom")

    class Good:
        def poll(self):
            return [MOUSE_R]
    app.sources = [Broken(), Good()]
    assert app.poll_once() == [MOUSE_R]
    assert (app.store.path).exists()


def test_theme_change_redraws(tmp_path):
    light = {"v": False}
    a = App(Store(tmp_path / "s.json"), FakeBackend(), light_taskbar=lambda: light["v"])
    a.sources = [type("S", (), {"poll": lambda self: [MOUSE_R]})()]
    a.poll_once()
    light["v"] = True
    a.poll_once()
    assert near(colour_of(a.backend.icons["logi-1"][0]), style.CHARCOAL)


def test_describe_variants():
    assert describe(HEADSET_R.with_(charging=True, muted=True), "full in ~40m") == \
        "HyperX Cloud Flight S: 45% - charging - mic muted - full in ~40m"
    assert describe(MOUSE_R.with_(level=100, charging=True)) == "PRO Wireless: 100% - full, on charger"
    assert describe(MOUSE_R.with_(level=None)) == "PRO Wireless: level unknown"
    assert describe(Reading("x", "Controller 1", "gamepad", 60, note="approximate")) == "Controller 1: ~60%"
    assert len(describe(MOUSE_R.with_(name="x" * 300))) <= 127
