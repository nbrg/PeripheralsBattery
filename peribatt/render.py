"""Draws the tray icon: a colour-coded frame around a pictogram of the device
(mouse, headset, keyboard, gamepad). The pictogram doubles as the gauge - it is
solid up to the battery level and faint above it, like a glass filling up.

Everything is drawn as vector shapes at 4x size and scaled down with a
high-quality filter, so edges are smooth at every tray size (16-64 px).
Optionally the percentage can be shown instead of the pictogram.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable, Dict

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .model import HEADSET, KEYBOARD, MOUSE

SS = 4                     # supersampling factor
EMPTY_ALPHA = 80           # opacity of the "empty" part of the pictogram
TEXT_RGB = (250, 250, 250)
TEXT_RGB_LIGHT = (24, 24, 24)
GAMEPAD = "gamepad"
BOLT_RGB = (255, 214, 10)

# --- pictograms -------------------------------------------------------------
# Each draws a white silhouette on an "L" mask of side s (already supersampled)
# in unit coordinates: u(0.5) is the middle.


def _mouse(d: ImageDraw.ImageDraw, u: Callable[[float], float]) -> None:
    d.rounded_rectangle((u(.30), u(.12), u(.70), u(.90)), radius=u(.20), fill=255)
    gap = max(1, round(u(.05)))
    d.line((u(.5), u(.12), u(.5), u(.42)), fill=0, width=gap)                   # button split
    d.line((u(.30), u(.42), u(.70), u(.42)), fill=0, width=gap)
    d.rounded_rectangle((u(.46), u(.2), u(.54), u(.34)), radius=u(.04), fill=255)  # wheel


def _headset(d, u) -> None:
    w = round(u(.09))
    d.arc((u(.17), u(.1), u(.83), u(.8)), 180, 360, fill=255, width=w)          # headband
    d.line((u(.17) + w / 2, u(.45), u(.17) + w / 2, u(.58)), fill=255, width=w)
    d.line((u(.83) - w / 2, u(.45), u(.83) - w / 2, u(.58)), fill=255, width=w)
    d.rounded_rectangle((u(.1), u(.5), u(.36), u(.9)), radius=u(.08), fill=255)   # ear cups
    d.rounded_rectangle((u(.64), u(.5), u(.9), u(.9)), radius=u(.08), fill=255)


def _keyboard(d, u) -> None:
    d.rounded_rectangle((u(.04), u(.24), u(.96), u(.78)), radius=u(.08), fill=255)
    key, step = u(.1), .135
    for y, x0, n in ((.32, .12, 6), (.46, .18, 5)):
        for i in range(n):
            x = u(x0 + i * step)
            d.rounded_rectangle((x, u(y), x + key, u(y) + key), radius=u(.02), fill=0)
    d.rounded_rectangle((u(.26), u(.6), u(.74), u(.69)), radius=u(.03), fill=0)   # space bar


def _gamepad(d, u) -> None:
    d.ellipse((u(.04), u(.34), u(.44), u(.84)), fill=255)                        # grips
    d.ellipse((u(.56), u(.34), u(.96), u(.84)), fill=255)
    d.rounded_rectangle((u(.18), u(.26), u(.82), u(.64)), radius=u(.16), fill=255)
    w = round(u(.07))
    d.line((u(.16), u(.48), u(.36), u(.48)), fill=0, width=w)                    # d-pad
    d.line((u(.26), u(.38), u(.26), u(.58)), fill=0, width=w)
    for cx, cy in ((.68, .41), (.78, .52)):                                      # buttons
        d.ellipse((u(cx - .05), u(cy - .05), u(cx + .05), u(cy + .05)), fill=0)


def _battery(d, u) -> None:
    d.rounded_rectangle((u(.28), u(.18), u(.72), u(.9)), radius=u(.07), fill=255)
    d.rectangle((u(.4), u(.1), u(.6), u(.18)), fill=255)


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
    px = int(s * (0.62 if len(text) < 3 else 0.46))
    ImageDraw.Draw(mask).text((s / 2, s / 2), text, fill=255, font=_font(px), anchor="mm",
                              stroke_width=max(1, s // 36), stroke_fill=255)
    return mask


# --- the icon ---------------------------------------------------------------

def frame_width(size: int) -> float:
    return max(1.6, size * 0.1)


def _level_mask(silhouette: Image.Image, level) -> Image.Image:
    """The part of the pictogram below the battery level."""
    box = silhouette.getbbox()
    if level is None or box is None:
        return silhouette
    top, bottom = box[1], box[3]
    cut = bottom - (bottom - top) * max(0, min(100, level)) / 100
    band = Image.new("L", silhouette.size, 0)
    ImageDraw.Draw(band).rectangle((0, cut, silhouette.width, silhouette.height), fill=255)
    return ImageChops.multiply(silhouette, band)


def _bolt(s: int) -> Image.Image:
    m = Image.new("L", (s, s), 0)
    pts = [(.78, .44), (.58, .74), (.70, .74), (.63, .98), (.90, .64), (.77, .64), (.86, .44)]
    ImageDraw.Draw(m).polygon([(x * s, y * s) for x, y in pts], fill=255)
    return m


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
        ring = Image.new("L", (s, s), 0)
        d = ImageDraw.Draw(ring)
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
        d.rounded_rectangle((b, b, s - 1 - b, s - 1 - b), radius=max(0, radius - b), fill=0)
        img.paste(Image.new("RGBA", (s, s), border_rgb + (255,)), (0, 0), ring)

    pad = int(b + s * 0.045)
    inner = s - 2 * pad
    if show_number:
        shape = full = text_mask(label(level), inner)
    else:
        shape = pictogram(kind, inner)
        full = _level_mask(shape, level)
    layer = Image.new("RGBA", (inner, inner), fg + (255,))
    layer.putalpha(ImageChops.lighter(full, shape.point(lambda v: v * EMPTY_ALPHA // 255)))
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
