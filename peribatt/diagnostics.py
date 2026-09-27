"""A plain-text report of what the app sees, for "why isn't my device showing?".

It lists every HID device Windows exposes (not just known brands), what each
provider tried and got back, the current readings, and the end of the log. The
tray menu and ``--probe`` write it to ``diagnostics.txt`` and open it, because
the packaged app has no console to print to.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

from . import DISPLAY_NAME, __version__
from .config import app_dir

LOG_TAIL = 120


def _inner(source):
    while hasattr(source, "source"):
        source = source.source
    return source


def hid_section(api, is_supported=lambda v, p: False) -> List[str]:
    out = ["HID devices (all of them):"]
    err = getattr(api, "error", "")
    if err:
        return out + [f"  hidapi could not be loaded: {err}"]
    devices = api.enumerate(0)
    if not devices:
        out.append("  none found")
    seen = set()
    for d in sorted(devices, key=lambda d: (d.get("vendor_id", 0), d.get("product_id", 0),
                                             d.get("interface_number", 0), d.get("usage_page", 0))):
        vid, pid = d.get("vendor_id", 0), d.get("product_id", 0)
        if (vid, pid) not in seen:
            seen.add((vid, pid))
            tag = "  [supported]" if is_supported(vid, pid) else ""
            out.append(f"  {vid:04x}:{pid:04x} {(d.get('manufacturer_string') or '').strip()!s} "
                       f"'{(d.get('product_string') or '').strip()}'{tag}")
        out.append(f"      if={d.get('interface_number')} usage={d.get('usage_page', 0):04x}:"
                   f"{d.get('usage', 0):04x}")
    return out


def sources_section(sources: Iterable, status: Optional[dict] = None) -> List[str]:
    out = ["Providers:"]
    for s in sources:
        name = getattr(s, "name", type(s).__name__)
        st = (status or {}).get(name)
        summary = f"  [{name}]"
        if st:
            summary += f" last poll: {st['count']} device(s) in {st['seconds']:.2f} s"
            if st.get("error"):
                summary += f", error: {st['error']}"
        out.append(summary)
        for line in getattr(_inner(s), "log", []) or []:
            out.append(f"      {line}")
    return out


def log_tail(path: Path, lines: int = LOG_TAIL) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ["(no log file yet)"]
    return text[-lines:]


def report(app=None, api=None, sources=None, readings=None, is_supported=lambda v, p: False) -> str:
    from .hidio import HidApi
    api = api or HidApi()
    how = "packaged" if getattr(sys, "frozen", False) else "from source"
    lines = [f"{DISPLAY_NAME} {__version__} diagnostics - {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"{platform.platform()}, Python {platform.python_version()} "
             f"({platform.architecture()[0]}), {how}",
             f"Data folder: {app_dir()}", ""]
    lines += hid_section(api, is_supported) + [""]
    if app is not None:
        sources = app.sources
        readings = list(app.readings.values())
        status = app.source_status
    else:
        status = None
    lines += sources_section(sources or [], status) + [""]
    lines.append("Readings:")
    for r in readings or []:
        lines.append(f"  {r}")
    if not readings:
        lines.append("  none")
    if app is not None:
        lines += ["", "Tray icons shown: " + (", ".join(app.shown) or "none"),
                  "Settings: " + ", ".join(f"{k}={v}" for k, v in sorted(app.store.settings.items())
                                           if k not in ("names",))]
    lines += ["", f"End of the log ({app_dir() / 'peribatt.log'}):"]
    lines += [f"  {line}" for line in log_tail(app_dir() / "peribatt.log")]
    lines += ["", "Paste this into an issue on GitHub to get help. It contains device names and ids,",
              "no personal data."]
    return "\n".join(lines) + "\n"


def save_and_open(text: str) -> Path:
    path = app_dir() / "diagnostics.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opens our own report in the default text editor
    elif os.environ.get("DISPLAY"):
        subprocess.Popen(["xdg-open", str(path)])
    return path
