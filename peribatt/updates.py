"""Is there a newer release? Asked once a day, from GitHub's public API.

One small anonymous request (``/releases/latest``); nothing is downloaded or
installed. When a newer version is out the tray says so once, and the menu and
the settings window offer a link to the release page. "Check for updates" in
the settings turns it off.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from typing import Callable, Optional, Tuple

from . import __version__

log = logging.getLogger("peribatt")

REPO = "nbrg/PeripheralsBattery"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
EVERY = 24 * 3600            # seconds between checks
WAKE = 3600                  # how often the thread looks at the clock
FIRST_DELAY = 60             # not in the middle of start-up
TIMEOUT = 10


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """'v0.6.0' / '0.6.0' -> (0, 6, 0); anything else (a pre-release tag...) -> None."""
    m = re.fullmatch(r"\s*v?(\d+(?:\.\d+){0,3})\s*", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def fetch_latest(opener=urllib.request.urlopen) -> Tuple[str, str]:
    """(version, release page URL). Raises on network or API errors."""
    req = urllib.request.Request(API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"PeripheralsBattery/{__version__}",
    })
    with opener(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or parse_version(tag) is None:
        raise ValueError(f"not a release: {tag!r}")
    return tag.lstrip("vV").strip(), str(data.get("html_url") or RELEASES_URL)


class UpdateChecker:
    """Runs :meth:`check_if_due` once an hour on a daemon thread. ``on_found`` gets
    (version, url) for a release newer than this one; ``announce`` is True only the
    first time that version is seen (the tray notification is sent once)."""

    def __init__(self, store, on_found: Callable[[str, str, bool], None],
                 fetch: Callable[[], Tuple[str, str]] = fetch_latest,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.on_found = on_found
        self.fetch = fetch
        self.clock = clock
        self.latest: Optional[Tuple[str, str]] = None
        self.error = ""
        self._stop = threading.Event()
        self._now = threading.Event()

    def check_if_due(self, force: bool = False) -> None:
        if not self.store["update_check"]:
            return
        now = self.clock()
        if not force and now - float(self.store["update_last"]) < EVERY:
            return
        self.store["update_last"] = now
        self.store.save()
        try:
            version, url = self.fetch()
        except Exception as e:                     # offline, rate limited, GitHub down...
            self.error = str(e)
            log.info("update check: %s", e)
            return
        self.error = ""
        if not newer(version):
            self.latest = None
            return
        self.latest = (version, url)
        announce = self.store["update_told"] != version
        if announce:
            self.store["update_told"] = version
            self.store.save()
        log.info("a newer version is out: %s", version)
        self.on_found(version, url, announce)

    def check_soon(self) -> None:
        """The setting was switched on: ask now rather than tomorrow."""
        self.store["update_last"] = 0.0
        self._now.set()

    def run(self) -> None:
        if self._stop.wait(FIRST_DELAY):
            return
        while not self._stop.is_set():
            try:
                self.check_if_due()
            except Exception:
                log.exception("update check failed")
            self._now.wait(WAKE)
            self._now.clear()

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="updates").start()

    def stop(self) -> None:
        self._stop.set()
        self._now.set()
