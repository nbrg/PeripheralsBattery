"""Logitech mouse settings over HID++ 2.0 - what G HUB changes, without G HUB.

Feature layouts as Solaar reads them:

* 0x2201 adjustable DPI
    fn 1 getSensorDpiList(sensor) -> <sensor> then up to 7 big-endian words, 0-terminated.
         A word 0b111x_xxxx_xxxx_xxxx is a *step*: ``lo, step, hi`` describes a range
         (the G PRO Wireless: 100..25600 in steps of 50); otherwise it is a list.
    fn 2 getSensorDpi(sensor)   -> <sensor> <dpi hi> <dpi lo> <default hi> <default lo>
    fn 3 setSensorDpi(sensor, dpi hi, dpi lo)
* 0x8060 report rate
    fn 0 getReportRateList -> bit n set: a report every n+1 ms is supported
    fn 1 getReportRate     -> ms
    fn 2 setReportRate(ms)
* 0x8100 onboard profiles
    fn 1 setOnboardMode(1 = onboard, 2 = host)
    fn 2 getOnboardMode -> mode

With onboard profiles on, a mouse follows the DPI levels and rate saved on it (the
DPI button cycles through them) and may ignore a change made from here, so every
write is read back, and a change that did not take says what to do.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .controls import CHOICE, RANGE, TOGGLE, Control, Unavailable, find

F_DPI = 0x2201
F_RATE = 0x8060
F_ONBOARD = 0x8100
ONBOARD, HOST = 1, 2
SENSOR = 0

ONBOARD_HELP = ("On: the mouse uses the settings saved on it, and its DPI button switches between "
                "them. Off: the DPI and polling rate chosen here apply.")


def capabilities(ch, index: int) -> Dict[str, Optional[int]]:
    return {"dpi": ch.feature_index(index, F_DPI),
            "rate": ch.feature_index(index, F_RATE),
            "onboard": ch.feature_index(index, F_ONBOARD)}


def parse_dpi_list(p: Sequence[int]) -> Tuple[List[int], Optional[int]]:
    """(values, step) from a getSensorDpiList reply. With a step, values is [lo, hi]."""
    values: List[int] = []
    step = None
    for i in range(1, 15, 2):
        word = (p[i] << 8) | p[i + 1]
        if word == 0:
            break
        if word >> 13 == 0b111:
            step = word & 0x1FFF
        else:
            values.append(word)
    return values, step


def rate_options(mask: int) -> List[Tuple[int, str]]:
    """[(ms, "1000 Hz"), ...], fastest first."""
    return [(ms, f"{round(1000 / ms)} Hz") for ms in range(1, 9) if mask & (1 << (ms - 1))]


def read(ch, index: int, caps: Dict[str, Optional[int]]) -> List[Control]:
    out: List[Control] = []
    fi = caps.get("dpi")
    if fi:
        values, step = parse_dpi_list(ch.call(index, fi, 1, (SENSOR,)))
        p = ch.call(index, fi, 2, (SENSOR,))
        dpi = (p[1] << 8) | p[2] or (p[3] << 8) | p[4]
        if step and len(values) == 2:
            out.append(Control("dpi", "Sensitivity", RANGE, dpi, values[0], values[1], step,
                               unit="DPI", help="How far the pointer moves per inch of mouse movement."))
        elif values:
            out.append(Control("dpi", "Sensitivity", CHOICE, dpi,
                               options=[(v, f"{v} DPI") for v in values], unit="DPI"))
    fi = caps.get("rate")
    if fi:
        options = rate_options(ch.call(index, fi, 0)[0])
        if options:
            out.append(Control("rate", "Polling rate", CHOICE, ch.call(index, fi, 1)[0], options=options,
                               help="How often the mouse reports its position. Faster uses more battery."))
    fi = caps.get("onboard")
    if fi:
        mode = ch.call(index, fi, 2)[0]
        out.append(Control("onboard", "Onboard profiles", TOGGLE, mode == ONBOARD, help=ONBOARD_HELP))
    return out


def write(ch, index: int, caps: Dict[str, Optional[int]], control_id: str, value) -> List[Control]:
    control = find(read(ch, index, caps), control_id)
    if control is None:
        raise ValueError("this device has no such setting")
    value = control.check(value)
    if control_id == "dpi":
        ch.call(index, caps["dpi"], 3, (SENSOR, value >> 8, value & 0xFF))
    elif control_id == "rate":
        ch.call(index, caps["rate"], 2, (value,))
    elif control_id == "onboard":
        ch.call(index, caps["onboard"], 1, (ONBOARD if value else HOST,))
    after = read(ch, index, caps)
    now = find(after, control_id)
    if now is not None and now.value != value:
        onboard = find(after, "onboard")
        if onboard is not None and onboard.value:
            raise Unavailable(f"The mouse kept its own {control.label.lower()}. Turn Onboard profiles "
                              "off to set it from here.")
        raise Unavailable(f"The mouse did not take the new {control.label.lower()}.")
    return after
