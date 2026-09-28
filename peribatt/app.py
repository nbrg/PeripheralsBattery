"""The app's brain, independent of any tray library: it merges readings from
all sources, decides colours, tooltips and notifications, and tells a
*backend* what to show. ``tray.py`` plugs in pystray; the tests plug in a fake.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Protocol, Sequence

from . import hidio, style
from .config import Store
from .estimate import Estimator
from .model import DEVICE, HEADSET, Reading, merge
from .render import render

log = logging.getLogger("peribatt")

PLACEHOLDER = "__none__"
FLASH_PERIOD = 0.25          # seconds per on/off half-cycle while the mic is muted
FAST_POLL = 3.0              # seconds between cheap status checks (mute, power)
TOOLTIP_MAX = 127
RESUME_SETTLE = 3.0          # after waking: give USB and the radios a moment...
RESUME_RECHECK = 15.0        # ...and look again once wireless devices have reconnected
SLEEP_GAP = 30.0             # a wait that overran by this much means the PC was asleep
SLOW_SOURCE = 5.0            # log a warning when one provider takes longer than this
# After a device is plugged in or connects, Windows reports some batteries (Bluetooth
# ones especially) a few seconds late: look again this long after the event.
RECHECKS = (3.0, 8.0, 15.0)
SEARCHING = "Peripherals Battery: looking for devices…"
NOTHING_FOUND = "Peripherals Battery: no devices found yet - right-click for Diagnostics"
ALL_HIDDEN = "Peripherals Battery: all devices are hidden - right-click to show them"
ALL_OFF = "Peripherals Battery: your devices are switched off"
# Pictures a device can be given by hand ("" = the one its source chose).
KIND_CHOICES = (("", "Automatic"), ("mouse", "Mouse"), ("keyboard", "Keyboard"),
                ("headset", "Headset"), ("gamepad", "Controller"), ("device", "Other"))
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
                 estimator: Optional[Estimator] = None,
                 light_taskbar: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.backend = backend
        self.sources = list(sources)
        self.estimator = estimator or Estimator()
        self.theme_probe = light_taskbar
        self.light = self._pick_light()
        self.mic_toggle: Optional[Callable[[], None]] = None
        self.open_settings: Optional[Callable[[], None]] = None    # set when a UI is available
        self.open_learn: Optional[Callable[[], None]] = None
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
        self._resumed_at: Optional[float] = None
        self.source_status: Dict[str, dict] = {}       # provider -> last poll's count/time/error
        self.polled_once = False
        self.open_diagnostics: Optional[Callable[[], None]] = None
        self.forgotten: set = set()                    # forgotten while off: ignore until seen again
        self._placeholder_light: Optional[bool] = None
        self.offline_since: Dict[str, float] = {}      # key -> when it was first seen off
        self.update_available: Optional[tuple] = None  # (version, url) of a newer release
        self.update_checker = None
        self._rechecks: List[float] = []               # extra polls after a device event
        self._restore_known()

    # -- state ------------------------------------------------------------
    def _restore_known(self) -> None:
        """Devices seen in earlier runs start as grey icons with their last level."""
        for key, info in self.store.devices.items():
            self.readings[key] = Reading(key, info.get("name", key), info.get("kind", DEVICE),
                                         info.get("level"), online=False, note="not found yet")

    def _pick_light(self) -> bool:
        """Draw for a light taskbar? "auto" asks Windows; a fixed colour is for
        see-through taskbars (TranslucentTB...) where the theme says nothing useful."""
        colour = self.store["icon_colour"]
        if colour == "white":
            return False                               # white icons, as on a dark taskbar
        if colour == "black":
            return True
        return bool(self.theme_probe())

    def effective(self, r: Reading) -> Reading:
        """The reading as shown: user-chosen name and picture, Windows mic mute folded in."""
        alias = self.store["names"].get(r.key)
        if alias and alias != r.name:
            r = r.with_(name=alias)
        kind = self.store["kinds"].get(r.key)
        if kind and kind != r.kind:
            r = r.with_(kind=kind)
        if not r.online:
            return r
        muted = r.muted or (self.store["windows_mute"] and self.windows_muted and r.kind == HEADSET)
        return r.with_(muted=muted) if muted != r.muted else r

    def rename(self, key: str, name: str) -> None:
        names = dict(self.store["names"])
        if name.strip():
            names[key] = name.strip()
        else:
            names.pop(key, None)
        self.set_setting("names", names)

    def apply(self, readings: Sequence[Reading]) -> None:
        """Results of a full poll. Devices that went missing turn grey."""
        from .bluetooth import drop_twins
        kept = drop_twins(list(readings))
        twins = {r.key for r in readings} - {r.key for r in kept}
        readings = kept
        with self.lock:
            for key in twins & set(self.readings):
                self._drop(key)                        # read over USB now: one icon, not two
            fresh = {}
            for r in merge(readings):
                if r.online:
                    self.forgotten.discard(r.key)      # it's back: remember it again
                elif r.key in self.forgotten:
                    continue                           # sources still report it; the user forgot it
                fresh[r.key] = r
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
            for key in [k for k in self.shown if k != PLACEHOLDER and k in self.readings]:
                if self.expired(key):
                    self._draw(key)                    # off for too long: leaves the tray
            self._sync_placeholder()
        self._flush_menus()

    def _drop(self, key: str) -> None:
        self.readings.pop(key, None)
        self.offline_since.pop(key, None)
        self.titles.pop(key, None)
        self.store.devices.pop(key, None)
        if self.shown.pop(key, None) is not None:
            self.backend.remove(key)
        self._menus_dirty = True

    def _flush_menus(self) -> None:
        if self._menus_dirty:
            self._menus_dirty = False
            self.backend.refresh_menus()

    def update(self, r: Reading) -> None:
        """A single pushed change (mute button, power switch)."""
        with self.lock:
            if r.online:
                self.forgotten.discard(r.key)
            elif r.key in self.forgotten:
                return
            self._update(r)
            self._sync_placeholder()
        self._flush_menus()

    def _update(self, r: Reading) -> None:
        prev = self.readings.get(r.key)
        if r.level is None and prev is not None and prev.level is not None and not r.online:
            r = r.with_(level=prev.level)          # keep the last known level on a grey icon
        self.readings[r.key] = r
        if r.online:
            self.offline_since.pop(r.key, None)
        else:
            self.offline_since.setdefault(r.key, self.clock())
        if r.online:
            self.estimator.record(r, self.clock())
            self.store.devices[r.key] = {"name": r.name, "kind": r.kind, "level": r.level}
        self._draw(r.key)                      # before alerts: a notification needs the icon
        if r.online and self.visible(r.key):     # no nagging about a device the user hid
            self._alerts(self.effective(r))

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

    def expired(self, key: str) -> bool:
        """Switched off for longer than the "remove after" setting."""
        minutes = self.store["hide_off_after"]
        since = self.offline_since.get(key)
        return bool(minutes) and since is not None and self.clock() - since >= minutes * 60

    def in_tray(self, key: str) -> bool:
        return self.visible(key) and not self.expired(key)

    def flashing(self, r: Reading) -> bool:
        return style.should_flash(self.effective(r), self.store["flash_on_mute"])

    def _draw(self, key: str) -> None:
        r = self.readings[key]
        if not self.in_tray(key):
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
        title = describe(eff, self.estimator.estimate(eff))
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
            self.light = self._pick_light()
            for key in list(self.readings):
                self._draw(key)
            self._sync_placeholder()
        self.backend.refresh_menus()

    def _sync_placeholder(self) -> None:
        """Until a device icon exists, one app icon: the menu (and Exit) must always
        be reachable, and it says whether the first search has finished."""
        visible = [k for k in self.shown if k != PLACEHOLDER]
        if any(not self.visible(k) for k in self.readings):
            title = ALL_HIDDEN
        elif self.readings and all(self.expired(k) for k in self.readings):
            title = ALL_OFF
        else:
            title = NOTHING_FOUND if self.polled_once else SEARCHING
        img = None
        if not visible and self._placeholder_light != self.light:
            img = render(self.backend.icon_size, DEVICE, None, style.neutral(self.light),
                         light_taskbar=self.light)
        if not visible and PLACEHOLDER not in self.shown:
            self.backend.show(PLACEHOLDER, img, title)
            self._placeholder_light = self.light
            self.shown[PLACEHOLDER] = ()
            self.titles[PLACEHOLDER] = title
        elif not visible:
            if img is not None:                        # taskbar theme changed
                self.backend.set_image(PLACEHOLDER, img, ())
                self._placeholder_light = self.light
            if self.titles.get(PLACEHOLDER) != title:
                self.backend.set_title(PLACEHOLDER, title)
                self.titles[PLACEHOLDER] = title
                self._menus_dirty = True
        elif visible and PLACEHOLDER in self.shown:
            self.backend.remove(PLACEHOLDER)
            self.shown.pop(PLACEHOLDER)
            self.titles.pop(PLACEHOLDER, None)
            self._placeholder_light = None

    def start(self) -> None:
        """Called once before polling starts: show the icon(s) straight away -
        remembered devices greyed, or the app icon - instead of after the first
        (possibly slow) search."""
        self.redraw_all()

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
        self._invalidate_sources()               # e.g. Bluetooth switched back on: look now
        self.refresh_event.set()

    def set_kind(self, key: str, kind: str) -> None:
        """The picture for one device; "" goes back to the one its source chose."""
        if kind not in dict(KIND_CHOICES):
            raise ValueError(f"unknown picture {kind!r}")
        kinds = {k: v for k, v in self.store["kinds"].items() if k != key}
        if kind:
            kinds[key] = kind
        self.set_setting("kinds", kinds)

    def found_update(self, version: str, url: str, announce: bool) -> None:
        """A newer release is out (from the update checker's thread)."""
        self.update_available = (version, url)
        if announce:
            self.backend.notify(None, "Update available",
                                f"Peripherals Battery {version} is out. Right-click the icon to get it.")
        self.backend.refresh_menus()

    def hide(self, key: str) -> None:
        hidden = list(self.store["hidden"])
        if key not in hidden:
            hidden.append(key)
        self.set_setting("hidden", hidden)

    def unhide_all(self) -> None:
        self.set_setting("hidden", [])

    def forget(self, key: str) -> bool:
        """Drop a switched-off device's grey icon and memory. Sources that still
        remember it are told to let go; it comes back when it is seen online."""
        with self.lock:
            r = self.readings.get(key)
            if r is None or r.online:
                return False
            self.readings.pop(key)
            self.store.devices.pop(key, None)
            self.forgotten.add(key)
            for s in self.sources:
                forget = getattr(s, "forget", None)
                if forget:
                    try:
                        forget(key)
                    except Exception as e:
                        log.debug("%s forget: %s", getattr(s, "name", s), e)
            if key in self.shown:
                self.backend.remove(key)
                self.shown.pop(key)
                self.titles.pop(key, None)
            self._sync_placeholder()
            self.store.save()
        self.backend.refresh_menus()
        return True

    # -- device settings (DPI, polling rate...) ---------------------------------------
    def _controller(self, key: str):
        """The source that can change settings of this device, or None."""
        for s in self.sources:
            while hasattr(s, "source"):                # Switchable / Throttled wrappers
                s = s.source
            has = getattr(s, "has_controls", None)
            if has is not None and has(key):
                return s
        return None

    def has_controls(self, key: str) -> bool:
        try:
            return self._controller(key) is not None
        except Exception:
            log.exception("has_controls %s", key)
            return False

    def device_controls(self, key: str) -> list:
        """The device's settings, read from it now. Raises controls.Unavailable."""
        src = self._controller(key)
        if src is None:
            raise ValueError("this device has no settings the app can change")
        return src.controls(key)

    def set_device_control(self, key: str, control_id: str, value) -> list:
        src = self._controller(key)
        if src is None:
            raise ValueError("this device has no settings the app can change")
        log.info("device setting %s: %s = %r", key, control_id, value)
        return src.set_control(key, control_id, value)

    def forget_offline(self) -> None:
        """Drop grey icons of devices that are gone for good."""
        for key, r in list(self.readings.items()):
            if not r.online:
                self.forget(key)

    # -- loops ------------------------------------------------------------
    def poll_once(self) -> List[Reading]:
        if self._pick_light() != self.light:
            self.redraw_all()
        results: List[Reading] = []
        for s in self.sources:
            name = getattr(s, "name", type(s).__name__)
            started = time.monotonic()
            error = ""
            found: List[Reading] = []
            try:
                found = s.poll()
            except Exception as e:           # one broken provider must not stop the rest
                error = f"{type(e).__name__}: {e}"
                log.exception("%s poll failed", name)
            took = time.monotonic() - started
            results.extend(found)
            prev = self.source_status.get(name)
            self.source_status[name] = {"count": len(found), "seconds": took, "error": error}
            if prev is None or (prev["count"], prev["error"]) != (len(found), error):
                log.info("%s: %d device(s) in %.2f s%s", name, len(found), took,
                         f" ({error})" if error else "")
            if took > SLOW_SOURCE:
                log.warning("%s took %.1f s to poll", name, took)
        self.polled_once = True
        self.apply(results)
        self.store.save()
        return results

    def resumed(self) -> None:
        """The PC woke up. Safe to call from any thread (the Windows power
        callback, or the poll loop's own clock check)."""
        log.info("resumed from sleep: re-checking devices")
        self._resumed_at = time.monotonic()
        self.refresh_event.set()

    def devices_changed(self) -> None:
        """A device was plugged in, removed or connected: look now, not at the next
        poll - and a few more times shortly after, for batteries reported late."""
        log.info("device plugged in or removed: re-checking")
        now = time.monotonic()
        self._rechecks = [now + d for d in RECHECKS]
        self._invalidate_sources()
        self.refresh_event.set()

    def _invalidate_sources(self) -> None:
        for s in self.sources:                          # cached (throttled) sources look again
            invalidate = getattr(s, "invalidate", None)
            if invalidate:
                invalidate()

    def _after_resume(self) -> Optional[float]:
        """On the poll thread: let devices settle, drop handles that may have gone
        stale while asleep, and return when to look again."""
        at = self._resumed_at
        if at is None:
            return None
        settle = at + RESUME_SETTLE - time.monotonic()
        if settle > 0 and self.stop_event.wait(settle):
            return None
        self._resumed_at = None
        hidio.device_list_changed()           # devices may have come or gone while asleep
        for s in self.sources:
            reset = getattr(s, "reset", None)
            if reset:
                try:
                    reset()
                except Exception as e:
                    log.debug("%s reset: %s", getattr(s, "name", s), e)
        return time.monotonic() + RESUME_RECHECK - RESUME_SETTLE

    def _wait(self, timeout: float) -> bool:
        """Waits for a refresh request; also notices a sleep in between, because
        the wall clock then moves far more than the wait was meant to take."""
        before = time.time()
        woken = self.refresh_event.wait(timeout)
        if not woken and time.time() - before > timeout + SLEEP_GAP and self._resumed_at is None:
            self.resumed()
            return True
        return woken

    def poll_loop(self) -> None:
        fast = [f for f in (getattr(s, "poll_fast", None) for s in self.sources) if f]
        while not self.stop_event.is_set():
            try:
                self._poll_cycle(fast)
            except Exception:
                # Whatever went wrong, keep the app alive and try again shortly.
                log.exception("poll cycle failed")
                self.stop_event.wait(10)

    def _poll_cycle(self, fast) -> None:
        self.refresh_event.clear()
        recheck = self._after_resume()
        if self.stop_event.is_set():
            return
        self.poll_once()
        now = time.monotonic()
        deadline = now + max(10, int(self.store["poll_seconds"]))
        if recheck is not None:
            deadline = min(deadline, recheck)
        self._rechecks = [t for t in self._rechecks if t > now]
        if self._rechecks:
            deadline = min(deadline, self._rechecks[0])
            self._invalidate_sources()
        while not self.stop_event.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            # Without a status-pushing source there is nothing to do until the next poll.
            if self._wait(min(FAST_POLL, left) if fast else left):
                break
            for f in fast:
                try:
                    f()
                except Exception as e:
                    log.debug("fast poll: %s", e)

    def flash_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                blinking = self.tick_flash()
            except Exception:
                log.exception("blink failed")
                blinking = False
            if blinking:
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
