"""Stable tray icon ids and the icon handle cache (the Win32 parts run in test_windows)."""
from PIL import Image

from peribatt import trayicon


def test_icon_ids_are_stable_and_distinct():
    assert trayicon.icon_uid("logi-1a2b3c4d") == trayicon.icon_uid("logi-1a2b3c4d")
    ids = {trayicon.icon_uid(k) for k in ("logi-1", "hyperx-16ea", "bt-k8", "__none__")}
    assert len(ids) == 4 and all(0 < i < 2 ** 31 for i in ids)


def test_bgra_rows_swap_red_and_blue():
    img = Image.new("RGBA", (2, 1), (10, 20, 30, 40))
    assert trayicon.bgra_rows(img) == bytes([30, 20, 10, 40] * 2)


def test_handle_cache_builds_each_frame_once_and_frees_what_it_drops():
    made, freed = [], []
    cache = trayicon.HandleCache(lambda img: made.append(img) or len(made), freed.append, size=2)
    a, b, c = (Image.new("RGBA", (4, 4), (i, 0, 0, 255)) for i in range(3))
    for _ in range(5):                                  # a blink: two frames, over and over
        assert cache.get(a) == 1 and cache.get(b) == 2
    assert cache.made == 2                              # built once, not once per frame
    cache.get(c)                                        # a third frame drops the oldest
    assert freed == [1]
    cache.clear()
    assert sorted(freed) == [1, 2, 3]


def test_plain_pystray_icon_off_windows():
    class FakePystray:
        class Icon:
            def __init__(self, *a, **kw):
                self.args = a
    icon = trayicon.make_icon(FakePystray, "logi-1", "name", None, "title")
    assert isinstance(icon, FakePystray.Icon)
