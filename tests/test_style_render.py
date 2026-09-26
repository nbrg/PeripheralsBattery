import pytest

from peribatt import style
from peribatt.model import HEADSET, KEYBOARD, MOUSE, Reading, merge
from peribatt.render import label, pictogram, render


def r(level=None, **kw):
    return Reading("k", "Dev", kw.pop("kind", MOUSE), level, **kw)


@pytest.mark.parametrize("level,colour", [
    (0, style.RED), (19, style.RED), (20, style.YELLOW), (33, style.YELLOW),
    (34, style.WHITE), (100, style.WHITE), (None, style.WHITE),
])
def test_border_thresholds(level, colour):
    assert style.border_colour(r(level)) == colour


def test_charging_beats_low_and_offline_beats_everything():
    assert style.border_colour(r(5, charging=True)) == style.GREEN
    assert style.border_colour(r(5, charging=True, online=False)) == style.GREY


def test_neutral_follows_taskbar_theme():
    assert style.border_colour(r(80), light_taskbar=True) == style.CHARCOAL


def test_custom_thresholds():
    assert style.border_colour(r(25), low=30, warn=50) == style.RED
    assert style.border_colour(r(45), low=30, warn=50) == style.YELLOW


def test_flash_only_when_online_and_muted():
    assert style.should_flash(r(50, muted=True, kind=HEADSET))
    assert not style.should_flash(r(50, muted=True, online=False))
    assert not style.should_flash(r(50, muted=True), flash_on_mute=False)
    assert not style.should_flash(r(50))


def test_merge_prefers_online_then_charging():
    off = Reading("a", "Mouse", MOUSE, 40, online=False)
    on = Reading("a", "Mouse", MOUSE, 50)
    chg = Reading("a", "Mouse", MOUSE, 50, charging=True)
    assert merge([off, on]) == [on]
    assert merge([on, chg, off]) == [chg]
    assert len(merge([on, Reading("b", "Other")])) == 2


def test_label_and_text_units():
    assert label(None) == "?"
    assert label(150) == "100"
    assert label(-3) == "0"


def pixels(img):
    flat = getattr(img, "get_flattened_data", None)       # Pillow >= 12
    return list(flat() if flat else img.getdata())


def colours(img):
    return {px[:3] for px in pixels(img) if px[3] > 200}


def near(a, b, tol=10):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


@pytest.mark.parametrize("size", [16, 20, 24, 32, 48, 64])
def test_render_sizes_and_frame_colour(size):
    img = render(size, MOUSE, 57, style.RED)
    assert img.size == (size, size) and img.mode == "RGBA"
    # the frame is drawn in the requested colour
    assert near(img.getpixel((0, size // 2))[:3], style.RED)
    # corners are transparent (rounded frame)
    assert img.getpixel((0, 0))[3] == 0


def test_border_off_frame_has_no_frame_pixels():
    on = render(32, HEADSET, 57, style.GREEN)
    off = render(32, HEADSET, 57, style.GREEN, border_on=False)
    assert near(on.getpixel((1, 16))[:3], style.GREEN)
    assert off.getpixel((1, 16))[3] == 0


def test_pictogram_fills_with_level():
    empty = render(32, MOUSE, 0, style.RED)
    half = render(32, MOUSE, 50, style.YELLOW)
    full = render(32, MOUSE, 100, style.WHITE)
    solid = [sum(1 for px in pixels(i) if px[:3] == (250, 250, 250) and px[3] > 200)
             for i in (empty, half, full)]
    assert solid[0] < solid[1] < solid[2]


def test_every_kind_has_a_distinct_pictogram():
    masks = {kind: pictogram(kind, 64).tobytes() for kind in (MOUSE, HEADSET, KEYBOARD, "gamepad", "x")}
    assert len(set(masks.values())) == 5
    assert all(pictogram(k, 64).getbbox() for k in masks)


def test_charging_bolt_only_on_bigger_icons():
    assert render(24, MOUSE, 50, style.GREEN, charging=True).tobytes() != \
        render(24, MOUSE, 50, style.GREEN).tobytes()
    assert render(16, MOUSE, 50, style.GREEN, charging=True).tobytes() == \
        render(16, MOUSE, 50, style.GREEN).tobytes()


def test_offline_icon_is_grey():
    img = render(32, MOUSE, 80, style.GREY, online=False)
    for red, green, blue in colours(img) | {px[:3] for px in pixels(img) if px[3]}:
        assert red == green == blue
    assert max(px[3] for px in pixels(img)) < 255


def test_digits_can_be_hidden():
    with_digits = render(32, MOUSE, 88, style.WHITE, show_number=True)
    without = render(32, MOUSE, 88, style.WHITE)
    assert with_digits.tobytes() != without.tobytes()


def test_render_is_cached():
    assert render(24, MOUSE, 42, style.WHITE) is render(24, MOUSE, 42, style.WHITE)
