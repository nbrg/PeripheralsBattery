import json

from peribatt import __main__ as cli
from peribatt.config import DEFAULTS, Store, app_dir
from peribatt.history import History, format_duration, slope_per_hour
from peribatt.model import MOUSE, Reading

# --- config ------------------------------------------------------------------


def test_store_roundtrip(tmp_path):
    s = Store.load(tmp_path)
    assert s.settings == DEFAULTS
    s["poll_seconds"] = 120
    s.devices["k"] = {"name": "Mouse", "kind": MOUSE, "level": 5}
    s.save()
    again = Store.load(tmp_path)
    assert again["poll_seconds"] == 120 and again.devices["k"]["level"] == 5


def test_store_ignores_unknown_and_wrong_types(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(
        {"settings": {"poll_seconds": "fast", "low": 25, "bogus": 1}}))
    s = Store.load(tmp_path)
    assert s["poll_seconds"] == DEFAULTS["poll_seconds"] and s["low"] == 25 and "bogus" not in s.settings


def test_store_survives_corrupt_file(tmp_path):
    (tmp_path / "settings.json").write_text("{nope")
    assert Store.load(tmp_path).settings == DEFAULTS


def test_app_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    assert app_dir() == tmp_path


# --- history -----------------------------------------------------------------

def test_slope_needs_enough_data():
    assert slope_per_hour([(0, 80)]) is None
    assert slope_per_hour([(0, 80), (600, 70)]) is None             # only 10 minutes
    assert slope_per_hour([(0, 80), (3600, 79.5)]) is None           # too small a change
    assert round(slope_per_hour([(0, 80), (1800, 79), (3600, 78)]), 3) == -2.0


def test_format_duration():
    assert format_duration(0.004) == "1m"
    assert format_duration(0.5) == "30m"
    assert format_duration(2.25) == "2h 15m"
    assert format_duration(30) == "30h"
    assert format_duration(100) == "4d"


def test_estimate_discharge_and_charge(tmp_path):
    h = History(tmp_path / "history.csv")
    r = Reading("m", "Mouse", MOUSE, 80)
    for i, level in enumerate((80, 79, 78, 77)):
        h.record(r.with_(level=level), now=i * 1800.0)
    assert h.estimate(r.with_(level=77)) == "~38h left"
    # plugging in starts a new session: no stale discharge estimate
    h.record(r.with_(level=77, charging=True), now=6000.0)
    assert h.estimate(r.with_(level=77, charging=True)) is None
    for i, level in enumerate((80, 85, 90)):
        h.record(r.with_(level=level, charging=True), now=6000.0 + (i + 1) * 900)
    assert h.estimate(r.with_(level=90, charging=True)).startswith("full in ~")
    rows = (tmp_path / "history.csv").read_text().splitlines()
    assert rows[0] == "time,device,level,charging" and len(rows) == 8


def test_history_ignores_offline():
    h = History()
    h.record(Reading("m", "Mouse", MOUSE, 50, online=False))
    assert h.sessions == {}


# --- CLI ---------------------------------------------------------------------

def test_cli_once_json(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "collect", lambda store: ([Reading("k", "Keychron K8 Pro", "keyboard", 55)], []))
    assert cli.main(["--once", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["name"] == "Keychron K8 Pro" and data[0]["level"] == 55


def test_cli_once_text(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "collect", lambda store: ([], []))
    cli.main(["--once"])
    assert "No devices" in capsys.readouterr().out


def test_cli_real_collect_runs_without_devices(monkeypatch, tmp_path):
    """The whole provider stack, against whatever this machine has (nothing on CI)."""
    monkeypatch.setenv("PERIBATT_HOME", str(tmp_path))
    readings, sources = cli.collect(Store.load(tmp_path), settle=0)
    assert isinstance(readings, list) and len(sources) == 7
