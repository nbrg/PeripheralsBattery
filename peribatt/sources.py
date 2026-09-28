"""Builds the list of providers. Order matters only for the probe report."""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from .bluetooth import BluetoothSource
from .config import Store, app_dir
from .hidpp import LogitechSource
from .hsc import HeadsetControlSource
from .hyperx import HYPERX_VID, HyperXSource
from .hyperx import PRODUCTS as HYPERX_PRODUCTS
from .model import Reading
from .razer import RazerSource
from .recipes import RecipeSource
from .recipes import load as load_recipes
from .xinput import XInputSource


class Switchable:
    """Skips a source entirely while its setting is off."""

    def __init__(self, source, enabled: Callable[[], bool]):
        self.source = source
        self.enabled = enabled
        self.name = getattr(source, "name", "source")

    def poll(self) -> List[Reading]:
        return self.source.poll() if self.enabled() else []

    def reset(self) -> None:
        reset = getattr(self.source, "reset", None)
        if reset:
            reset()


class Throttled:
    """Polls the wrapped source at most every ``seconds``; in between the last
    result is returned. For sources that are slow to query but change slowly."""

    def __init__(self, source, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.source = source
        self.seconds = seconds
        self.clock = clock
        self.name = getattr(source, "name", "source")
        self._at: Optional[float] = None
        self._last: List[Reading] = []

    def poll(self) -> List[Reading]:
        now = self.clock()
        if self._at is None or now - self._at >= self.seconds:
            self._last = self.source.poll()
            self._at = now
        return list(self._last)

    def invalidate(self) -> None:
        """A setting changed: ask the source again on the next poll."""
        self._at = None

    def reset(self) -> None:
        """Forget the cached result so the next poll asks again (after sleep)."""
        self._at = None
        reset = getattr(self.source, "reset", None)
        if reset:
            reset()


def build_sources(store: Store, on_change: Optional[Callable[[Reading], None]] = None,
                  api=None) -> list:
    recipes = RecipeSource(load_recipes([app_dir() / "recipes.json"]), api=api, on_change=on_change)
    skip = recipes.claimed | {(HYPERX_VID, pid) for pid in HYPERX_PRODUCTS}
    return [
        LogitechSource(api=api, known=store.logitech_slots),
        HyperXSource(api=api, on_change=on_change),
        RazerSource(api=api),
        recipes,
        Throttled(Switchable(BluetoothSource(), lambda: store["bluetooth"]), 120),
        Switchable(XInputSource(), lambda: store["xinput"]),
        Throttled(HeadsetControlSource(lambda: store["headsetcontrol"], skip), 300),
    ]


def supported_check(sources) -> Callable[[int, int], bool]:
    """Whether a USB id is already read natively (for the learn wizard's list)."""
    from .hidpp import LOGITECH_VID
    from .razer import RAZER_VID
    claimed = {(HYPERX_VID, pid) for pid in HYPERX_PRODUCTS}
    for s in sources:
        claimed |= getattr(s, "claimed", set())
    return lambda vid, pid: vid in (LOGITECH_VID, RAZER_VID) or (vid, pid) in claimed


def reload_recipes(sources) -> None:
    for s in sources:
        if isinstance(s, RecipeSource):
            s.set_recipes(load_recipes([app_dir() / "recipes.json"]))
