"""Device settings: DPI, polling rate, onboard profiles (Logitech), Razer, HyperX."""
import pytest

from peribatt import logi_controls
from peribatt.controls import CHOICE, RANGE, TOGGLE, Control, Unavailable
from peribatt.hidpp import LogitechSource

from .fakes import FakeApi, FakeClock, FakeHidppChannel, FakeLogiDevice


def logitech(**kw):
    mouse = FakeLogiDevice(settings=True, **kw)
    api = FakeApi()
    api.add(0x046D, 0xC539, b"long", FakeHidppChannel({1: mouse}), 0xFF00, 0x0002)
    src = LogitechSource(api=api, clock=FakeClock())
    [r] = src.poll()
    return src, mouse, r.key


def by_id(controls):
    return {c.id: c for c in controls}


# --- the Control model ----------------------------------------------------------

def test_range_values_snap_to_the_device_step():
    c = Control("dpi", "Sensitivity", RANGE, 800, 100, 25600, 50)
    assert c.check(1234) == 1250
    with pytest.raises(ValueError):
        c.check(30000)
    with pytest.raises(ValueError):
        c.check(True)                                  # a bool is not a number here


def test_choices_and_toggles_are_checked():
    c = Control("rate", "Polling rate", CHOICE, 1, options=[(1, "1000 Hz"), (8, "125 Hz")])
    assert c.check(8) == 8
    with pytest.raises(ValueError):
        c.check(3)
    t = Control("onboard", "Onboard profiles", TOGGLE, True)
    with pytest.raises(ValueError):
        t.check(1)
    assert t.to_json()["type"] == "toggle"


# --- Logitech ----------------------------------------------------------------------

def test_dpi_list_parsing():
    ranged = [0, 0x00, 0x64, 0xE0, 0x32, 0x64, 0x00] + [0] * 9
    listed = [0, 0x01, 0x90, 0x03, 0x20, 0x06, 0x40] + [0] * 9
    assert logi_controls.parse_dpi_list(ranged) == ([100, 25600], 50)
    assert logi_controls.parse_dpi_list(listed) == ([400, 800, 1600], None)
    rates = logi_controls.rate_options(0b10001011)
    assert rates == [(1, "1000 Hz"), (2, "500 Hz"), (4, "250 Hz"), (8, "125 Hz")]


def test_logitech_mouse_settings_are_read():
    src, mouse, key = logitech()
    assert src.has_controls(key)
    c = by_id(src.controls(key))
    dpi = c["dpi"]
    assert (dpi.type, dpi.value, dpi.min, dpi.max, dpi.step) == (RANGE, 800, 100, 25600, 50)
    assert c["rate"].value == 1 and [v for v, _ in c["rate"].options] == [1, 2, 4, 8]
    assert c["onboard"].value is True


def test_logitech_dpi_and_rate_are_written_and_read_back():
    src, mouse, key = logitech()
    after = by_id(src.set_control(key, "dpi", 1600))
    assert mouse.dpi == 1600 and after["dpi"].value == 1600
    src.set_control(key, "rate", 2)
    assert mouse.rate == 2
    src.set_control(key, "onboard", False)
    assert mouse.mode == 2


def test_a_change_the_mouse_ignores_says_why():
    src, mouse, key = logitech(onboard_locks=True)
    with pytest.raises(Unavailable, match="Onboard profiles"):
        src.set_control(key, "dpi", 1600)
    src.set_control(key, "onboard", False)
    src.set_control(key, "dpi", 1600)
    assert mouse.dpi == 1600


def test_bad_values_are_refused_before_anything_is_written():
    src, mouse, key = logitech()
    with pytest.raises(ValueError):
        src.set_control(key, "dpi", 99999)
    with pytest.raises(ValueError):
        src.set_control(key, "nope", 1)
    assert mouse.dpi == 800


def test_a_switched_off_mouse_cannot_be_changed():
    src, mouse, key = logitech()
    mouse.online = False
    with pytest.raises(Unavailable, match="switched off"):
        src.controls(key)
    with pytest.raises(Unavailable, match="Switch the device on"):
        src.controls("logi-never-seen")


def test_a_mouse_without_settings_features_offers_none():
    mouse = FakeLogiDevice()
    api = FakeApi()
    api.add(0x046D, 0xC539, b"long", FakeHidppChannel({1: mouse}), 0xFF00, 0x0002)
    src = LogitechSource(api=api, clock=FakeClock())
    [r] = src.poll()
    assert src.has_controls(r.key)                      # not known yet
    assert src.controls(r.key) == []
    assert not src.has_controls(r.key)                  # now it is


# --- Razer ---------------------------------------------------------------------------

def razer():
    from peribatt.razer import RazerSource

    from .fakes import FakeRazer
    dev = FakeRazer(raw=200)
    api = FakeApi()
    api.add(0x1532, 0x00B7, b"r", dev, product="Razer DeathAdder V3 Pro")
    src = RazerSource(api=api, sleep=lambda s: None)
    [r] = src.poll()
    return src, dev, r.key


def test_razer_mouse_dpi_and_polling_rate():
    src, dev, key = razer()
    assert src.has_controls(key)
    c = by_id(src.controls(key))
    assert c["dpi"].value == 1600 and c["rate"].value == 1000
    after = by_id(src.set_control(key, "dpi", 3200))
    assert dev.dpi == 3200 and after["dpi"].value == 3200
    src.set_control(key, "rate", 500)
    assert dev.rate == 0x02
    with pytest.raises(ValueError):
        src.set_control(key, "rate", 250)             # not a Razer rate


def test_razer_asleep_mouse_says_so():
    src, dev, key = razer()
    dev.asleep = True
    with pytest.raises(Unavailable, match="wake"):
        src.controls(key)


def test_razer_headsets_offer_no_mouse_settings():
    from peribatt.razer import RazerSource

    from .fakes import FakeRazer
    api = FakeApi()
    api.add(0x1532, 0x0527, b"h", FakeRazer(raw=100), product="Razer BlackShark V2 Pro")
    src = RazerSource(api=api, sleep=lambda s: None)
    [r] = src.poll()
    assert not src.has_controls(r.key)


# --- HyperX ---------------------------------------------------------------------------

def hyperx(answers=True):
    from peribatt.hyperx import HyperXSource

    from .fakes import QueueHandle

    class Headset(QueueHandle):
        """Answers like the Cloud Flight S dongle (HyperHeadset's protocol)."""
        auto_off, sidetone = 20, False

        def on_write(self, data):
            cmd, payload = data[15], (data[16] if len(data) > 16 else 0)
            if not answers:
                return
            if cmd == 0x02:
                self.inbox.append([0x0B, 0, 0xBB, 2, 0, 0, 0, 70] + [0] * 56)
            elif cmd == 0x18:
                Headset.auto_off = payload
            elif cmd == 0x1A:
                self.inbox.append([0x0B, 0, 0xBB, 0x1A, Headset.auto_off] + [0] * 59)
            elif cmd == 0x19:
                Headset.sidetone = bool(payload)
                self.inbox.append([0x0B, 0, 0xBB, 0x19, payload] + [0] * 59)

    api = FakeApi()
    h = Headset()
    api.add(0x0951, 0x16EA, b"v", h, usage_page=0xFF13, usage=1)
    src = HyperXSource(api=api)
    src.poll()
    import time
    end = time.time() + 2
    while not src.state.online and time.time() < end:
        time.sleep(0.01)
    return src, Headset, src.state.key


def test_hyperx_auto_power_off_and_sidetone():
    src, headset, key = hyperx()
    try:
        assert src.has_controls(key)
        c = by_id(src.controls(key))
        assert c["auto_off"].value == 20 and c["sidetone"].value is None
        src.set_control(key, "auto_off", 30)
        assert headset.auto_off == 30
        after = by_id(src.set_control(key, "sidetone", True))
        assert headset.sidetone is True and after["sidetone"].value is True
    finally:
        src.close()


def test_hyperx_headset_that_is_off_cannot_be_changed():
    from peribatt.hyperx import HyperXSource

    from .fakes import QueueHandle
    api = FakeApi()
    api.add(0x0951, 0x16EA, b"v", QueueHandle(), usage_page=0xFF13, usage=1)
    src = HyperXSource(api=api, threaded=False)
    src.poll()
    with pytest.raises(Unavailable, match="Switch the headset on"):
        src.controls(src.state.key)
