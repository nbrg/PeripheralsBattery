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
