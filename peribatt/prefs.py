"""The settings window's logic, separate from its widgets so it can be tested:
which options exist, how form text is validated, and how a saved form is
applied to the running app."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import winshell


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    options: Tuple[Tuple[Any, str], ...]


@dataclass(frozen=True)
class Number:
    key: str
    label: str
    lo: int
    hi: int


@dataclass(frozen=True)
class Toggle:
    key: str
    label: str


@dataclass(frozen=True)
class Path_:
    key: str
    label: str
    hint: str


SECTIONS: Tuple[Tuple[str, Sequence[object]], ...] = (
    ("General", (
        Choice("poll_seconds", "Check batteries every",
               ((30, "30 seconds"), (60, "1 minute"), (120, "2 minutes"), (300, "5 minutes"))),
        Choice("alert_at", "Low battery notification at",
               ((0, "Off"), (10, "10%"), (15, "15%"), (20, "20%"), (25, "25%"))),
        Toggle("notify_full", "Notify when a device is fully charged"),
        Toggle("autostart", "Start with Windows"),
        Toggle("update_check", "Check for updates once a day"),
    )),
    ("Icons", (
        Number("low", "Red frame below (%)", 1, 50),
        Number("warn", "Yellow frame up to (%)", 2, 90),
        Toggle("show_number", "Show the percentage instead of the device picture"),
        Toggle("flash_on_mute", "Blink the headset icon while its mic is muted"),
        Toggle("windows_mute", "Count a mic muted in Windows as muted"),
        Choice("icon_colour", "Icon colour",
               (("auto", "Follow the taskbar"), ("white", "White"), ("black", "Black"))),
        Choice("hide_off_after", "Remove switched-off devices from the tray",
               ((0, "Never"), (5, "After 5 minutes"), (30, "After 30 minutes"), (120, "After 2 hours"))),
    )),
    ("Sources", (
        Toggle("bluetooth", "Bluetooth devices Windows knows the battery of"),
        Toggle("xinput", "Xbox-compatible controllers"),
        Path_("headsetcontrol", "HeadsetControl program",
              "Optional: headsetcontrol.exe adds 100+ more headsets. Empty = look on PATH."),
    )),
)


def options() -> List[object]:
    return [o for _, opts in SECTIONS for o in opts]


def current(store, autostart_enabled=winshell.autostart_enabled) -> Dict[str, Any]:
    values = {o.key: store[o.key] for o in options() if o.key != "autostart"}
    values["autostart"] = autostart_enabled()
    return values


def validate(form: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Form values (strings from entry boxes are fine) -> clean values + errors."""
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    for o in options():
        if o.key not in form:
            continue
        v = form[o.key]
        if isinstance(o, Number):
            try:
                n = int(str(v).strip().rstrip("%"))
            except ValueError:
                errors.append(f"{o.label}: enter a whole number")
                continue
            if not o.lo <= n <= o.hi:
                errors.append(f"{o.label}: must be between {o.lo} and {o.hi}")
                continue
            clean[o.key] = n
        elif isinstance(o, Choice):
            allowed = [value for value, _ in o.options]
            if v not in allowed:
                try:
                    v = type(allowed[0])(v)
                except (TypeError, ValueError):
                    pass
            if v not in allowed:
                errors.append(f"{o.label}: unknown choice")
                continue
            clean[o.key] = v
        elif isinstance(o, Toggle):
            clean[o.key] = bool(v)
        else:
            clean[o.key] = str(v).strip().strip('"')
    if "low" in clean and "warn" in clean and clean["warn"] <= clean["low"]:
        errors.append("The yellow limit must be above the red limit")
    return clean, errors


def apply(app, clean: Dict[str, Any], set_autostart=winshell.set_autostart) -> None:
    changed = False
    with app.lock:
        for key, value in clean.items():
            if key == "autostart":
                continue
            if app.store[key] != value:
                app.store[key] = value
                changed = True
        app.store.save()
    if clean.get("update_check") and getattr(app, "update_checker", None) is not None:
        app.update_checker.check_soon()
    if "autostart" in clean:
        set_autostart(bool(clean["autostart"]))
    if changed:
        app.redraw_all()
        app.refresh_event.set()


@dataclass
class DeviceRow:
    key: str
    name: str            # as shown (the user's name if renamed)
    original: str
    kind: str
    status: str
    hidden: bool


def device_rows(app) -> List[DeviceRow]:
    rows = []
    with app.lock:
        for key, r in app.readings.items():
            eff = app.effective(r)
            status = (f"{r.level}%" if r.level is not None else "?") if r.online else (r.note or "off")
            if r.online and r.charging:
                status += ", charging"
            rows.append(DeviceRow(key, eff.name, r.name, r.kind, status, key in app.store["hidden"]))
    return sorted(rows, key=lambda d: d.name.lower())


def set_hidden(app, key: str, hidden: bool) -> None:
    keys = [k for k in app.store["hidden"] if k != key] + ([key] if hidden else [])
    app.set_setting("hidden", keys)


def forget(app, key: str) -> Optional[str]:
    """Drop one device's memory (its icon returns if it is seen again)."""
    r = app.readings.get(key)
    if r is None:
        return None
    if r.online:
        return "Only disconnected devices can be forgotten."
    app.forget(key)
    return None
