"""Border colour rules, kept separate from drawing so they are trivial to test.

    offline                  -> grey (the whole icon is greyed out)
    charging                 -> green
    level < low  (20%)       -> red
    level <= warn (33%)      -> yellow
    otherwise                -> neutral (white on a dark taskbar, charcoal on a light one)
"""
from __future__ import annotations

from typing import Tuple

from .model import Reading

RGB = Tuple[int, int, int]

GREEN: RGB = (46, 204, 113)
YELLOW: RGB = (241, 196, 15)
RED: RGB = (231, 76, 60)
WHITE: RGB = (245, 245, 245)
CHARCOAL: RGB = (38, 38, 38)
GREY: RGB = (128, 128, 128)

DEFAULT_LOW = 20
DEFAULT_WARN = 33


def neutral(light_taskbar: bool) -> RGB:
    return CHARCOAL if light_taskbar else WHITE


def border_colour(r: Reading, low: int = DEFAULT_LOW, warn: int = DEFAULT_WARN,
                  light_taskbar: bool = False) -> RGB:
    if not r.online:
        return GREY
    if r.charging:
        return GREEN
    if r.level is None:
        return neutral(light_taskbar)
    if r.level < low:
        return RED
    if r.level <= warn:
        return YELLOW
    return neutral(light_taskbar)


def should_flash(r: Reading, flash_on_mute: bool = True) -> bool:
    """The headset border blinks while its microphone is muted."""
    return flash_on_mute and r.online and r.muted
