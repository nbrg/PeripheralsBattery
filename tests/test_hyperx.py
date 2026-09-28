from peribatt import hyperx
from peribatt.hyperx import (
    CMD_BATTERY,
    CMD_CHARGE,
    CMD_STATUS,
    MISSED_POLLS_OFFLINE,
    HyperXSource,
    decode,
    packet,
)
from peribatt.model import HEADSET

from .fakes import FakeApi, QueueHandle


def reply(cmd, b4=0, b7=0):
    r = [0x0B, 0x00, 0xBB, cmd, b4, 0, 0, b7]
    return r + [0] * (64 - len(r))


def test_packet_layout():
    # 16 bytes, as CubE135's Cloud Flight S monitor sends: hidapi pads a short write to
    # the report length, but a longer one (the Cloud II's 62) is refused by the Flight S
    p = packet(CMD_BATTERY)
    assert p == bytes.fromhex("06 00 02 00 9a 00 00 68 4a 8e 0a 00 00 00 bb 02")
    assert packet(0x18, 30)[16] == 30 and len(packet(0x18, 30)) == 17


def test_decode_known_replies():
    assert decode(reply(CMD_BATTERY, b7=73)) == {"online": True, "level": 73}
    assert decode(reply(CMD_CHARGE, b4=1)) == {"charging": True}
    assert decode(reply(CMD_CHARGE, b4=2)) == {"charging": True}      # full, still plugged
    assert decode(reply(CMD_CHARGE, b4=0)) == {"charging": False}
    assert decode(reply(0x08, b4=1)) == {"muted": True}
    assert decode(reply(0x08, b4=0)) == {"muted": False}
    assert decode(reply(CMD_STATUS, b4=1)) == {"online": True}
    assert decode(reply(CMD_STATUS, b4=4)) == {"online": True}
    # other status values are not "off" on every dongle (the Flight S flickered)
    assert decode(reply(CMD_STATUS, b4=2)) == {}
    assert decode(reply(CMD_STATUS, b4=0)) == {}


def test_decode_rejects_junk():
    assert decode([]) == {}
    assert decode(reply(CMD_BATTERY, b7=200)) == {}
    assert decode([0x06] + reply(CMD_BATTERY, b7=50)[1:]) == {}       # wrong report id
    assert decode(reply(0x11, b4=1)) == {}                            # firmware version etc.


def dongle(pid=0x16EA):
    api = FakeApi()
    h = QueueHandle()
    api.add(0x0951, pid, b"vendor", h, usage_page=0xFF13, usage=1)
    api.add(0x0951, pid, b"consumer", QueueHandle(), usage_page=0x0C, usage=1)
    return api, h


def test_poll_opens_vendor_collection_and_asks_for_everything():
    api, h = dongle()
    src = HyperXSource(api=api, threaded=False)
    [r] = src.poll()
    assert api.opened == [b"vendor", b"consumer"]          # the vendor page first
    assert [w[15] for w in h.written] == [CMD_STATUS, CMD_BATTERY, CMD_CHARGE]
    assert r.name == "HyperX Cloud Flight S" and r.kind == HEADSET and not r.online


def test_replies_update_state_and_notify():
    api, h = dongle()
    seen = []
    src = HyperXSource(api=api, on_change=seen.append, threaded=False)
    src.poll()
    for rep in (reply(CMD_STATUS, 1), reply(CMD_BATTERY, b7=64), reply(CMD_CHARGE, 1)):
        src.feed(rep)
    r = src.readings()[0]
    assert (r.online, r.level, r.charging, r.muted) == (True, 64, True, False)
    src.feed(reply(0x08, b4=1))
    assert seen[-1].muted
    n = len(seen)
    src.feed(reply(0x08, b4=1))          # no change -> no callback
    assert len(seen) == n


def test_power_on_triggers_battery_query():
    api, h = dongle()
    src = HyperXSource(api=api, threaded=False)
    src.poll()
    h.written.clear()
    src.feed(reply(CMD_STATUS, 1))
    assert [w[15] for w in h.written] == [CMD_BATTERY, CMD_CHARGE]


def test_goes_offline_after_missed_polls_and_back():
    api, h = dongle()
    src = HyperXSource(api=api, threaded=False)
    src.poll()
    src.feed(reply(CMD_BATTERY, b7=50))
    for _ in range(MISSED_POLLS_OFFLINE + 1):
        r = src.poll()[0]
    assert not r.online and r.note == "switched off" and r.level == 50
    src.feed(reply(CMD_BATTERY, b7=49))
    assert src.readings()[0].online


def test_muted_is_not_reported_while_off():
    api, _ = dongle()
    src = HyperXSource(api=api, threaded=False)
    src.poll()
    src.feed(reply(0x08, b4=1))
    for _ in range(MISSED_POLLS_OFFLINE + 1):           # no battery answers: switched off
        src.poll()
    assert not src.readings()[0].muted


def test_status_reports_do_not_flip_a_working_headset_off():
    """The Cloud Flight S answers the 3-second status query with values other than
    1/4 while it is on; that used to mark it off until the next battery reply."""
    api, _ = dongle()
    src = HyperXSource(api=api, threaded=False)
    src.poll()
    src.feed(reply(CMD_BATTERY, b7=64))
    for value in (0, 2, 3):
        src.feed(reply(CMD_STATUS, value))
        assert src.readings()[0].online


def test_unplugged_dongle():
    api, h = dongle()
    seen = []
    src = HyperXSource(api=api, on_change=seen.append, threaded=False)
    src.poll()
    src._lost()
    assert h.closed and seen[-1].note == "dongle unplugged"
    assert src.readings()[0].online is False


def test_requests_are_sent_one_at_a_time():
    """A Cloud Flight S answered only the last of three requests sent back to back
    (diagnostics from a real one: only 'bb 03' replies). Each request now waits for
    the previous one's reply."""
    import time

    class OneAtATime(QueueHandle):
        busy = False

        def on_write(self, data):
            if self.inbox:                            # a reply not read yet: the dongle drops it
                OneAtATime.busy = True
                return
            cmd = data[15]
            self.inbox.append(reply(cmd, b4=1, b7=64 if cmd == CMD_BATTERY else 0))

    api = FakeApi()
    h = OneAtATime()
    api.add(0x0951, 0x16EA, b"v", h, usage_page=0xFF13, usage=1)
    src = HyperXSource(api=api)
    try:
        src.poll()
        end = time.time() + 3
        while len(h.written) < 3 and time.time() < end:
            time.sleep(0.01)
        time.sleep(0.05)
        assert [w[15] for w in h.written] == [CMD_STATUS, CMD_BATTERY, CMD_CHARGE]
        assert not OneAtATime.busy                    # never written over a pending reply
        r = src.readings()[0]
        assert r.online and r.level == 64
    finally:
        src.close()


def test_nothing_reported_before_the_dongle_was_ever_seen():
    assert HyperXSource(api=FakeApi(), threaded=False).poll() == []


def test_cloud_ii_wireless_uses_its_own_name():
    api, _ = dongle(pid=0x1718)
    [r] = HyperXSource(api=api, threaded=False).poll()
    assert r.name == "HyperX Cloud II Wireless" and r.key == "hyperx-1718"


def test_reader_thread_feeds_reports():
    import threading

    api, h = dongle()
    got = threading.Event()
    src = HyperXSource(api=api, on_change=lambda r: got.set() if r.level == 81 else None)
    h.inbox.append(reply(CMD_BATTERY, b7=81))
    src.poll()
    assert got.wait(2)
    src.close()
    assert hyperx.VENDOR_PAGE == 0xFF13


def test_windows_dongle_answering_on_another_collection():
    """On Windows the Cloud Flight S may take requests on a collection that is
    not the 0xFF13 page: every collection is tried, then only the one that answers."""
    api = FakeApi()
    other = QueueHandle()
    api.add(0x0951, 0x16EA, b"kbd", OSError("access denied"), usage_page=0x01, usage=6)
    api.add(0x0951, 0x16EA, b"ff00", other, usage_page=0xFF00, usage=1)
    api.add(0x0951, 0x16EA, b"cons", QueueHandle(), usage_page=0x0C, usage=1)
    src = HyperXSource(api=api, threaded=False)
    [r] = src.poll()
    assert other.written and r.note == "switched off"      # found, not yet answered
    src.feed(reply(CMD_BATTERY, b7=77), other)
    assert src.readings()[0].level == 77
    assert src._writers == [other]
    other.written.clear()
    src.poll()
    assert [w[15] for w in other.written] == [CMD_STATUS, CMD_BATTERY, CMD_CHARGE]


def test_feature_report_fallback():
    class FeatureOnly(QueueHandle):
        def __init__(self):
            super().__init__()
            self.features = []

        def write(self, data):
            raise OSError("WriteFile: (0x00000001) Incorrect function.")

        def send_feature_report(self, data):
            self.features.append(bytes(data))
            return len(data)

    api = FakeApi()
    h = FeatureOnly()
    api.add(0x0951, 0x16EA, b"v", h, usage_page=0xFF13, usage=1)
    src = HyperXSource(api=api, threaded=False)
    [r] = src.poll()
    assert [f[15] for f in h.features] == [CMD_STATUS, CMD_BATTERY, CMD_CHARGE]
    assert r.note == "switched off"                        # still there, not "unplugged"


def test_collections_that_reject_requests_are_dropped():
    class Deaf(QueueHandle):
        def write(self, data):
            raise OSError("wrong report id")

    api = FakeApi()
    good, deaf = QueueHandle(), Deaf()
    api.add(0x0951, 0x16EA, b"v", good, usage_page=0xFF13, usage=1)
    api.add(0x0951, 0x16EA, b"c", deaf, usage_page=0x0C, usage=1)
    src = HyperXSource(api=api, threaded=False)
    src.poll()
    assert src._writers == [good] and src.state.present
