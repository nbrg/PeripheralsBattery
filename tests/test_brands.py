"""The brand recipes and the recipe engine features they use, against simulated
devices that answer the way each protocol's source documents."""
import pytest

from peribatt import hidpp
from peribatt import recipes as rc
from peribatt.hidpp import LogitechSource
from peribatt.model import HEADSET

from .fakes import FakeApi, FakeClock, FakeHidppChannel, FakeLogiDevice, QueueHandle

BUNDLED = {(r.vendor_id, p): r for r in rc.load() for p in r.product_ids}
VENDOR_WIDE = {r.vendor_id: r for r in rc.load() if r.any_product}


def recipe_for(vid, pid):
    return BUNDLED.get((vid, pid)) or VENDOR_WIDE[vid]


class Device(QueueHandle):
    """Answers output and feature reports with ``reply(request)`` (None = silence)."""

    def __init__(self, reply=None, feature_reply=None, pushes=()):
        super().__init__()
        self.reply, self.feature_reply = reply, feature_reply
        self.features_sent = []
        self.inbox.extend(pushes)

    def on_write(self, data):
        out = self.reply(data) if self.reply else None
        if out is not None:
            self.inbox.append(out)

    def send_feature_report(self, data):
        self.features_sent.append(bytes(data))
        if self.reply is not None:                    # e.g. Keychron: feature request, input reply
            out = self.reply(bytes(data))
            if out is not None:
                self.inbox.append(out)
        return len(data)

    def get_feature_report(self, rid, n):
        return self.feature_reply(self.features_sent[-1]) if self.feature_reply else []


def read(vid, pid, dev, product="", **info):
    api = FakeApi()
    api.add(vid, pid, b"p", dev, product=product, **info)
    src = rc.RecipeSource([recipe_for(vid, pid)], api=api, clock=FakeClock(step=0.01), threaded=False)
    src.sleep = lambda s: None
    return src.poll()


def frame(*head, size=64, at=None):
    out = [0] * size
    for i, b in enumerate(head):
        out[i] = b
    for k, v in (at or {}).items():
        out[k] = v
    return out


# --- engine features --------------------------------------------------------------

def test_checksum_signs_and_verifies():
    step = rc.Step.parse({"write": "08 04", "pad_to": 16, "checksum": {"at": 16, "base": "0x55"},
                          "expect": "08 04", "verify_checksum": True, "level": {"byte": 6}})
    assert len(step.write) == 17 and sum(step.write) % 256 == 0x55
    good = [0x08, 0x04, 0, 0, 0, 0, 77] + [0] * 9
    good.append((0x55 - sum(good)) % 256)
    assert rc.evaluate(step, good) == {"level": 77}
    bad = list(good)
    bad[16] ^= 1
    assert not step.accepts(bad)


def test_anchored_replies_count_from_the_marker():
    step = rc.Step.parse({"expect": "d6 0c 00 00", "anchor": True, "level": {"byte": 4}})
    buf = [7, 1, 2, 0xD6, 0x0C, 0, 0, 64, 9]
    assert rc.evaluate(step, buf) == {"level": 64}
    assert not step.accepts([7, 1, 2, 3])


def test_sixteen_bit_xor_steps_and_clamp_fields():
    assert rc.Field.parse({"byte": 0, "bytes": 2, "min": 1, "max": 1000}).percent([0xE8, 0x03]) == 100
    assert rc.Field.parse({"byte": 0, "xor": "0xff"}).percent([0xFF - 42]) == 42
    aerox = rc.Field.parse({"byte": 0, "mask": "0x7f", "steps": 21, "steps_from": 1})
    assert aerox.percent([11]) == 50 and aerox.percent([21]) == 100 and aerox.percent([0x80 | 60]) == 60
    assert rc.Field.parse({"byte": 0, "mask": "0x0f", "max": 10, "clamp": True}).percent([11]) == 100


def test_required_state_bytes_reject_other_reports():
    step = rc.Step.parse({"write": "06 b0", "require": [{"byte": 15, "in": [1, 2, 8]}],
                          "level": {"byte": 6, "max": 8}})
    assert rc.evaluate(step, frame(at={6: 4, 15: 8})) == {"level": 50}
    assert not step.accepts(frame(at={6: 4, 15: 5}))


def test_bluetooth_paths_are_recognised():
    bt = rb"\\?\HID#{00001124-0000-1000-8000-00805f9b34fb}_VID&0002057e_PID&2009#9"
    assert rc.is_bluetooth({"path": bt})
    assert not rc.is_bluetooth({"path": rb"\\?\HID#VID_057E&PID_2009#8"})


def test_a_collection_that_does_not_answer_is_skipped_and_the_right_one_remembered():
    api = FakeApi()
    deaf = Device()
    talk = Device(reply=lambda d: frame(0xD2, 0x80 | 60) if d[:2] == b"\x00\xd2" else None)
    api.add(0x1038, 0x1838, b"a", deaf, usage_page=0xFFC0, interface=3)
    api.add(0x1038, 0x1838, b"b", talk, usage_page=0xFFC0, interface=3)
    src = rc.RecipeSource([recipe_for(0x1038, 0x1838)], api=api, clock=FakeClock(step=0.01), threaded=False)
    [r] = src.poll()
    assert (r.level, r.charging, r.name) == (60, True, "SteelSeries Aerox 3 Wireless")
    assert src.routes[(0x1038, 0x1838)] == b"b"


# --- the brands ----------------------------------------------------------------------

def test_steelseries_nova_pro_wireless():
    dev = Device(reply=lambda d: frame(0x06, 0xB0, at={6: 6, 15: 2}) if d[:2] == b"\x06\xb0" else None)
    [r] = read(0x1038, 0x12E0, dev, interface=4)
    assert (r.level, r.charging) == (75, True)


def test_steelseries_nova7_ignores_stray_reports():
    dev = Device(reply=lambda d: [0x01, 0x00, 0x63, 0x02])     # not the b0 answer
    assert read(0x1038, 0x22A1, dev, usage_page=0xFFC0) == []


def test_corsair_void_v2_wireless_handshake_and_level():
    def answer(d):
        if d[:5] == bytes([0, 2, 9, 2, 0x12]):
            return frame(0x02, 0x09)                        # headset heartbeat answered
        if d[:5] == bytes([0, 2, 9, 2, 0x0F]):
            return frame(0x02, 0x09, 0x02, 0x0F, 0x9A, 0x02)   # 666 -> 66.6 %
        return None
    dev = Device(reply=answer)
    [r] = read(0x1B1C, 0x2A08, dev, interface=4)
    assert r.level == 67 and r.name == "Corsair Void v2 Wireless"
    assert [w[4] for w in dev.written] == [0x13, 0x12, 0x12, 0x0F]


def test_astro_a50():
    dev = Device(reply=lambda d: frame(0x02, 0x0C, 0x06, 0x00, 0x06, 0x0C, 88, 88, 1))
    [r] = read(0x046D, 0x0B1C, dev, usage_page=0xFF32, usage=0x74)
    assert (r.level, r.charging) == (88, True)


def test_audeze_maxwell_marker_in_a_rolling_buffer_and_the_dongle_alone():
    dev = Device(reply=lambda d: frame(0x07, 0, 0x55, 0x10, 0xD6, 0x0C, 0, 0, 91, size=62))
    [r] = read(0x3329, 0x4B19, dev, usage_page=0xFF13, usage=1, product="Audeze Maxwell HID")
    assert r.level == 91
    api = FakeApi()
    api.add(0x3329, 0x4B19, b"p", dev, usage_page=0xFF13, usage=1, product="Audeze Maxwell Dongle")
    src = rc.RecipeSource([recipe_for(0x3329, 0x4B19)], api=api, clock=FakeClock(step=0.01), threaded=False)
    assert src.poll() == []                                 # no headset linked: nothing to show


def test_jbl_quantum_910_listens():
    dev = Device(pushes=[[0x2F, 1], [0x08, 0x5F]])
    api = FakeApi()
    api.add(0x0ECB, 0x2088, b"p", dev, usage_page=0xFF13, usage=1)
    src = rc.RecipeSource([recipe_for(0x0ECB, 0x2088)], api=api, threaded=False)
    src.poll()
    lst = next(iter(src.listeners.values()))
    for rep in list(dev.inbox):
        lst.feed(rep)
    assert src.poll()[0].level == 95


def test_keychron_ultra_link_feature_request_input_reply():
    dev = Device(reply=lambda d: frame(0xB4, 0x06, at={20: 73}) if d[:2] == b"\xb3\x06" else None)
    [r] = read(0x3434, 0xD028, dev, interface=4)
    assert r.level == 73 and dev.features_sent[0][:2] == b"\xb3\x06"


def test_lofree_transaction():
    def answer(d):
        cmd = d[3]
        return frame(0x04, 0, 0, cmd if cmd != 0x1A else 0, at={8: {0xAA: 1, 0x1A: 64}.get(cmd, 0)}, size=32)
    dev = Device(reply=answer)
    [r] = read(0x388D, 0x0025, dev, usage_page=0xFF1C, usage=0x92)
    assert r.level == 64
    assert [w[3] for w in dev.written] == [0x01, 0xAA, 0x1A, 0x02]


def test_pulsar_checksummed_frames():
    def answer(d):
        assert sum(d) % 256 == 0x55
        f = [0x08, 0x04, 0, 0, 0, 0, 58, 1] + [0] * 8
        return f + [(0x55 - sum(f)) % 256]
    [r] = read(0x3554, 0xF58A, Device(reply=answer), interface=1)
    assert (r.level, r.charging, r.name) == (58, True, "VXE R1 Pro Max")


def test_asus_echo_with_or_without_the_report_id():
    for prefix in ([], [0x00]):
        dev = Device(reply=lambda d, p=prefix: p + frame(0x12, 0x07, at={4: 81, 9: 0}))
        [r] = read(0x0B05, 0x197D, dev, interface=0)
        assert (r.level, r.name) == (81, "ROG Gladius III Wireless")
    dev = Device(reply=lambda d: frame(0x12, 0x07, at={4: 3, 9: 1}))
    [r] = read(0x0B05, 0x18E5, dev, interface=0)            # a 0..4 step model
    assert (r.level, r.charging) == (75, True)


def test_wlmouse_family_feature_exchange():
    def reply(req):
        return [0x00, 0xA1, 0x00, 0x02, 0x02, 0x00, 0x83, 0x01, 77] + [0] * 56

    for vid, pid in ((0x33E4, 0x3854), (0x373E, 0x001E), (0x36A7, 0xA880)):
        info = {"interface": 2, "usage_page": 0xFFFF} if vid == 0x373E else {}
        [r] = read(vid, pid, Device(feature_reply=reply), **info)
        assert (r.level, r.charging) == (77, True)


def test_mchose_inverted_frames_for_any_product():
    def reply(req):
        pay = bytes([0x53, 0x52, 0x31, 0x00, 0, 0, 0, 0, 0x09, 66, 0]).ljust(64, b"\0")
        return [0x12, 0x06 ^ 0xFF] + [b ^ 0xFF for b in pay]
    [r] = read(0x5253, 0x1020, Device(feature_reply=reply), usage_page=0xFF01)
    assert (r.level, r.name) == (66, "MCHOSE M7 Ultra")


def test_razer_pa_headsets_are_left_to_their_recipe():
    def answer(d):
        if d[8] == 0x21:
            return frame(0x01, 0x80, 0x09, 0x50, 0x49, 0x08, at={13: 0x21, 14: 0x01, 15: 1, 16: 34})
        if d[8] == 0x2A:
            return frame(0x01, 0x80, 0x09, 0x50, 0x49, 0x08, at={13: 0x2A, 14: 0x01, 15: 1, 16: 1})
        return frame(0x01, 0x80)
    [r] = read(0x1532, 0x053A, Device(reply=answer), usage_page=0xFF00)
    assert (r.level, r.charging) == (34, True)
    import tempfile
    from pathlib import Path

    from peribatt.config import Store
    from peribatt.sources import build_sources
    razer = build_sources(Store(Path(tempfile.mkdtemp()) / "s.json"), api=FakeApi())[2]
    assert {0x053A, 0x0555, 0x0556} <= razer.skip


def test_hyperx_cloud_iii():
    def answer(d):
        if d[1] == 0x89:
            return frame(0x66, 0x0D, 1, 0, 57, size=62)
        return frame(0x66, 0x0C, 1, size=62)
    [r] = read(0x03F0, 0x05B7, Device(reply=answer), usage_page=0xFF13, usage=1)
    assert (r.level, r.charging) == (57, True)


@pytest.mark.parametrize("pid,report,at", [(0x05C4, 0x01, 30), (0x09CC, 0x11, 32)])
def test_dualshock4_usb_and_bluetooth(pid, report, at):
    full = frame(report, at={at: 0x10 | 7}, size=78)

    class Streaming(Device):
        def read(self, n, timeout_ms=0):                     # a controller never stops sending
            return list(full)
    dev = Streaming()
    [r] = read(0x054C, pid, dev)
    assert (r.level, r.charging, r.kind) == (70, True, "gamepad")
    assert dev.written == []                             # listen only, never switches modes


def test_dualsense_edge():
    dev = Device(pushes=[frame(0x01, at={53: 0x10 | 11})])
    [r] = read(0x054C, 0x0DF2, dev)
    assert (r.level, r.charging, r.name) == (100, True, "DualSense Edge")


def test_switch_pro_controller_over_bluetooth():
    bt = rb"\\?\HID#{00001124-0000-1000-8000-00805f9b34fb}_VID&0002057e_PID&2009#9"
    dev = Device(reply=lambda d: frame(0x21, 0x10, 0x60 | 0x10) if d[10] == 0x02 else None)
    api = FakeApi()
    api.add(0x057E, 0x2009, bt, dev)
    src = rc.RecipeSource([recipe_for(0x057E, 0x2009)], api=api, clock=FakeClock(step=0.01), threaded=False)
    [r] = src.poll()
    assert (r.level, r.charging) == (75, True)              # "medium", on the charger
    usb = FakeApi()
    usb.add(0x057E, 0x2009, rb"\\?\HID#VID_057E&PID_2009#8", Device())
    assert rc.RecipeSource([recipe_for(0x057E, 0x2009)], api=usb, threaded=False).poll() == []


# --- Logitech headsets (HID++ 0x1F20) --------------------------------------------------

def test_logitech_headset_battery_voltage():
    headset = FakeLogiDevice(name="G733 Gaming Headset", dev_type=8, battery_feature=hidpp.F_ADC,
                             battery=(0x0F, 0xA0, 0x01))             # 4000 mV, connected
    api = FakeApi()
    api.add(0x046D, 0x0AB5, b"hs", FakeHidppChannel({0xFF: headset}), 0xFF43, 0x0202)
    src = LogitechSource(api=api, clock=FakeClock())
    [r] = src.poll()
    assert (r.kind, r.charging, r.name) == (HEADSET, False, "G733 Gaming Headset")
    assert 80 <= r.level <= 90
    level = r.level
    headset.battery = [0x10, 0x60, 0x03]                              # on the charger
    [r] = src.poll()
    assert r.charging and r.level is None and level                  # no level from the charger's voltage
    headset.battery = [0x0F, 0xA0, 0x00]                              # headset off, dongle in
    [r] = src.poll()
    assert not r.online and r.note == "switched off"


def test_a_logitech_receiver_found_by_its_name():
    mouse = FakeLogiDevice()
    api = FakeApi()
    api.add(0x046D, 0xC5FF, b"long", FakeHidppChannel({2: mouse}), 0xFF00, 0x0002, product="USB Receiver")
    [r] = LogitechSource(api=api, clock=FakeClock()).poll()
    assert r.name == "PRO Wireless"
