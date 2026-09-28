"""Settings and remembered devices, kept in one JSON file in
``%APPDATA%\\PeripheralsBattery`` (``~/.config/peripheralsbattery`` elsewhere,
or wherever ``PERIBATT_HOME`` points)."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

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
    "first_run_done": False,     # autostart is switched on once, at the very first launch
    "welcomed": False,           # the first-launch hint about the ^ tray overflow was shown
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
        self._save_lock = threading.Lock()
        self._saved_text: Optional[str] = None

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
        if not isinstance(raw, dict):
            log.warning("settings file has an unexpected shape, using defaults")
            return store
        settings = raw.get("settings")
        for k, v in (settings.items() if isinstance(settings, dict) else ()):
            default = DEFAULTS.get(k)
            # bool is an int in Python: don't let `true` become a poll interval
            if k in DEFAULTS and isinstance(v, type(default)) and \
                    isinstance(v, bool) == isinstance(default, bool):
                store.settings[k] = v
        for attr in ("devices", "logitech_slots"):
            value = raw.get(attr)
            if isinstance(value, dict):
                setattr(store, attr, {k: v for k, v in value.items() if isinstance(v, dict)})
        return store

    def _snapshot(self) -> str:
        """The file's text. Other threads may be updating the dicts while this
        runs ("dictionary changed size during iteration"): just try again."""
        for _ in range(5):
            try:
                doc = {"settings": dict(self.settings), "devices": dict(self.devices),
                       "logitech_slots": dict(self.logitech_slots)}
                return json.dumps(doc, indent=2, sort_keys=True)
            except RuntimeError:
                continue
        raise RuntimeError("settings kept changing while saving")

    def save(self) -> None:
        """Writes the file if anything changed. Safe to call from any thread."""
        with self._save_lock:
            try:
                text = self._snapshot()
            except RuntimeError as e:
                log.warning("could not save settings: %s", e)
                return
            if text == self._saved_text:
                return                               # nothing new: no disk write
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Write-then-rename so a crash never leaves a half-written file.
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-")
            except OSError as e:
                log.warning("could not save settings: %s", e)
                return
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, self.path)
                self._saved_text = text
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
