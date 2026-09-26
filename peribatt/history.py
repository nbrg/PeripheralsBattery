"""Battery history and time-remaining estimates.

Every change of level is kept in memory per device (and appended to
``history.csv`` for anyone who wants to chart it). The estimate is a
least-squares line through the current discharge (or charge) session, which
smooths out the jitter of voltage-based readings such as the G PRO's.
"""
from __future__ import annotations

import csv
import logging
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from .model import Reading

log = logging.getLogger("peribatt")

MIN_SPAN_S = 20 * 60          # need 20 minutes of data...
MIN_DELTA = 2                 # ...and a 2% change before guessing
MAX_SAMPLES = 256
MAX_CSV_BYTES = 1_000_000

Sample = Tuple[float, int]


def slope_per_hour(samples: List[Sample]) -> Optional[float]:
    """Least-squares slope in %/hour, or None when there is too little data."""
    if len(samples) < 2:
        return None
    t0 = samples[0][0]
    xs = [(t - t0) / 3600 for t, _ in samples]
    ys = [lvl for _, lvl in samples]
    if xs[-1] * 3600 < MIN_SPAN_S or abs(max(ys) - min(ys)) < MIN_DELTA:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den


def format_duration(hours: float) -> str:
    minutes = int(round(hours * 60))
    if minutes < 60:
        return f"{max(1, minutes)}m"
    h, m = divmod(minutes, 60)
    if h >= 48:
        return f"{round(hours / 24)}d"
    return f"{h}h {m:02d}m" if h < 10 else f"{h}h"


class History:
    def __init__(self, csv_path: Optional[Path] = None):
        self.csv_path = csv_path
        self.sessions: Dict[str, Deque[Sample]] = {}
        self.charging: Dict[str, bool] = {}
        self.last_level: Dict[str, int] = {}

    def record(self, r: Reading, now: Optional[float] = None) -> None:
        if not r.online or r.level is None:
            return
        now = time.time() if now is None else now
        if self.charging.get(r.key) != r.charging:
            self.sessions[r.key] = deque(maxlen=MAX_SAMPLES)   # new session
            self.charging[r.key] = r.charging
        session = self.sessions[r.key]
        if session and session[-1][1] == r.level:
            return
        session.append((now, r.level))
        if self.last_level.get(r.key) != r.level:
            self.last_level[r.key] = r.level
            self._write(now, r)

    def _write(self, now: float, r: Reading) -> None:
        if not self.csv_path:
            return
        try:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            if self.csv_path.exists() and self.csv_path.stat().st_size > MAX_CSV_BYTES:
                self.csv_path.replace(self.csv_path.with_suffix(".old.csv"))
            new = not self.csv_path.exists()
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "device", "level", "charging"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                            r.name, r.level, int(r.charging)])
        except OSError as e:
            log.debug("history write: %s", e)

    def estimate(self, r: Reading) -> Optional[str]:
        """"~6h 10m left" while discharging, "full in ~40m" while charging."""
        if not r.online or r.level is None:
            return None
        rate = slope_per_hour(list(self.sessions.get(r.key, ())))
        if rate is None:
            return None
        if r.charging and rate > 0 and r.level < 100:
            return f"full in ~{format_duration((100 - r.level) / rate)}"
        if not r.charging and rate < 0:
            return f"~{format_duration(r.level / -rate)} left"
        return None
