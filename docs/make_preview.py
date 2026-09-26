"""Regenerates docs/icons.png: every icon state at the common tray sizes."""
from PIL import Image, ImageDraw

from peribatt import style
from peribatt.model import Reading
from peribatt.render import render

CASES = [
    ("mouse 76%", Reading("m", "", "mouse", 76)),
    ("keyboard 91%", Reading("k", "", "keyboard", 91)),
    ("mouse 15%", Reading("m", "", "mouse", 15)),
    ("headset 28%", Reading("h", "", "headset", 28)),
    ("charging", Reading("h", "", "headset", 64, charging=True)),
    ("mic muted (blink)", Reading("h", "", "headset", 64, muted=True)),
    ("full on charger", Reading("m", "", "mouse", 100, charging=True)),
    ("switched off", Reading("m", "", "mouse", 76, online=False)),
    ("controller 60%", Reading("x", "", "gamepad", 60)),
]
SIZES = (16, 24, 32, 64)


def main(path="docs/icons.png"):
    cell = sum(SIZES) + 8 * len(SIZES) + 12
    sheet = Image.new("RGBA", (cell * 3, 3 * 100), (32, 32, 32, 255))
    d = ImageDraw.Draw(sheet)
    for i, (label, r) in enumerate(CASES):
        x0, y0 = (i % 3) * cell + 8, (i // 3) * 100 + 8
        d.text((x0, y0), label, fill=(200, 200, 200, 255))
        x = x0
        for s in SIZES:
            frame = render(s, r.kind, r.level, style.border_colour(r), online=r.online,
                           border_on=not r.muted, charging=r.charging)
            sheet.alpha_composite(frame, (x, y0 + 18))
            x += s + 8
    sheet.resize((sheet.width * 2, sheet.height * 2), Image.NEAREST).save(path)


if __name__ == "__main__":
    main()
