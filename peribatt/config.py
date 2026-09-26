"""Settings and remembered devices, kept in one JSON file in
``%APPDATA%\\PeripheralsBattery`` (``~/.config/peripheralsbattery`` elsewhere,
or wherever ``PERIBATT_HOME`` points)."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from . import APP_NAME

log = logging.getLogger("peribatt")

DEFAULTS: Dict[str, Any] = {
    "poll_seconds": 60,          # full battery poll
    "low": 20,                   # red frame below this
    "warn": 33,                  # yellow frame up to and including this
    "alert_at": 15,              # low battery notification (0 = off)
    "notify_full": True,         # "fully charged, you can unplug" notification
    "flash_on_mute": True,       # headset frame blinks while the mic is muted
    "windows_mute": True,        # a mic muted in Windows counts as muted too
    "show_number": False,        # percentage instead of the device pictogram
    "bluetooth": True,           # Windows-reported Bluetooth batteries
    "xinput": True,              # Xbox-compatible controllers
    "headsetcontrol": "",        # path to headsetcontrol.exe ("" = look on PATH)
    "hidden": [],                # device keys the user chose to hide
    "names": {},                 # device key -> name chosen by the user
    "history": True,             # write history.csv
    "first_run_done": False,     # autostart is switched on once, at the very first launch
}


def app_dir() -> Path:
    env = os.environ.get("PERIBATT_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path.home() / ".config" / APP_NAME.lower()


class Store:
    """``settings`` merges the defaults with the saved values; ``devices`` are
    the devices seen before, so their icons can appear (greyed) at start-up."""

    def __init__(self, path: Path):
        self.path = path
        self.settings: Dict[str, Any] = dict(DEFAULTS)
        self.devices: Dict[str, Dict[str, Any]] = {}
        self.logitech_slots: Dict[str, dict] = {}

    @classmethod
    def load(cls, directory: Path = None) -> "Store":
        directory = directory or app_dir()
        store = cls(directory / "settings.json")
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return store
        except (OSError, ValueError) as e:
            log.warning("settings unreadable, using defaults: %s", e)
            return store
        for k, v in (raw.get("settings") or {}).items():
            if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
                store.settings[k] = v
        store.devices = dict(raw.get("devices") or {})
        store.logitech_slots = dict(raw.get("logitech_slots") or {})
        return store

    def save(self) -> None:
        doc = {"settings": self.settings, "devices": self.devices,
               "logitech_slots": self.logitech_slots}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash never leaves a half-written file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save settings: %s", e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def __getitem__(self, key: str) -> Any:
        return self.settings[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.settings[key] = value
