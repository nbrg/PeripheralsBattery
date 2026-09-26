import json
import random

from peribatt import learn, recipes
from peribatt.learn import Capture, Findings, Report, build_recipe, level_candidates, toggle_candidates

from .fakes import FakeApi, QueueHandle


def rep(data, phase="idle", collection=0, query=None, t=0.0):
    return Report(t, collection, tuple(data), phase, query)


def noise(rng, n=20, length=16):
    """Unrelated traffic: a counter report and random-ish junk."""
    out = []
    for i in range(n):
        out.append(rep([0x02, 0x01, i & 0xFF] + [rng.randrange(256) for _ in range(length - 3)]))
    return out


def test_finds_pushed_battery_percentage():
    rng = random.Random(1)
    battery = [rep([0x0B, 0x00, 0xBB, 0x02, 0, 0, 0, 57] + [0] * 8) for _ in range(3)]
    [best, *_] = level_candidates(noise(rng) + battery, 57)
    assert best.byte == 7 and best.prefix == (0x0B, 0x00, 0xBB, 0x02) and best.spec == {"byte": 7}


def test_finds_0_255_scale():
    reports = [rep([0x05, 0x10, 0x00, 0x91] + [0] * 4) for _ in range(2)]   # 0x91 = 145 -> 57%
    best = level_candidates(reports, 57)[0]
    assert best.spec == {"byte": 3, "max": 255}


def test_query_reply_wins():
    reports = [rep([0x07, 57, 0, 0]), rep([0x06, 0xFF, 0xBB, 0x02, 0, 0, 0, 57], query=1)]
    best = level_candidates(reports, 57)[0]
    assert best.query == 1 and best.byte == 7


def test_position_with_other_values_is_rejected():
    reports = [rep([0x03, 57, 1]), rep([0x03, 12, 1]), rep([0x03, 99, 1])]
    assert all(c.byte != 1 for c in level_candidates(reports, 57))


def test_mute_toggle_found_and_counters_ignored():
    unmuted = [rep([0x0B, 0, 0xBB, 0x08, 0, 5, i], "unmuted") for i in range(3)]
    muted = [rep([0x0B, 0, 0xBB, 0x08, 1, 5, 10 + i], "muted") for i in range(3)]
    [best, *rest] = toggle_candidates(unmuted, muted)
    assert best.byte == 4 and best.spec == {"byte": 4, "in": [1]}
    assert best.prefix == (0x0B, 0, 0xBB, 0x08)
    assert all(c.byte != 6 for c in rest)          # the counter changes within a phase


def test_toggle_needs_both_phases():
    assert toggle_candidates([rep([1, 0])], []) == []


def test_build_recipe_listen_and_query_variants_are_valid_recipes():
    lvl = level_candidates([rep([0x0B, 0, 0xBB, 0x02, 0, 0, 0, 57])], 57)[0]
    mute = toggle_candidates([rep([0x0B, 0, 0xBB, 0x08, 0], "u")], [rep([0x0B, 0, 0xBB, 0x08, 1], "m")])[0]
    spec = build_recipe("Mystery Headset", "headset", 0x1234, 0xABCD, Findings(level=lvl, muted=mute))
    assert "steps" not in spec and len(spec["listen"]) == 2
    r = recipes.Recipe.parse(spec)
    assert r.product_ids == [0xABCD] and len(r.rules) == 2

    q = level_candidates([rep([0x06, 0xFF, 0xBB, 0x02, 0, 0, 0, 57], query=1)], 57)[0]
    spec = build_recipe("Q", "headset", 1, 2, Findings(level=q))
    assert spec["steps"][0]["write"] == "06 ff bb 02" and spec["steps"][0]["pad_to"] == 52
    assert recipes.Recipe.parse(spec).steps[0].expect == bytes([0x06, 0xFF, 0xBB, 0x02])


def test_learned_recipe_drives_a_listener_end_to_end(tmp_path):
    """Wizard output -> recipes file -> RecipeSource reads pushed reports."""
    lvl = level_candidates([rep([0x0B, 0, 0xBB, 0x02, 0, 0, 0, 57])], 57)[0]
    mute = toggle_candidates([rep([0x0B, 0, 0xBB, 0x08, 0], "u")], [rep([0x0B, 0, 0xBB, 0x08, 1], "m")])[0]
    path = tmp_path / "recipes.json"
    learn.save_recipe(build_recipe("Mystery", "headset", 0x1234, 0xABCD,
                                   Findings(level=lvl, muted=mute)), path)
    loaded = [r for r in recipes.load([path]) if r.vendor_id == 0x1234]
    api = FakeApi()
    h = QueueHandle()
    api.add(0x1234, 0xABCD, b"v", h, usage_page=0xFF00)
    seen = []
    src = recipes.RecipeSource(loaded, api=api, on_change=seen.append, threaded=False)
    assert src.poll() == []                       # opened, nothing heard yet
    lst = src.listeners["hid-1234-abcd"]
    lst.feed([0x0B, 0, 0xBB, 0x02, 0, 0, 0, 61])
    lst.feed([0x0B, 0, 0xBB, 0x08, 1])
    assert seen[-1].level == 61 and seen[-1].muted
    [r] = src.poll()
    assert r.level == 61 and r.muted and r.online
    api.infos.clear()                             # unplugged
    [r] = src.poll()
    assert not r.online and h.closed


def test_save_recipe_replaces_same_device(tmp_path):
    path = tmp_path / "recipes.json"
    a = {"name": "A", "vendor_id": "0x0001", "product_ids": ["0x0002"],
         "listen": [{"expect": "01", "level": {"byte": 1}}]}
    b = dict(a, name="B")
    other = dict(a, name="Other", product_ids=["0x0003"])
    learn.save_recipe(a, path)
    learn.save_recipe(other, path)
    learn.save_recipe(b, path)
    names = [e["name"] for e in json.loads(path.read_text())]
    assert names == ["Other", "B"]


def test_capture_records_phases_and_sends_known_queries():
    api = FakeApi()
    vendor, consumer = QueueHandle(), QueueHandle()
    api.add(0x1234, 0x1, b"v", vendor, usage_page=0xFF00)
    api.add(0x1234, 0x1, b"c", consumer, usage_page=0x000C)
    api.add(0x1234, 0x1, b"k", QueueHandle(), usage_page=0x0001, usage=0x06)
    cap = Capture(api, api.enumerate(0x1234), threaded=False)
    assert cap.open() == 2                        # keyboard collection skipped
    cap.set_phase("muted")
    cap.feed(1, [1, 2, 3])
    cap.send_known_queries(pause=lambda s: None)
    assert len(vendor.written) == len(learn.KNOWN_QUERIES) and not consumer.written
    assert len(vendor.written[0]) == 62
    assert cap.in_phase("muted")[0].data == (1, 2, 3)
    cap.close()
    assert vendor.closed


def test_device_list_groups_collections_and_flags_supported():
    api = FakeApi()
    api.add(0x046D, 0xC539, b"a", None, usage_page=0xFF00, product="USB Receiver")
    api.add(0x046D, 0xC539, b"b", None, usage_page=0x000C, product="USB Receiver")
    api.add(0x1234, 0x0001, b"c", None, usage_page=0xFF00, product="Mystery Dongle")
    api.add(0x1111, 0x0002, b"d", None, usage_page=0x0001, usage=0x06, product="Plain keyboard")
    devs = learn.device_list(api, skip=lambda v, p: v == 0x046D)
    assert [d["name"] for d in devs] == ["Mystery Dongle", "USB Receiver"]
    assert devs[1]["supported"] and len(devs[1]["infos"]) == 2


def test_describe():
    assert learn.describe(None) == "not found"
    c = level_candidates([rep([0x05, 0x10, 0x00, 0x91])], 57)[0]
    assert "0-255" in learn.describe(c)
