import json

import pytest

from peribatt import bluetooth, hsc, xinput
from peribatt.micmute import MicMuteWatcher
from peribatt.model import HEADSET, KEYBOARD, MOUSE
from peribatt.sources import Switchable

# --- Bluetooth ---------------------------------------------------------------


@pytest.mark.parametrize("raw,clean", [
    ("WH-1000XM4 Hands-Free AG", "WH-1000XM4"),
    ("WH-1000XM4 Hands-Free AG Audio", "WH-1000XM4"),
    ("Keychron K8 Pro", "Keychron K8 Pro"),
    ("MX Master 3S LE", "MX Master 3S"),
    ("JBL Tune 760NC Stereo", "JBL Tune 760NC"),
    ("", "Bluetooth device"),
])
def test_clean_name(raw, clean):
    assert bluetooth.clean_name(raw) == clean


@pytest.mark.parametrize("name,kind", [
    ("Keychron K8 Pro", KEYBOARD), ("K2", KEYBOARD), ("NuPhy Air75", KEYBOARD),
    ("MX Keys", KEYBOARD), ("MX Master 3S", MOUSE), ("WH-1000XM4", HEADSET),
    ("Turtle Beach Stealth 600", HEADSET), ("Galaxy Buds2", HEADSET), ("Mystery", "device"),
])
def test_bluetooth_kind(name, kind):
    assert bluetooth.guess_kind(name) == kind


def test_records_are_deduplicated_per_container():
    recs = [
        ("{AAA}", "WH-1000XM4 Hands-Free AG", 70, True),
        ("{AAA}", "WH-1000XM4 Avrcp Transport", 70, False),
        ("{BBB}", "Keychron K8 Pro", 55, True),
        ("{CCC}", "Broken", 255, True),            # nonsense level ignored
    ]
    out = {r.name: r for r in bluetooth.to_readings(recs)}
    assert set(out) == {"WH-1000XM4", "Keychron K8 Pro"}
    k8 = out["Keychron K8 Pro"]
    assert (k8.kind, k8.level, k8.online, k8.key) == (KEYBOARD, 55, True, "bt-bbb")


def test_disconnected_bluetooth_device_is_offline():
    [r] = bluetooth.to_readings([("{X}", "Keychron K8 Pro", 40, False)])
    assert not r.online and r.note == "disconnected"


def test_bluetooth_source_with_injected_records():
    src = bluetooth.BluetoothSource(records=lambda: [("{B}", "Keychron K8 Pro", 88, True)])
    assert src.poll()[0].level == 88
    idle = bluetooth.BluetoothSource(records=lambda: [])
    idle._records = None                        # what a non-Windows machine ends up with
    assert idle.poll() == []


# --- XInput ------------------------------------------------------------------

def test_xinput_levels():
    r = xinput.to_reading(0, xinput.DEVTYPE_GAMEPAD, xinput.TYPE_NIMH, 2)
    assert (r.name, r.level, r.note, r.kind) == ("Controller 1", 60, "approximate", "gamepad")
    assert xinput.to_reading(1, xinput.DEVTYPE_GAMEPAD, xinput.TYPE_WIRED, 3) is None
    assert xinput.to_reading(1, xinput.DEVTYPE_GAMEPAD, xinput.TYPE_DISCONNECTED, 0) is None
    h = xinput.to_reading(1, xinput.DEVTYPE_HEADSET, xinput.TYPE_UNKNOWN, 1)
    assert h.kind == HEADSET and h.level == 25


def test_xinput_source_polls_all_slots():
    answers = {(2, 0): (xinput.TYPE_ALKALINE, 3)}
    src = xinput.XInputSource(query=lambda u, d: answers.get((u, d)))
    [r] = src.poll()
    assert r.key == "xinput-2-0" and r.level == 100


# --- HeadsetControl ------------------------------------------------------------

HSC_JSON = json.dumps({"devices": [
    {"device": "Logitech G533", "id_vendor": "0x046d", "id_product": "0x0a66",
     "battery": {"status": "BATTERY_CHARGING", "level": 45}},
    {"device": "SteelSeries Arctis Nova 7", "id_vendor": "0x1038", "id_product": "0x2202",
     "battery": {"status": "BATTERY_AVAILABLE", "level": 75}},
    {"device": "Corsair Void", "id_vendor": 6940, "id_product": 2580,
     "battery": {"status": "BATTERY_UNAVAILABLE", "level": -1}},
]})


def test_headsetcontrol_json():
    out = {r.name: r for r in hsc.parse(HSC_JSON, skip={(0x1038, 0x2202)})}
    assert set(out) == {"Logitech G533", "Corsair Void"}
    assert out["Logitech G533"].charging and out["Logitech G533"].level == 45
    assert not out["Corsair Void"].online


def test_headsetcontrol_bad_output_and_missing_exe():
    assert hsc.parse("not json") == []
    src = hsc.HeadsetControlSource(run=lambda a: HSC_JSON)
    src.exe = ""                                # not installed
    assert not src.available and src.poll() == []


def test_headsetcontrol_runs_with_battery_json_flags():
    calls = []
    src = hsc.HeadsetControlSource(exe="headsetcontrol", run=lambda a: calls.append(a) or HSC_JSON)
    assert len(src.poll()) == 3
    assert calls == [["headsetcontrol", "-b", "-o", "json"]]


# --- misc ----------------------------------------------------------------------

def test_switchable_source():
    class S:
        name = "s"

        def poll(self):
            return ["x"]
    on = {"v": False}
    sw = Switchable(S(), lambda: on["v"])
    assert sw.poll() == []
    on["v"] = True
    assert sw.poll() == ["x"]


def test_mic_watcher_reports_changes_only():
    seen = []
    w = MicMuteWatcher(seen.append, factory=lambda: None)
    for v in (False, False, True, True, False):
        w.step(v)
    assert seen == [False, True, False]
    assert w.supported


def test_supported_check_and_reload(tmp_path, monkeypatch):
    from peribatt import sources
    from peribatt.recipes import RecipeSource
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    rs = RecipeSource([])
    check = sources.supported_check([rs, object()])
    assert check(0x046D, 0x1234) and check(0x1532, 1) and check(0x0951, 0x16EA)
    assert not check(0x1234, 0x0001)
    (tmp_path / "recipes.json").write_text(
        '[{"name": "X", "vendor_id": "0x1234", "product_ids": ["0x0001"],'
        ' "listen": [{"expect": "01", "level": {"byte": 1}}]}]')
    sources.reload_recipes([rs])
    assert (0x1234, 0x0001) in rs.claimed


# --- Bluetooth: connection state, device class, twins ---------------------------------

def test_mac_addresses_from_instance_ids():
    root = r"BTHENUM\DEV_A0B1C2D3E4F5\7&1b2a&0&BLUETOOTHDEVICE_A0B1C2D3E4F5"
    assert bluetooth.mac_of(root) == "A0B1C2D3E4F5"
    assert bluetooth.mac_of(r"BTHENUM\{0000110b-0000-1000-8000-00805f9b34fb}_LOCALMFG&0002\7&1b&0&"
                            r"A0B1C2D3E4F5_C00000000") == "A0B1C2D3E4F5"
    assert bluetooth.mac_of(r"BTHLEDEVICE\{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&02046d_PID&b023"
                            r"_REV&0003_d1e2f3a4b5c6\8&2a&0&0019") == "D1E2F3A4B5C6"
    assert bluetooth.mac_of(r"HID\{00001124-0000-1000-8000-00805f9b34fb}_VID&0002046D\9&1&0&0000") == ""


def test_device_class_gives_the_picture():
    from peribatt.model import HEADSET, KEYBOARD, MOUSE
    assert bluetooth.kind_from_class(0x240404) == HEADSET          # audio/video, wearable headset
    assert bluetooth.kind_from_class(0x002540) == KEYBOARD         # peripheral, keyboard
    assert bluetooth.kind_from_class(0x002580) == MOUSE            # peripheral, pointing device
    assert bluetooth.kind_from_class(0x002508) == "gamepad"        # peripheral, gamepad
    assert bluetooth.kind_from_class(0x5A020C) == "device"         # a phone


def test_classic_connection_state_beats_the_node_state():
    records = [("{H}", "WH-1000XM4 Hands-Free AG", 70, True, "A0B1C2D3E4F5"),
               ("{P}", "Mystery Pad", 50, False, "112233445566")]
    classic = {"A0B1C2D3E4F5": bluetooth.Classic(False, 0x240404),
               "112233445566": bluetooth.Classic(True, 0x002508)}
    out = {r.name: r for r in bluetooth.to_readings(records, classic)}
    assert not out["WH-1000XM4"].online and out["WH-1000XM4"].kind == "headset"
    assert out["Mystery Pad"].online and out["Mystery Pad"].kind == "gamepad"


def test_a_device_read_over_usb_hides_its_bluetooth_twin():
    from peribatt.model import MOUSE, Reading
    readings = [Reading("razer-00b7", "Razer DeathAdder V3 Pro", MOUSE, 60),
                Reading("bt-1", "DeathAdder V3 Pro", MOUSE, 60),
                Reading("bt-2", "Keychron K8 Pro", "keyboard", 80),
                Reading("logi-1", "G Pro", MOUSE, 50),
                Reading("bt-3", "Logitech G Pro X Wireless", "headset", 70)]
    keys = [r.key for r in bluetooth.drop_twins(readings)]
    assert keys == ["razer-00b7", "bt-2", "logi-1", "bt-3"]     # a short name does not swallow others


def test_the_bluetooth_twin_leaves_the_tray(tmp_path):
    from peribatt.app import App
    from peribatt.config import Store
    from peribatt.model import MOUSE, Reading

    from .test_app import FakeBackend
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    app.apply([Reading("bt-1", "DeathAdder V3 Pro", MOUSE, 60)])
    assert "bt-1" in app.backend.icons
    app.apply([Reading("razer-00b7", "Razer DeathAdder V3 Pro", MOUSE, 60),
               Reading("bt-1", "DeathAdder V3 Pro", MOUSE, 60)])
    assert "bt-1" not in app.backend.icons and "bt-1" not in app.readings


def test_struct_sizes_match_windows():
    import ctypes
    assert ctypes.sizeof(bluetooth.BLUETOOTH_DEVICE_INFO) == 560
    assert ctypes.sizeof(bluetooth.BLUETOOTH_DEVICE_SEARCH_PARAMS) == 40


def test_a_plug_event_schedules_quick_rechecks(tmp_path):
    from peribatt.app import RECHECKS, App
    from peribatt.config import Store

    from .test_app import FakeBackend
    calls = []

    class Cached:
        name = "bluetooth"

        def poll(self):
            return []

        def invalidate(self):
            calls.append(1)
    app = App(Store(tmp_path / "settings.json"), FakeBackend(), sources=[Cached()])
    app.devices_changed()
    assert len(app._rechecks) == len(RECHECKS) and calls == [1]
