"""Time-remaining estimates ("~6h left", "full in ~40m").

Recent level changes are kept in memory per device - nothing is written to
disk. The estimate is a least-squares line through the current discharge (or
charge) session, which smooths out the jitter of voltage-based readings such as
the G PRO's.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .model import Reading

MIN_SPAN_S = 20 * 60          # need 20 minutes of data...
MIN_DELTA = 2                 # ...and a 2% change before guessing
MAX_SAMPLES = 256

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


class Estimator:
    def __init__(self):
        self.sessions: Dict[str, Deque[Sample]] = {}
        self.charging: Dict[str, bool] = {}

    def record(self, r: Reading, now: Optional[float] = None) -> None:
        if not r.online or r.level is None:
            return
        now = time.time() if now is None else now
        if self.charging.get(r.key) != r.charging:
            self.sessions[r.key] = deque(maxlen=MAX_SAMPLES)   # plugged in or out: new session
            self.charging[r.key] = r.charging
        session = self.sessions[r.key]
        if not session or session[-1][1] != r.level:
            session.append((now, r.level))

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
