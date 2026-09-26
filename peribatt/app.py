"""The app's brain, independent of any tray library: it merges readings from
all sources, decides colours, tooltips and notifications, and tells a
*backend* what to show. ``tray.py`` plugs in pystray; the tests plug in a fake.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Protocol, Sequence

from . import style
from .config import Store
from .history import History
from .model import DEVICE, HEADSET, Reading, merge
from .render import render

log = logging.getLogger("peribatt")

PLACEHOLDER = "__none__"
FLASH_PERIOD = 0.25          # seconds per on/off half-cycle while the mic is muted
FAST_POLL = 3.0              # seconds between cheap status checks (mute, power)
TOOLTIP_MAX = 127
# Devices from these sources vanish (rather than turn grey) when the source is switched off.
SOURCE_SWITCHES = {"bt-": "bluetooth", "xinput-": "xinput"}


class Backend(Protocol):
    icon_size: int

    def show(self, key: str, image, title: str) -> None: ...
    def set_image(self, key: str, image, cache_key: tuple) -> None: ...
    def set_title(self, key: str, title: str) -> None: ...
    def remove(self, key: str) -> None: ...
    def notify(self, key: str, title: str, message: str) -> None: ...
    def refresh_menus(self) -> None: ...


def describe(r: Reading, estimate: Optional[str] = None) -> str:
    """Tooltip text, e.g. "PRO Wireless: 76% - ~31h left"."""
    if not r.online:
        last = f", last {r.level}%" if r.level is not None else ""
        return _clip(f"{r.name}: {r.note or 'off'}{last}")
    parts = [f"{'~' if r.note == 'approximate' else ''}{r.level}%" if r.level is not None
             else "level unknown"]
    if r.charging:
        parts.append("charging" if (r.level or 0) < 100 else "full, on charger")
    if r.muted:
        parts.append("mic muted")
    if estimate:
        parts.append(estimate)
    return _clip(f"{r.name}: " + " - ".join(parts))


def _clip(text: str) -> str:
    return text if len(text) <= TOOLTIP_MAX else text[:TOOLTIP_MAX - 1] + "…"


class App:
    def __init__(self, store: Store, backend: Backend, sources: Sequence = (),
                 history: Optional[History] = None,
                 light_taskbar: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.backend = backend
        self.sources = list(sources)
        self.history = history or History()
        self.theme_probe = light_taskbar
        self.light = light_taskbar()
        self.mic_toggle: Optional[Callable[[], None]] = None
        self.clock = clock
        self.readings: Dict[str, Reading] = {}
        self.shown: Dict[str, tuple] = {}         # key -> cache key of the image on screen
        self.titles: Dict[str, str] = {}
        self.windows_muted = False
        self.flash_on = True
        self.alerted_low: set = set()
        self.alerted_full: set = set()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.flash_event = threading.Event()
        self._menus_dirty = False
        self._restore_known()

    # -- state ------------------------------------------------------------
    def _restore_known(self) -> None:
        """Devices seen in earlier runs start as grey icons with their last level."""
        for key, info in self.store.devices.items():
            self.readings[key] = Reading(key, info.get("name", key), info.get("kind", DEVICE),
                                         info.get("level"), online=False, note="not found yet")

    def effective(self, r: Reading) -> Reading:
        if not r.online:
            return r
        muted = r.muted or (self.store["windows_mute"] and self.windows_muted and r.kind == HEADSET)
        return r.with_(muted=muted) if muted != r.muted else r

    def apply(self, readings: Sequence[Reading]) -> None:
        """Results of a full poll. Devices that went missing turn grey."""
        with self.lock:
            fresh = {r.key: r for r in merge(readings)}
            for key, old in list(self.readings.items()):
                if key in fresh:
                    continue
                if old.online:
                    fresh[key] = old.with_(online=False, charging=False, muted=False,
                                           note="not found")
                elif key not in self.shown:
                    fresh[key] = old            # remembered from last run: show it greyed
            for r in fresh.values():
                self._update(r)
            self._sync_placeholder()
        self._flush_menus()

    def _flush_menus(self) -> None:
        if self._menus_dirty:
            self._menus_dirty = False
            self.backend.refresh_menus()

    def update(self, r: Reading) -> None:
        """A single pushed change (mute button, power switch)."""
        with self.lock:
            self._update(r)
            self._sync_placeholder()
        self._flush_menus()

    def _update(self, r: Reading) -> None:
        prev = self.readings.get(r.key)
        if r.level is None and prev is not None and prev.level is not None and not r.online:
            r = r.with_(level=prev.level)          # keep the last known level on a grey icon
        self.readings[r.key] = r
        if r.online:
            self.history.record(r, self.clock())
            self.store.devices[r.key] = {"name": r.name, "kind": r.kind, "level": r.level}
        self._draw(r.key)                      # before alerts: a notification needs the icon
        if r.online:
            self._alerts(r)

    def set_windows_muted(self, muted: bool) -> None:
        with self.lock:
            self.windows_muted = muted
            for key, r in self.readings.items():
                if r.kind == HEADSET:
                    self._draw(key)
        self.flash_event.set()

    # -- drawing ----------------------------------------------------------
    def visible(self, key: str) -> bool:
        if key in self.store["hidden"]:
            return False
        return all(self.store[setting] for prefix, setting in SOURCE_SWITCHES.items()
                   if key.startswith(prefix))

    def flashing(self, r: Reading) -> bool:
        return style.should_flash(self.effective(r), self.store["flash_on_mute"])

    def _draw(self, key: str) -> None:
        r = self.readings[key]
        if not self.visible(key):
            if key in self.shown:
                self.backend.remove(key)
                self.shown.pop(key, None)
                self.titles.pop(key, None)
                self._menus_dirty = True
            return
        eff = self.effective(r)
        light = self.light
        border = style.border_colour(eff, self.store["low"], self.store["warn"], light)
        border_on = self.flash_on or not self.flashing(r)
        cache_key = (self.backend.icon_size, eff.kind, eff.level, border, eff.online,
                     border_on, self.store["show_number"], eff.charging, light)
        image = render(*cache_key[:4], online=eff.online, border_on=border_on,
                       show_number=self.store["show_number"], charging=eff.charging,
                       light_taskbar=light)
        title = describe(eff, self.history.estimate(eff))
        if key not in self.shown:
            self.backend.show(key, image, title)
        elif self.shown[key] != cache_key:
            self.backend.set_image(key, image, cache_key)
        if key in self.shown and self.titles.get(key) != title:
            self.backend.set_title(key, title)
        if self.titles.get(key) != title:
            self._menus_dirty = True
        self.shown[key] = cache_key
        self.titles[key] = title
        if border_on is False or self.flashing(r):
            self.flash_event.set()

    def redraw_all(self) -> None:
        with self.lock:
            for key in list(self.readings):
                self._draw(key)
            self._sync_placeholder()
        self.backend.refresh_menus()

    def _sync_placeholder(self) -> None:
        visible = [k for k in self.shown if k != PLACEHOLDER]
        if not visible and PLACEHOLDER not in self.shown:
            img = render(self.backend.icon_size, DEVICE, None, style.GREY, online=False)
            self.backend.show(PLACEHOLDER, img, "Peripherals Battery: looking for devices…")
            self.shown[PLACEHOLDER] = ()
        elif visible and PLACEHOLDER in self.shown:
            self.backend.remove(PLACEHOLDER)
            self.shown.pop(PLACEHOLDER)

    def tick_flash(self) -> bool:
        """Advance the blink phase; True while something is blinking."""
        with self.lock:
            blinking = [k for k, r in self.readings.items()
                        if self.flashing(r) and self.visible(k)]
            if blinking:
                self.flash_on = not self.flash_on
            elif self.flash_on:
                return False
            else:
                self.flash_on = True
                blinking = list(self.readings)
            for key in blinking:
                self._draw(key)
            return bool(blinking) and any(self.flashing(self.readings[k]) for k in blinking)

    # -- notifications ------------------------------------------------------
    def _alerts(self, r: Reading) -> None:
        if r.level is None:
            return
        alert_at = self.store["alert_at"]
        if r.charging or r.level > alert_at + 5:
            self.alerted_low.discard(r.key)
        elif alert_at and r.level <= alert_at and r.key not in self.alerted_low:
            self.alerted_low.add(r.key)
            self.backend.notify(r.key, "Low battery", f"{r.name} is at {r.level}%. Time to charge.")
        if not r.charging:
            self.alerted_full.discard(r.key)
        elif self.store["notify_full"] and r.level >= 100 and r.key not in self.alerted_full:
            self.alerted_full.add(r.key)
            self.backend.notify(r.key, "Fully charged", f"{r.name} is full - you can unplug it.")

    # -- settings ---------------------------------------------------------
    def set_setting(self, key: str, value) -> None:
        with self.lock:
            self.store[key] = value
            self.store.save()
        self.redraw_all()
        self.refresh_event.set()

    def hide(self, key: str) -> None:
        hidden = list(self.store["hidden"])
        if key not in hidden:
            hidden.append(key)
        self.set_setting("hidden", hidden)

    def unhide_all(self) -> None:
        self.set_setting("hidden", [])

    def forget_offline(self) -> None:
        """Drop grey icons of devices that are gone for good."""
        with self.lock:
            for key, r in list(self.readings.items()):
                if not r.online:
                    self.readings.pop(key)
                    self.store.devices.pop(key, None)
                    if key in self.shown:
                        self.backend.remove(key)
                        self.shown.pop(key)
                        self.titles.pop(key, None)
            self._sync_placeholder()
            self.store.save()
        self.backend.refresh_menus()

    # -- loops ------------------------------------------------------------
    def poll_once(self) -> List[Reading]:
        light = self.theme_probe()
        if light != self.light:
            self.light = light
            self.redraw_all()
        results: List[Reading] = []
        for s in self.sources:
            try:
                results.extend(s.poll())
            except Exception as e:           # one broken provider must not stop the rest
                log.exception("%s poll failed: %s", getattr(s, "name", s), e)
        self.apply(results)
        self.store.save()
        return results

    def poll_loop(self) -> None:
        fast = [f for f in (getattr(s, "poll_fast", None) for s in self.sources) if f]
        while not self.stop_event.is_set():
            self.refresh_event.clear()
            self.poll_once()
            deadline = time.monotonic() + max(10, int(self.store["poll_seconds"]))
            while not self.stop_event.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                # Without a status-pushing source there is nothing to do until the next poll.
                if self.refresh_event.wait(min(FAST_POLL, left) if fast else left):
                    break
                for f in fast:
                    try:
                        f()
                    except Exception as e:
                        log.debug("fast poll: %s", e)

    def flash_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.tick_flash():
                time.sleep(FLASH_PERIOD)
            else:
                self.flash_event.wait()          # no timeout: zero wake-ups while idle
                self.flash_event.clear()

    def stop(self) -> None:
        self.stop_event.set()
        self.flash_event.set()
        self.refresh_event.set()

    def menu_status(self) -> List[str]:
        with self.lock:
            return [self.titles[k] for k in self.shown if k != PLACEHOLDER and k in self.titles]
