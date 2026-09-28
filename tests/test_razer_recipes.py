import json

import pytest

from peribatt import recipes as rc
from peribatt.razer import (
    CLASS_POWER,
    CMD_BATTERY,
    RazerSource,
    build_report,
    guess_kind,
    parse_report,
    raw_to_percent,
)

from .fakes import FakeApi, FakeClock, FakeRazer, QueueHandle

# --- Razer -------------------------------------------------------------------

def test_razer_report_layout_and_crc():
    rep = build_report(0x1F, CLASS_POWER, CMD_BATTERY)
    assert len(rep) == 90
    assert rep[1] == 0x1F and rep[5] == 0x02 and rep[6] == 0x07 and rep[7] == 0x80
    assert rep[88] == 0x02 ^ 0x07 ^ 0x80


def test_razer_parse_strips_report_id_and_checks_command():
    body = bytearray(build_report(0x1F, CLASS_POWER, CMD_BATTERY))
    body[0], body[9] = 0x02, 200
    assert parse_report(b"\x00" + bytes(body), CLASS_POWER, CMD_BATTERY) == (0x02, 200)
    assert parse_report(bytes(body), CLASS_POWER, 0x84) == (0x02, None)
    assert parse_report(b"\x00" * 10, CLASS_POWER, CMD_BATTERY) == (None, None)


def test_raw_to_percent():
    assert raw_to_percent(255) == 100 and raw_to_percent(0) == 0 and raw_to_percent(153) == 60


def razer(*devices):
    api = FakeApi()
    for pid, dev, product in devices:
        api.add(0x1532, pid, f"p{pid}".encode(), dev, product=product)
    return api


def test_razer_finds_transaction_id_and_remembers_it():
    dev = FakeRazer(tid=0x3F, raw=153, charging=1)
    src = RazerSource(api=razer((0x00B7, dev, "Razer DeathAdder V3 Pro")), sleep=lambda s: None)
    [r] = src.poll()
    assert (r.level, r.charging, r.kind, r.online) == (60, True, "mouse", True)
    assert src.working[0x00B7] == (0, 0x3F)
    dev.sent.clear()
    src.poll()
    assert {req[1] for req in dev.sent} == {0x3F}          # no more probing


def test_razer_unsupported_device_is_skipped_later():
    kb = FakeRazer(supported=False)
    src = RazerSource(api=razer((0x0241, kb, "Razer BlackWidow")), sleep=lambda s: None)
    assert src.poll() == []
    kb.sent.clear()
    assert src.poll() == [] and kb.sent == []


def test_razer_asleep_keeps_last_level_greyed():
    dev = FakeRazer(raw=102)
    src = RazerSource(api=razer((0x00AA, dev, "Razer Basilisk V3 Pro")), sleep=lambda s: None)
    src.poll()
    dev.asleep = True
    [r] = src.poll()
    assert not r.online and r.level == 40


@pytest.mark.parametrize("name,kind", [("Razer BlackShark V2 Pro", "headset"),
                                       ("Razer Huntsman Mini", "keyboard"),
                                       ("Razer Viper Ultimate", "mouse"),
                                       ("Razer Mouse Dock", "mouse"),
                                       ("Razer Thing", "device")])
def test_razer_kind(name, kind):
    assert guess_kind(name) == kind


# --- recipes -----------------------------------------------------------------

def test_bundled_recipes_all_parse():
    loaded = rc.load()
    assert len(loaded) >= 9
    names = {r.name for r in loaded}
    assert {"SteelSeries Arctis Nova 7", "Corsair Void", "HyperX Cloud II Wireless"} <= names
    for r in loaded:
        assert r.steps or r.listen
        assert r.product_ids or r.any_product


def test_field_scaling_and_flags():
    f = rc.Field.parse({"byte": 2, "max": 4})
    assert f.percent([0, 0, 3]) == 75
    assert f.percent([0, 0, 9]) is None                   # out of range is refused
    assert f.percent([0]) is None
    m = rc.Field.parse({"byte": 1, "mask": "0x7f"})
    assert m.percent([0, 0x80 | 55]) == 55
    b = rc.Field.parse({"byte": 0, "in": [4, 5]})
    assert b.flag([5]) is True and b.flag([1]) is False
    lo = rc.Field.parse({"byte": 0, "min": 100, "max": 154})
    assert lo.percent([127]) == 50


class NovaHandle(QueueHandle):
    def __init__(self, level, status):
        super().__init__()
        self.level, self.status = level, status

    def on_write(self, data):
        self.inbox.append(bytes([0x0F]))                          # unrelated noise
        self.inbox.append(bytes([0xB0, 0x03, self.level, self.status] + [0] * 60))


def nova_api(level, status, pid=0x2202):
    api = FakeApi()
    api.add(0x1038, pid, b"other", QueueHandle(), usage_page=0xFF00)
    api.add(0x1038, pid, b"nova", NovaHandle(level, status), usage_page=0xFFC0)
    return api


def test_recipe_reads_nova7_discrete_levels():
    src = rc.RecipeSource(rc.load(), api=nova_api(3, 0x03), clock=FakeClock())
    [r] = src.poll()
    assert (r.name, r.level, r.charging, r.online) == ("SteelSeries Arctis Nova 7", 75, False, True)


def test_recipe_charging_and_offline():
    src = rc.RecipeSource(rc.load(), api=nova_api(2, 0x01), clock=FakeClock())
    assert src.poll()[0].charging
    src.api = nova_api(2, 0x00)
    [r] = src.poll()
    assert not r.online and r.level == 50          # last level kept


def test_recipe_expect_filters_replies():
    step = rc.Step.parse({"write": "06 ff bb 02", "pad_to": 52, "expect": "06 ff bb 02",
                          "level": {"byte": 7}})
    assert len(step.write) == 52
    assert step.accepts([6, 0xFF, 0xBB, 2, 0, 0, 0, 80])
    assert not step.accepts([6, 0xFF, 0xBB, 3, 0, 0, 0, 80])
    assert rc.evaluate(step, [6, 0xFF, 0xBB, 2, 0, 0, 0, 80]) == {"level": 80}


def test_user_recipe_file_adds_and_overrides(tmp_path):
    user = tmp_path / "recipes.json"
    user.write_text(json.dumps([
        {"name": "My Nova", "vendor_id": "0x1038", "product_ids": ["0x2202"],
         "steps": [{"write": "00 b0", "level": {"byte": 2}}]},
        {"name": "broken"},
    ]))
    loaded = rc.load([user])
    by_pid = {(r.vendor_id, p): r.name for r in loaded for p in r.product_ids}
    assert by_pid[(0x1038, 0x2202)] == "My Nova"
    assert by_pid[(0x1038, 0x2206)] == "SteelSeries Arctis Nova 7"


def test_recipe_match_filters():
    r = rc.Recipe.parse({"name": "x", "vendor_id": 1, "product_ids": [2],
                         "match": {"usage_page": "0xffc0", "interface": 3},
                         "steps": [{"write": "00"}]})
    assert r.picks({"usage_page": 0xFFC0, "interface_number": 3})
    assert not r.picks({"usage_page": 0xFFC0, "interface_number": 0})
