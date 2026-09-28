"""Device settings the app can change without the maker's software - DPI, polling
rate, auto power-off and so on.

A source that can change settings of a device offers three methods:

* ``has_controls(key) -> bool``: cheap, no device I/O; decides whether the
  settings window shows a "Device settings" button for that device.
* ``controls(key) -> list[Control]``: reads the current values from the device.
* ``set_control(key, control_id, value)``: writes one value and checks it took.

Both of the last two raise :class:`Unavailable` with a sentence for the user when
the device cannot be reached (switched off, asleep, unplugged).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

RANGE, CHOICE, TOGGLE = "range", "choice", "toggle"


class Unavailable(Exception):
    """The device cannot be asked right now; the message is shown to the user."""


@dataclass
class Control:
    id: str
    label: str
    type: str                                   # RANGE, CHOICE or TOGGLE
    value: Any = None                           # None: not known (e.g. write-only)
    min: int = 0
    max: int = 0
    step: int = 1
    options: Sequence[Tuple[Any, str]] = field(default_factory=tuple)
    unit: str = ""
    help: str = ""

    def to_json(self) -> dict:
        out = {"id": self.id, "label": self.label, "type": self.type, "value": self.value,
               "unit": self.unit, "help": self.help}
        if self.type == RANGE:
            out.update(min=self.min, max=self.max, step=self.step)
        if self.type == CHOICE:
            out["options"] = [[v, label] for v, label in self.options]
        return out

    def check(self, value: Any) -> Any:
        """The value to write, or ValueError with a message for the user."""
        if self.type == TOGGLE:
            if not isinstance(value, bool):
                raise ValueError(f"{self.label}: expected on or off")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.label}: expected a number")
        value = int(value)
        if self.type == RANGE:
            if not self.min <= value <= self.max:
                raise ValueError(f"{self.label}: must be between {self.min} and {self.max}")
            # snap to the device's step, counted from its minimum
            return self.min + round((value - self.min) / self.step) * self.step
        if value not in [v for v, _ in self.options]:
            raise ValueError(f"{self.label}: {value} is not one of the choices")
        return value


def find(controls: List[Control], control_id: str) -> Optional[Control]:
    return next((c for c in controls if c.id == control_id), None)
