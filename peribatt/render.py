"""Draws the tray icon: a pictogram of the device (mouse, headset, keyboard,
gamepad) inside a frame that doubles as the battery gauge. The frame fills
clockwise from the top centre, like a progress ring, in its status colour
(green charging, red / yellow low, white or charcoal otherwise); the empty rest
of it is a faint track.

Everything is drawn as vector shapes at 4x size and scaled down with a
high-quality filter, so edges are smooth at every tray size (16-64 px).
Optionally the percentage can be shown instead of the pictogram.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Callable, Dict, List, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .model import HEADSET, KEYBOARD, MOUSE

SS = 4                     # supersampling factor
TRACK_ALPHA = 70           # opacity of the empty part of the gauge
TEXT_RGB = (250, 250, 250)
TEXT_RGB_LIGHT = (24, 24, 24)
GAMEPAD = "gamepad"
BOLT_RGB = (255, 214, 10)

# --- pictograms -------------------------------------------------------------
# Each draws a white silhouette on an "L" mask of side s (already supersampled)
# in unit coordinates: u(0.5) is the middle. They use nearly the whole square:
# at 16 px every pixel counts.


def _mouse(d: ImageDraw.ImageDraw, u: Callable[[float], float]) -> None:
    d.rounded_rectangle((u(.22), u(.04), u(.78), u(.96)), radius=u(.28), fill=255)
    gap = max(1, round(u(.06)))
    d.line((u(.5), u(.04), u(.5), u(.42)), fill=0, width=gap)                   # button split
    d.line((u(.22), u(.42), u(.78), u(.42)), fill=0, width=gap)
    d.rounded_rectangle((u(.45), u(.14), u(.55), u(.32)), radius=u(.05), fill=255)  # wheel


def _headset(d, u) -> None:
    w = round(u(.12))
    d.arc((u(.08), u(.02), u(.92), u(.86)), 180, 360, fill=255, width=w)         # headband
    d.line((u(.08) + w / 2, u(.42), u(.08) + w / 2, u(.6)), fill=255, width=w)
    d.line((u(.92) - w / 2, u(.42), u(.92) - w / 2, u(.6)), fill=255, width=w)
    d.rounded_rectangle((u(.02), u(.48), u(.36), u(.98)), radius=u(.1), fill=255)  # ear cups
    d.rounded_rectangle((u(.64), u(.48), u(.98), u(.98)), radius=u(.1), fill=255)


def _keyboard(d, u) -> None:
    d.rounded_rectangle((u(0), u(.16), u(1), u(.86)), radius=u(.1), fill=255)
    key, step = u(.13), .17
    for y, x0, n in ((.27, .08, 5), (.45, .16, 4)):
        for i in range(n):
            x = u(x0 + i * step)
            d.rounded_rectangle((x, u(y), x + key, u(y) + key), radius=u(.03), fill=0)
    d.rounded_rectangle((u(.24), u(.64), u(.76), u(.75)), radius=u(.04), fill=0)   # space bar


def _gamepad(d, u) -> None:
    d.ellipse((u(0), u(.3), u(.46), u(.9)), fill=255)                            # grips
    d.ellipse((u(.54), u(.3), u(1), u(.9)), fill=255)
    d.rounded_rectangle((u(.12), u(.18), u(.88), u(.66)), radius=u(.2), fill=255)
    w = round(u(.09))
    d.line((u(.12), u(.46), u(.36), u(.46)), fill=0, width=w)                    # d-pad
    d.line((u(.24), u(.34), u(.24), u(.58)), fill=0, width=w)
    for cx, cy in ((.7, .38), (.8, .52)):                                        # buttons
        d.ellipse((u(cx - .06), u(cy - .06), u(cx + .06), u(cy + .06)), fill=0)


def _battery(d, u) -> None:
    d.rounded_rectangle((u(.24), u(.12), u(.76), u(.98)), radius=u(.08), fill=255)
    d.rectangle((u(.38), u(.02), u(.62), u(.12)), fill=255)


PICTOGRAMS: Dict[str, Callable] = {MOUSE: _mouse, HEADSET: _headset, KEYBOARD: _keyboard,
                                   GAMEPAD: _gamepad}


def pictogram(kind: str, s: int) -> Image.Image:
    mask = Image.new("L", (s, s), 0)
    PICTOGRAMS.get(kind, _battery)(ImageDraw.Draw(mask), lambda v: v * s)
    return mask


# --- percentage text (optional mode) ----------------------------------------

def label(level) -> str:
    return "?" if level is None else str(max(0, min(100, int(level))))


def _font(px: int):
    try:
        return ImageFont.load_default(size=px)          # smooth built-in font (Pillow >= 10.1)
    except (TypeError, OSError):                         # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def text_mask(text: str, s: int) -> Image.Image:
    mask = Image.new("L", (s, s), 0)
    px = int(s * (0.7 if len(text) < 3 else 0.52))
    ImageDraw.Draw(mask).text((s / 2, s / 2), text, fill=255, font=_font(px), anchor="mm",
                              stroke_width=max(1, s // 36), stroke_fill=255)
    return mask


# --- the gauge --------------------------------------------------------------

def frame_width(size: int) -> float:
    return max(2.0, size * 0.13)


def frame_path(s: float, inset: float, radius: float, steps: int = 24) -> List[Tuple[float, float]]:
    """Points along the middle of the frame, clockwise from the top centre. The
    gauge is measured along this path, so each percent is the same length of
    frame everywhere (a pie slice would run fast on the sides, slow in corners)."""
    lo, hi = inset, s - inset
    r = max(0.0, radius - inset)
    pts = [((lo + hi) / 2, lo)]
    corners = ((hi - r, lo + r, -90), (hi - r, hi - r, 0), (lo + r, hi - r, 90), (lo + r, lo + r, 180))
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(((lo + hi) / 2, lo))
    return pts


def head(pts: List[Tuple[float, float]], share: float) -> List[Tuple[float, float]]:
    """The first ``share`` (0..1) of the path, by length."""
    segments = list(zip(pts, pts[1:], strict=False))
    lengths = [math.dist(a, b) for a, b in segments]
    goal = sum(lengths) * max(0.0, min(1.0, share))
    out = [pts[0]]
    for (a, b), n in zip(segments, lengths, strict=True):
        if goal <= n:
            t = goal / n if n else 0.0
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            return out
        out.append(b)
        goal -= n
    return out


def gauge_masks(s: int, width: float, radius: float, level) -> Tuple[Image.Image, Image.Image]:
    """(filled part, empty track) of the frame for a battery level. An unknown
    level (or the percentage mode, which shows the number) is a full frame."""
    full = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(full)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
    d.rounded_rectangle((width, width, s - 1 - width, s - 1 - width),
                        radius=max(0, radius - width), fill=0)
    if level is None or level >= 100:
        return full, Image.new("L", (s, s), 0)
    band = Image.new("L", (s, s), 0)
    part = head(frame_path(s, width / 2, radius), max(0, level) / 100)
    if len(part) > 1:
        # drawn wider than the frame and cut by the frame's shape: clean square ends
        ImageDraw.Draw(band).line(part, fill=255, width=int(width * 2), joint="curve")
    filled = ImageChops.multiply(full, band)
    return filled, ImageChops.subtract(full, filled)


def _bolt(s: int) -> Image.Image:
    m = Image.new("L", (s, s), 0)
    pts = [(.78, .44), (.58, .74), (.70, .74), (.63, .98), (.90, .64), (.77, .64), (.86, .44)]
    ImageDraw.Draw(m).polygon([(x * s, y * s) for x, y in pts], fill=255)
    return m


# --- the icon ---------------------------------------------------------------

@lru_cache(maxsize=256)
def render(size: int, kind: str, level, border_rgb, *, online: bool = True,
           border_on: bool = True, show_number: bool = False, charging: bool = False,
           light_taskbar: bool = False) -> Image.Image:
    """An RGBA icon. Cached: blinking or re-polling an unchanged device costs no
    drawing at all."""
    s = size * SS
    b = frame_width(size) * SS
    radius = s * 0.26
    fg = TEXT_RGB_LIGHT if light_taskbar else TEXT_RGB
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    if border_on:
        filled, track = gauge_masks(s, b, radius, None if show_number else level)
        img.paste(Image.new("RGBA", (s, s), border_rgb + (255,)), (0, 0), filled)
        img.paste(Image.new("RGBA", (s, s), border_rgb + (TRACK_ALPHA,)), (0, 0), track)

    pad = int(b + s * 0.05)
    inner = s - 2 * pad
    shape = text_mask(label(level), inner) if show_number else pictogram(kind, inner)
    layer = Image.new("RGBA", (inner, inner), fg + (255,))
    layer.putalpha(shape)
    img.alpha_composite(layer, (pad, pad))

    if charging and size >= 20:
        bolt = _bolt(s)
        halo = bolt.filter(ImageFilter.MaxFilter(2 * SS + 1))
        img.paste(Image.new("RGBA", (s, s), (25, 25, 25, 255)), (0, 0), halo)
        img.paste(Image.new("RGBA", (s, s), BOLT_RGB + (255,)), (0, 0), bolt)

    img = img.reduce(SS)                     # exact box filter: smooth edges, no ringing
    return img if online else greyed(img)


def greyed(img: Image.Image) -> Image.Image:
    """The whole icon in grey at reduced opacity: the device is off."""
    alpha = img.getchannel("A").point(lambda v: v * 3 // 5)
    grey = img.convert("L").convert("RGBA")
    grey.putalpha(alpha)
    return grey
