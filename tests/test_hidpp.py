import pytest

from peribatt import hidpp
from peribatt.hidpp import (
    F_STATUS,
    F_UNIFIED,
    F_VOLTAGE,
    HidppError,
    LogitechSource,
    build_request,
    decode_battery,
    match_reply,
    voltage_to_percent,
)
from peribatt.model import MOUSE

from .fakes import FakeApi, FakeClock, FakeHidppChannel, FakeLogiDevice, QueueHandle

RECEIVER_PID, WIRED_PID = 0xC539, 0xC088


def test_build_request_layout():
    req = build_request(1, 0x06, 1, (0xAA,))
    assert req[:5] == [0x11, 0x01, 0x06, 0x1B, 0xAA]
    assert len(req) == 20
    with pytest.raises(ValueError):
        build_request(1, 0, 0, range(20))


def test_match_reply_ok_unrelated_and_errors():
    ok = [0x11, 1, 6, 0x0B, 7, 8] + [0] * 14
    assert match_reply(ok, 1, 6, 0)[:2] == [7, 8]
    assert match_reply(ok, 2, 6, 0) is None                    # other device
    assert match_reply([0x11, 1, 6, 0x1B] + [0] * 16, 1, 6, 0) is None   # other function
    with pytest.raises(HidppError) as e:
        match_reply([0x10, 1, 0x8F, 6, 0x0B, 0x09, 0], 1, 6, 0)
    assert e.value.legacy and e.value.code == 0x09
    with pytest.raises(HidppError) as e:
        match_reply([0x11, 1, 0xFF, 6, 0x0B, 0x05] + [0] * 14, 1, 6, 0)
    assert not e.value.legacy


@pytest.mark.parametrize("mv,pct", [(4200, 100), (4186, 100), (3811, 50), (3500, 0),
                                    (3000, 0), (3835, 55)])
def test_voltage_curve(mv, pct):
    assert voltage_to_percent(mv) == pct


def test_voltage_curve_is_monotonic():
    values = [voltage_to_percent(mv) for mv in range(3400, 4300, 5)]
    assert values == sorted(values)


def test_decode_each_battery_feature():
    assert decode_battery(F_VOLTAGE, [0x0F, 0x03, 0x00]) == (57, False)   # 3843 mV
    assert decode_battery(F_VOLTAGE, [0x0F, 0x03, 0x80]) == (57, True)
    assert decode_battery(F_VOLTAGE, [0x00, 0x00, 0x00]) == (None, False)
    assert decode_battery(F_UNIFIED, [64, 4, 0, 0]) == (64, False)
    assert decode_battery(F_UNIFIED, [0, 2, 1, 1]) == (20, True)          # level from flags
    assert decode_battery(F_STATUS, [80, 50, 1]) == (80, True)
    assert decode_battery(F_STATUS, [0, 0, 0]) == (None, False)


def receiver(devices, pid=RECEIVER_PID, with_short=True):
    api = FakeApi()
    short = QueueHandle() if with_short else None
    long_h = FakeHidppChannel(devices, short)
    api.add(0x046D, pid, b"long", long_h, 0xFF00, 0x0002)
    if with_short:
        api.add(0x046D, pid, b"short", short, 0xFF00, 0x0001)
    api.add(0x046D, pid, b"kbd", QueueHandle(), 0x0001, 0x0006)   # ignored collection
    return api, long_h


def test_reads_g_pro_wireless_through_receiver():
    mouse = FakeLogiDevice(battery=(0x0F, 0x03, 0x00))
    api, _ = receiver({1: mouse})
    src = LogitechSource(api=api, clock=FakeClock())
    [r] = src.poll()
    assert (r.key, r.name, r.kind, r.level, r.charging, r.online) == \
        ("logi-1a2b3c4d", "PRO Wireless", MOUSE, 57, False, True)
    assert src.known["c539:1"]["name"] == "PRO Wireless"


def test_second_poll_skips_discovery():
    mouse = FakeLogiDevice()
    api, _ = receiver({1: mouse})
    src = LogitechSource(api=api, clock=FakeClock())
    src.poll()
    first = mouse.calls
    src.poll()
    assert mouse.calls - first == 2                   # just a wake-up ping and the battery request


def test_switched_off_mouse_stays_as_offline_reading():
    mouse = FakeLogiDevice()
    api, _ = receiver({1: mouse})
    src = LogitechSource(api=api, clock=FakeClock())
    src.poll()
    mouse.online = False
    [r] = src.poll()
    assert not r.online and r.note == "switched off" and r.key == "logi-1a2b3c4d"


def test_known_mouse_restored_from_settings_while_receiver_unplugged():
    src = LogitechSource(api=FakeApi(), known={"c539:1": {"key": "logi-x", "name": "PRO", "kind": "mouse"}})
    [r] = src.poll()
    assert not r.online and r.note == "not connected"


def test_unpaired_slot_is_forgotten():
    api, _ = receiver({})
    src = LogitechSource(api=api, clock=FakeClock(),
                         known={"c539:1": {"key": "logi-x", "name": "PRO", "kind": "mouse"}})
    assert src.poll() == []
    assert src.known == {}


def test_timeout_without_short_collection_counts_as_offline():
    mouse = FakeLogiDevice()
    api, long_h = receiver({1: mouse}, with_short=False)
    src = LogitechSource(api=api, clock=FakeClock(step=0.2))
    src.poll()
    mouse.online = False       # error replies go to the (unopened) short collection
    [r] = src.poll()
    assert not r.online


def test_charging_on_cable_merges_with_receiver_copy():
    from peribatt.model import merge
    wired_mouse = FakeLogiDevice(battery=(0x0F, 0x03, 0x80))
    api, _ = receiver({1: FakeLogiDevice(online=False)})
    api.add(0x046D, WIRED_PID, b"wired", FakeHidppChannel({0xFF: wired_mouse}), 0xFF00, 0x0002)
    src = LogitechSource(api=api, clock=FakeClock())
    [r] = merge(src.poll())
    # on the cable, charging: the voltage is the charger's, not a level (none known yet)
    assert r.online and r.charging and r.level is None


def test_unified_battery_device():
    kb = FakeLogiDevice(name="G915", dev_type=0, battery_feature=F_UNIFIED, battery=(88, 8, 0, 0))
    api, _ = receiver({2: kb})
    [r] = LogitechSource(api=api, clock=FakeClock()).poll()
    assert (r.kind, r.level) == ("keyboard", 88)


def test_open_failure_is_contained():
    api = FakeApi()
    api.add(0x046D, RECEIVER_PID, b"long", OSError("access denied"), 0xFF00, 0x0002)
    src = LogitechSource(api=api)
    assert src.poll() == []
    assert any("open failed" in line for line in src.log)


def test_slug():
    assert hidpp.slug("G PRO Wireless!") == "g-pro-wireless"
    assert hidpp.slug("***") == "device"


def test_receiver_instance_from_a_windows_path():
    p = rb"\\?\HID#VID_046D&PID_C539&MI_02&Col02#7&2b1f0a3&0&0001#{4d1e55b2-f16f-11cf-88cb-001111000030}"
    assert hidpp.instance(p) == "7&2b1f0a3&0"
    assert hidpp.instance(p.replace(b"Col02", b"Col01").replace(b"&0001#", b"&0000#")) == "7&2b1f0a3&0"
    assert hidpp.instance(b"/dev/hidraw3") == ""


def test_two_receivers_of_the_same_kind_are_both_read():
    def path(inst, col):
        return rf"\\?\HID#VID_046D&PID_C52B&MI_02&Col0{col}#{inst}&000{col - 1}#{{x}}".encode()

    api = FakeApi()
    for inst, dev in (("7&aaa&0", FakeLogiDevice(name="MX Keys", dev_type=0, unit=b"\x01\x02\x03\x04")),
                      ("7&bbb&0", FakeLogiDevice(name="MX Master 3", unit=b"\x05\x06\x07\x08"))):
        long_h = FakeHidppChannel({1: dev})
        api.add(0x046D, 0xC52B, path(inst, 2), long_h, 0xFF00, 0x0002)
    names = sorted(r.name for r in LogitechSource(api=api, clock=FakeClock()).poll())
    assert names == ["MX Keys", "MX Master 3"]


def test_a_late_reply_to_an_earlier_request_is_not_taken_as_the_answer():
    from peribatt.hidpp import Channel

    class Late(QueueHandle):
        """Answers each request only after the next one was sent."""
        pending = None

        def on_write(self, data):
            if self.pending:
                self.inbox.append(self.pending)
            self.pending = [*data[:4], 42] + [0] * 15

    ch = Channel(Late(), clock=FakeClock())
    with pytest.raises(TimeoutError):
        ch.call(1, 0x06, 0, timeout=0.2)
    with pytest.raises(TimeoutError):
        ch.call(1, 0x06, 0, timeout=0.2)          # the first request's answer is not ours


# --- a voltage on the charger is not a battery level -------------------------------

def test_plugging_in_does_not_read_as_full():
    """A G PRO Wireless at 74% reported 4211 mV the moment its cable went in, which
    the voltage curve reads as 100% - and a 'fully charged' notification followed."""
    mouse = FakeLogiDevice(battery=(0x0F, 0x6A, 0x00))          # 3946 mV on battery
    api, _ = receiver({1: mouse})
    src = LogitechSource(api=api, clock=FakeClock())
    [r] = src.poll()
    on_battery = r.level
    assert 70 <= on_battery <= 80 and not r.charging
    mouse.battery = [0x10, 0x73, 0x80]                            # 4211 mV, charging, not full
    [r] = src.poll()
    assert r.charging and r.level == on_battery and r.note == "approximate"
    mouse.battery = [0x10, 0x68, 0x81]                            # the mouse says: full
    [r] = src.poll()
    assert r.charging and r.level == 100


def test_the_last_level_on_battery_survives_a_restart():
    mouse = FakeLogiDevice(battery=(0x0F, 0x6A, 0x00))
    api, _ = receiver({1: mouse})
    known = {}
    LogitechSource(api=api, known=known, clock=FakeClock()).poll()
    mouse.battery = [0x10, 0x73, 0x80]
    [r] = LogitechSource(api=api, known=known, clock=FakeClock()).poll()    # a new session
    assert r.level is not None and r.level < 100


def test_charging_without_a_known_level_shows_none():
    mouse = FakeLogiDevice(battery=(0x10, 0x73, 0x80))            # first seen on the charger
    api, _ = receiver({1: mouse})
    [r] = LogitechSource(api=api, clock=FakeClock()).poll()
    assert r.charging and r.level is None


def test_charge_estimate_rises_fast_then_slower_and_never_claims_full():
    assert hidpp.charge_estimate(74, 0) == 74
    assert hidpp.charge_estimate(40, 10, rate=1.0) == 50              # fast phase
    assert hidpp.charge_estimate(74, 10, rate=0.8) == 81               # 6 min to the knee, then slower
    assert hidpp.charge_estimate(74, 10_000) == 99                     # 100 only when the device says
    assert hidpp.learned_rate(40, 60, 20) == 1.0
    assert hidpp.learned_rate(40, 60, 5) is None                       # too short to learn from
    # the learned rate reproduces the observed charge
    rate = hidpp.learned_rate(50, 90, 60)
    assert hidpp.charge_estimate(50, 60, rate) == 90


def test_the_level_climbs_while_charging_and_the_rate_is_learned():
    t = {"now": 1_000_000.0}
    mouse = FakeLogiDevice(battery=(0x0F, 0x6A, 0x00))                 # ~74% on battery
    api, _ = receiver({1: mouse})
    known = {}
    src = LogitechSource(api=api, known=known, clock=FakeClock(), now=lambda: t["now"])
    [r] = src.poll()
    start = r.level
    mouse.battery = [0x10, 0x73, 0x80]                                 # plugged in
    src.poll()
    t["now"] += 10 * 60
    [r] = src.poll()
    assert start < r.level < 100 and r.charging and r.note == "approximate"
    t["now"] += 20 * 60                                                 # 30 min of charging...
    mouse.battery = [0x10, 0x40, 0x00]                                 # ...unplugged: a real 4160 mV
    [r] = src.poll()
    assert not r.charging and r.level >= 95
    [info] = known.values()
    assert "charge_start" not in info and info["charge_rate"] > hidpp.DEFAULT_CHARGE_RATE
