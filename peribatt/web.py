"""The settings window, served as a local web app.

The UI is a React + shadcn/ui single-page app, pre-built into ``webui/``
(see ``web/`` for its source). This module serves it plus a small JSON API on
127.0.0.1 and opens it in a chromeless Edge window (``msedge --app=``), which
every Windows 10/11 machine has; elsewhere the default browser is used.

It costs nothing while closed: the server starts when the window is opened and
stops itself once the page has stopped polling for ``IDLE_SECONDS``.

Security: the server only listens on loopback, every API call must carry the
random per-session token, and the Host header must be the loopback address
(which defeats DNS-rebinding pages in the user's normal browser).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, learn, prefs, style
from .config import app_dir
from .controls import Unavailable
from .hidio import HidApi
from .model import DEVICE, Reading
from .render import render

log = logging.getLogger("peribatt")

STATIC = Path(__file__).with_name("webui")
IDLE_SECONDS = 45
MAX_BODY = 64 * 1024
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'")
# Fixed types: on Windows, mimetypes reads the registry, where .js is sometimes
# "text/plain" - and browsers refuse to run a module script served like that.
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon", ".woff2": "font/woff2", ".json": "application/json"}
NOT_BUILT = b"The settings UI is not built. Run: cd web && npm ci && npm run build"


# --- helpers ------------------------------------------------------------------

def png(image) -> bytes:
    buf = BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def find_edge() -> Optional[str]:
    if sys.platform != "win32":
        return None
    candidates = [Path(os.environ.get(v, "")) / "Microsoft/Edge/Application/msedge.exe"
                  for v in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("msedge")


def open_window(url: str) -> None:
    """A chromeless app window where possible, the default browser otherwise."""
    edge = find_edge()
    if edge:
        flags = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
        subprocess.Popen([edge, f"--app={url}", "--window-size=1000,720",
                          "--no-first-run", "--disable-features=Translate"], creationflags=flags)
        return
    import webbrowser
    webbrowser.open(url)


class LearnSession:
    """The wizard's server-side state: one capture at a time."""

    def __init__(self, api, is_supported: Callable[[int, int], bool]):
        self.api = api
        self.is_supported = is_supported
        self.devices: list = []
        self.device: Optional[dict] = None
        self.capture: Optional[learn.Capture] = None
        self.findings = learn.Findings()

    def list(self, show_all: bool) -> list:
        self.devices = learn.device_list(self.api, self.is_supported)
        return [{"id": i, "name": d["name"], "maker": d["maker"],
                 "vid": f"{d['vendor_id']:04x}", "pid": f"{d['product_id']:04x}",
                 "supported": d["supported"], "collections": len(d["infos"])}
                for i, d in enumerate(self.devices) if show_all or not d["supported"]]

    def open(self, index: int) -> int:
        self.close()
        self.device = self.devices[index]
        self.capture = learn.Capture(self.api, self.device["infos"])
        self.findings = learn.Findings()
        return self.capture.open()

    def close(self) -> None:
        if self.capture:
            self.capture.close()
        self.capture = None

    def analyze(self, what: str, level: Optional[int]) -> dict:
        cap = self.capture
        if cap is None:
            raise ValueError("no device open")
        if what == "level":
            reports = cap.in_phase("battery")
            if level is None:
                return {"found": False, "reports": len(reports), "description": "no level given"}
            cands = learn.level_candidates(reports, level)
            self.findings.level = cands[0] if cands else None
            found = self.findings.level
        elif what == "muted":
            reports = cap.in_phase("unmuted", "unmuted2", "muted")
            cands = learn.toggle_candidates(cap.in_phase("unmuted", "unmuted2"), cap.in_phase("muted"))
            self.findings.muted = found = cands[0] if cands else None
        elif what == "charging":
            reports = cap.in_phase("unplugged", "plugged")
            cands = learn.toggle_candidates(cap.in_phase("unplugged"), cap.in_phase("plugged"))
            self.findings.charging = found = cands[0] if cands else None
        else:
            raise ValueError(f"unknown step {what!r}")
        return {"found": found is not None, "reports": len(reports),
                "description": learn.describe(found)}

    def recipe(self, name: str, kind: str) -> dict:
        d = self.device
        if d is None:
            raise ValueError("no device open")
        return learn.build_recipe(name.strip() or d["name"], kind or DEVICE,
                                  d["vendor_id"], d["product_id"], self.findings)


# --- server ---------------------------------------------------------------------

class WebUi:
    """Starts on demand, stops when idle. ``app`` may be None (``--learn`` alone)."""

    def __init__(self, app=None, api=None, is_supported: Callable[[int, int], bool] = lambda v, p: False,
                 on_recipe_saved: Optional[Callable[[dict], None]] = None,
                 recipes_path: Optional[Path] = None, opener: Optional[Callable[[str], None]] = None,
                 idle_seconds: float = IDLE_SECONDS, set_autostart=None, autostart_enabled=None):
        self.app = app
        self.api = api or HidApi()
        self.is_supported = is_supported
        self.on_recipe_saved = on_recipe_saved
        self.recipes_path = recipes_path or (app_dir() / "recipes.json")
        self.opener = opener or (lambda url: open_window(url))   # looked up when used
        self.idle_seconds = idle_seconds
        self.set_autostart = set_autostart
        self.autostart_enabled = autostart_enabled
        self.token = ""
        self.server: Optional[ThreadingHTTPServer] = None
        self.learning = LearnSession(self.api, is_supported)
        self.last_seen = 0.0
        self._lock = threading.Lock()
        self.stopped = threading.Event()

    # -- lifecycle ---------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.server is not None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/?t={self.token}"

    def start(self) -> str:
        with self._lock:
            if self.server is None:
                self.token = secrets.token_urlsafe(24)
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
                self.server.daemon_threads = True
                self.last_seen = time.monotonic()
                self.stopped.clear()
                threading.Thread(target=self.server.serve_forever, name="web", daemon=True).start()
                threading.Thread(target=self._watchdog, args=(self.server,), name="web-idle",
                                 daemon=True).start()
            # Opening (again) counts as activity: the server must not idle out just as
            # a new window is about to load.
            self.last_seen = time.monotonic()
            return self.url

    def open(self, page: str = "") -> str:
        url = self.start() + (f"#/{page}" if page else "")
        self.opener(url)
        return url

    def stop(self) -> None:
        with self._lock:
            server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        self.learning.close()
        self.stopped.set()

    def _watchdog(self, server) -> None:
        while self.server is server:
            time.sleep(min(5.0, self.idle_seconds / 3))
            if time.monotonic() - self.last_seen > self.idle_seconds:
                log.info("settings window closed; stopping the local server")
                self.stop()
                return

    # -- API -----------------------------------------------------------------------
    def state(self) -> dict:
        app = self.app
        out: dict = {"version": __version__, "platform": sys.platform, "standalone": app is None,
                     "choices": {o.key: [list(c) for c in o.options] for o in prefs.options()
                                 if isinstance(o, prefs.Choice)},
                     "limits": {o.key: [o.lo, o.hi] for o in prefs.options() if isinstance(o, prefs.Number)},
                     "devices": [], "settings": {}}
        if app is None:
            return out
        kw = {"autostart_enabled": self.autostart_enabled} if self.autostart_enabled else {}
        out["settings"] = prefs.current(app.store, **kw)
        with app.lock:
            items = list(app.readings.items())
        for key, r in items:
            eff = app.effective(r)
            out["devices"].append({
                "key": key, "name": eff.name, "original": r.name, "kind": r.kind,
                "level": r.level, "charging": r.charging, "online": r.online, "muted": eff.muted,
                "note": r.note, "hidden": key in app.store["hidden"],
                "configurable": app.has_controls(key),
                "estimate": app.estimator.estimate(eff),
                "icon": f"/api/icon?key={key}&v={r.level}-{int(r.charging)}-{int(r.online)}",
            })
        out["devices"].sort(key=lambda d: (not d["online"], d["name"].lower()))
        return out

    def save_settings(self, form: dict) -> dict:
        # The UI saves one setting at a time: check limits against the current values.
        if "low" in form or "warn" in form:
            form = {"low": self.app.store["low"], "warn": self.app.store["warn"], **form}
        clean, errors = prefs.validate(form)
        if errors:
            return {"ok": False, "errors": errors}
        kw = {"set_autostart": self.set_autostart} if self.set_autostart else {}
        prefs.apply(self.app, clean, **kw)
        return {"ok": True, "settings": self.state()["settings"]}

    def device_controls(self, key: str) -> dict:
        return {"controls": [c.to_json() for c in self.app.device_controls(key)]}

    def set_device_control(self, body: dict) -> dict:
        key, control_id = str(body.get("key", "")), str(body.get("id", ""))
        if "value" not in body:
            raise ValueError("no value")
        after = self.app.set_device_control(key, control_id, body["value"])
        return {"ok": True, "controls": [c.to_json() for c in after]}

    def device_action(self, body: dict) -> dict:
        app, key, action = self.app, body.get("key", ""), body.get("action")
        if key not in app.readings:
            return {"ok": False, "error": "unknown device"}
        if action == "rename":
            app.rename(key, str(body.get("name", ""))[:60])
        elif action in ("hide", "show"):
            prefs.set_hidden(app, key, action == "hide")
        elif action == "forget":
            err = prefs.forget(app, key)
            if err:
                return {"ok": False, "error": err}
        else:
            return {"ok": False, "error": "unknown action"}
        return {"ok": True}

    def icon(self, query: dict) -> bytes:
        size = max(16, min(128, int(query.get("size", ["64"])[0])))
        key = query.get("key", [""])[0]
        if self.app is not None and key in self.app.readings:
            r = self.app.effective(self.app.readings[key])
        else:
            r = Reading("preview", "", query.get("kind", [DEVICE])[0],
                        _opt_int(query.get("level", [""])[0]),
                        charging=query.get("charging", ["0"])[0] == "1",
                        online=query.get("online", ["1"])[0] == "1")
        low, warn = self._thresholds(query)
        light = query.get("light", ["0"])[0] == "1"          # drawn for the page's theme
        colour = style.border_colour(r, low, warn, light_taskbar=light)
        return png(render(size, r.kind, r.level, colour, online=r.online, charging=r.charging,
                          show_number=query.get("number", ["0"])[0] == "1", light_taskbar=light))

    def _thresholds(self, query: dict):
        s = self.app.store if self.app else {"low": style.DEFAULT_LOW, "warn": style.DEFAULT_WARN}
        low = _opt_int(query.get("low", [""])[0]) or s["low"]
        warn = _opt_int(query.get("warn", [""])[0]) or s["warn"]
        return low, warn

    def learn_call(self, action: str, body: dict):
        s = self.learning
        if action == "devices":
            return s.list(bool(body.get("all")))
        if action == "open":
            n = s.open(int(body["id"]))
            return {"ok": n > 0, "collections": n}
        if action == "phase":
            if s.capture:
                s.capture.set_phase(str(body.get("phase", "idle")))
            return {"ok": s.capture is not None}
        if action == "queries":
            if s.capture:
                threading.Thread(target=s.capture.send_known_queries, daemon=True).start()
            return {"ok": s.capture is not None}
        if action == "analyze":
            return s.analyze(str(body.get("what")), _opt_int(body.get("level")))
        if action == "recipe":
            return {"recipe": s.recipe(str(body.get("name", "")), str(body.get("kind", DEVICE)))}
        if action == "save":
            recipe = s.recipe(str(body.get("name", "")), str(body.get("kind", DEVICE)))
            learn.save_recipe(recipe, self.recipes_path)
            s.close()
            if self.on_recipe_saved:
                self.on_recipe_saved(recipe)
            return {"ok": True, "recipe": recipe, "path": str(self.recipes_path)}
        if action == "close":
            s.close()
            return {"ok": True}
        raise ValueError(f"unknown learn action {action!r}")


def _opt_int(v) -> Optional[int]:
    try:
        return int(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _handler(ui: WebUi):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PeripheralsBattery"
        sys_version = ""

        def log_message(self, fmt, *args):          # keep the log file quiet
            log.debug("web: " + fmt, *args)

        # -- plumbing --------------------------------------------------------------
        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _authorised(self) -> bool:
            given = self.headers.get("X-Token", "")
            if not given and self.command == "GET":
                # <img> tags cannot send headers: images may carry the token in the URL
                given = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            return bool(ui.token) and secrets.compare_digest(given, ui.token)

        def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "max-age=31536000, immutable" if cache else "no-store")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _guard(self) -> bool:
            if not self._host_ok():
                self._send(403, b"forbidden", "text/plain")
                return False
            if self.path.startswith("/api/"):
                if not self._authorised():
                    self._json(401, {"error": "bad token"})
                    return False
                ui.last_seen = time.monotonic()
            return True

        # -- routes ------------------------------------------------------------------
        def do_GET(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/api/state":
                    return self._json(200, ui.state())
                if url.path == "/api/icon":
                    return self._send(200, ui.icon(q), "image/png")
                if url.path == "/api/controls":
                    if ui.app is None:
                        return self._json(409, {"error": "the tray app is not running"})
                    try:
                        return self._json(200, ui.device_controls(q.get("key", [""])[0]))
                    except Unavailable as e:
                        return self._json(409, {"error": str(e)})
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                if url.path == "/api/learn/devices":
                    return self._json(200, ui.learn_call("devices", {"all": q.get("all", ["0"])[0] == "1"}))
                if url.path.startswith("/api/"):
                    return self._json(404, {"error": "not found"})
                return self._static(url.path)
            except Exception as e:
                log.exception("web GET %s", url.path)
                return self._json(500, {"error": str(e)})

        def do_POST(self):
            if not self._guard():
                return
            path = urlparse(self.path).path
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(415, {"error": "JSON only"})
            try:
                body = self._body()
                if path == "/api/ping":
                    return self._json(200, {"ok": True})
                if ui.app is None and path in ("/api/settings", "/api/device", "/api/control"):
                    return self._json(409, {"error": "the tray app is not running"})
                if path == "/api/settings":
                    res = ui.save_settings(body)
                    return self._json(200 if res["ok"] else 400, res)
                if path == "/api/control":
                    try:
                        return self._json(200, ui.set_device_control(body))
                    except Unavailable as e:
                        return self._json(409, {"error": str(e)})
                if path == "/api/device":
                    res = ui.device_action(body)
                    return self._json(200 if res["ok"] else 400, res)
                if path.startswith("/api/learn/"):
                    return self._json(200, ui.learn_call(path.rsplit("/", 1)[1], body))
                return self._json(404, {"error": "not found"})
            except (ValueError, KeyError, IndexError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                log.exception("web POST %s", path)
                return self._json(500, {"error": str(e)})

        def _static(self, path: str):
            rel = path.lstrip("/") or "index.html"
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                target = STATIC / "index.html"               # single-page app routes
            if not target.is_file():
                return self._send(503, NOT_BUILT, "text/plain")
            ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            return self._send(200, target.read_bytes(), ctype, cache="/assets/" in path)

    return Handler
