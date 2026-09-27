"""The local settings server, over real HTTP."""
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import pytest

from peribatt import web
from peribatt.app import App
from peribatt.config import Store
from peribatt.model import HEADSET, MOUSE, Reading

from .fakes import FakeApi, QueueHandle
from .test_app import FakeBackend


class Client:
    def __init__(self, ui):
        self.ui = ui
        u = urlparse(ui.start())
        self.base = f"{u.scheme}://{u.netloc}"

    def request(self, method, path, body=None, token=True, headers=None, raw=None):
        h = {"X-Token": self.ui.token} if token else {}
        data = None
        if body is not None or raw is not None:
            data = raw if raw is not None else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def json(self, method, path, body=None):
        code, _, data = self.request(method, path, body)
        return code, json.loads(data)


@pytest.fixture
def app(tmp_path):
    a = App(Store(tmp_path / "settings.json"), FakeBackend())
    a.apply([Reading("logi-1", "PRO Wireless", MOUSE, 76),
             Reading("hx", "HyperX Cloud Flight S", HEADSET, 28, charging=True)])
    return a


@pytest.fixture
def client(app, tmp_path):
    saved = []
    ui = web.WebUi(app, api=FakeApi(), recipes_path=tmp_path / "recipes.json",
                   on_recipe_saved=saved.append, opener=lambda url: None,
                   set_autostart=lambda on: None, autostart_enabled=lambda: False)
    c = Client(ui)
    c.saved = saved
    yield c
    ui.stop()


def test_token_is_required_for_the_api(client):
    assert client.get("/api/state", token=False)[0] == 401
    assert client.get("/api/state", headers={"X-Token": "wrong"}, token=False)[0] == 401
    assert client.get("/api/state")[0] == 200
    # images may carry it in the URL (an <img> cannot send headers)
    assert client.get(f"/api/icon?key=logi-1&t={client.ui.token}", token=False)[0] == 200


def test_foreign_host_header_is_refused(client):
    """A web page that rebinds its own domain to 127.0.0.1 still sends its own Host."""
    code, _, _ = client.get("/api/state", headers={"Host": "evil.example:80"})
    assert code == 403


def test_posts_must_be_json(client):
    code, _, _ = client.request("POST", "/api/ping", raw=b"a=b",
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert code == 415


def test_oversized_body_is_refused(client):
    code, _, _ = client.request("POST", "/api/settings", raw=b"{" + b" " * (web.MAX_BODY + 10) + b"}")
    assert code == 400


def test_state_lists_devices_and_settings(client):
    code, st = client.json("GET", "/api/state")
    assert code == 200 and st["version"] and not st["standalone"]
    names = [d["name"] for d in st["devices"]]
    assert names == ["HyperX Cloud Flight S", "PRO Wireless"]
    hx = st["devices"][0]
    assert hx["charging"] and hx["level"] == 28 and hx["icon"].startswith("/api/icon?key=hx")
    assert st["settings"]["warn"] == 33 and st["settings"]["autostart"] is False
    assert st["choices"]["poll_seconds"][1] == [60, "1 minute"]
    assert st["limits"]["low"] == [1, 50]


def test_settings_are_validated_and_saved(client, app):
    code, res = client.json("POST", "/api/settings", {"warn": 10})
    assert code == 400 and "yellow limit" in res["errors"][0]
    code, res = client.json("POST", "/api/settings", {"warn": 45, "show_number": True})
    assert code == 200 and res["settings"]["warn"] == 45
    assert app.store["warn"] == 45 and app.store["show_number"] is True


def test_device_actions(client, app):
    rename = {"key": "logi-1", "action": "rename", "name": "Desk mouse"}
    assert client.json("POST", "/api/device", rename)[0] == 200
    assert app.effective(app.readings["logi-1"]).name == "Desk mouse"
    assert client.json("POST", "/api/device", {"key": "hx", "action": "hide"})[0] == 200
    assert "hx" in app.store["hidden"]
    code, res = client.json("POST", "/api/device", {"key": "hx", "action": "forget"})
    assert code == 400 and "disconnected" in res["error"]
    assert client.json("POST", "/api/device", {"key": "nope", "action": "hide"})[0] == 400
    assert client.json("POST", "/api/device", {"key": "hx", "action": "explode"})[0] == 400


def test_icons_are_pngs_of_the_real_renderer(client):
    code, headers, body = client.get("/api/icon?key=hx&size=48")
    assert code == 200 and headers["Content-Type"] == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"
    code, _, preview = client.get("/api/icon?kind=keyboard&level=15&low=20&warn=33&size=40")
    assert code == 200 and preview[:4] == b"\x89PNG"


def test_static_files_and_spa_fallback(client):
    code, headers, body = client.get("/", token=False)
    if code == 503:
        pytest.skip("web UI not built")
    assert code == 200 and b"<div id=\"root\">" in body
    assert "Content-Security-Policy" in headers
    assert client.get("/some/route", token=False)[0] == 200             # SPA route -> index.html
    assert client.get("/../settings.json", token=False)[2] == body      # no path traversal
    js = next(web.STATIC.glob("assets/*.js")).name
    code, headers, _ = client.get(f"/assets/{js}", token=False)
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert "immutable" in headers["Cache-Control"]


def test_learn_flow_over_http(client, app):
    dongle = QueueHandle()
    client.ui.api.add(0x1234, 0x0001, b"v", dongle, usage_page=0xFF00, product="Mystery Headset")
    client.ui.api.add(0x046D, 0xC539, b"l", QueueHandle(), usage_page=0xFF00, product="Receiver")
    client.ui.learning.is_supported = lambda v, p: v == 0x046D

    code, devices = client.json("GET", "/api/learn/devices?all=0")
    assert [d["name"] for d in devices] == ["Mystery Headset"]
    assert client.json("POST", "/api/learn/open", {"id": devices[0]["id"]})[1]["ok"]
    cap = client.ui.learning.capture

    client.json("POST", "/api/learn/phase", {"phase": "battery"})
    cap.feed(0, [0x0B, 0, 0xBB, 0x02, 0, 0, 0, 57])
    code, res = client.json("POST", "/api/learn/analyze", {"what": "level", "level": 57})
    assert res["found"] and "byte 7" in res["description"]

    for phase, value in (("unmuted", 0), ("muted", 1), ("unmuted2", 0)):
        client.json("POST", "/api/learn/phase", {"phase": phase})
        cap.feed(0, [0x0B, 0, 0xBB, 0x08, value])
    assert client.json("POST", "/api/learn/analyze", {"what": "muted"})[1]["found"]
    assert not client.json("POST", "/api/learn/analyze", {"what": "charging"})[1]["found"]

    code, res = client.json("POST", "/api/learn/recipe", {"name": "My Headset", "kind": "headset"})
    assert res["recipe"]["name"] == "My Headset" and len(res["recipe"]["listen"]) == 2
    code, res = client.json("POST", "/api/learn/save", {"name": "My Headset", "kind": "headset"})
    assert code == 200 and client.saved
    end = time.time() + 5                              # its reader thread releases the handle
    while not dongle.closed and time.time() < end:
        time.sleep(0.02)
    assert dongle.closed
    saved = json.loads(client.ui.recipes_path.read_text())
    assert saved[0]["product_ids"] == ["0x0001"]
    assert client.json("POST", "/api/learn/explode", {})[0] == 400


def test_query_probe_runs_in_background(client):
    dongle = QueueHandle()
    client.ui.api.add(0x1234, 0x0001, b"v", dongle, usage_page=0xFF00, product="X")
    devices = client.json("GET", "/api/learn/devices?all=1")[1]
    client.json("POST", "/api/learn/open", {"id": devices[0]["id"]})
    assert client.json("POST", "/api/learn/queries", {})[1]["ok"]
    end = time.time() + 5
    while not dongle.written and time.time() < end:
        time.sleep(0.05)
    assert dongle.written


def test_standalone_mode_only_offers_learning(tmp_path):
    ui = web.WebUi(None, api=FakeApi(), opener=lambda u: None)
    c = Client(ui)
    try:
        code, st = c.json("GET", "/api/state")
        assert st["standalone"] and st["devices"] == []
        assert c.json("POST", "/api/settings", {"warn": 40})[0] == 409
        assert c.json("GET", "/api/learn/devices?all=1")[0] == 200
    finally:
        ui.stop()


def test_server_stops_when_the_window_is_gone(app):
    ui = web.WebUi(app, api=FakeApi(), opener=lambda u: None, idle_seconds=0.6)
    opened = []
    ui.opener = opened.append
    url = ui.open("learn")
    assert opened == [url] and url.endswith("#/learn") and "?t=" in url
    assert ui.stopped.wait(5) and not ui.running
    ui.open()                                        # opening again starts a fresh server
    assert ui.running and ui.token
    ui.stop()


def test_open_window_prefers_an_app_window(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "find_edge", lambda: "C:/edge/msedge.exe")
    monkeypatch.setattr(web.subprocess, "Popen", lambda args, **kw: calls.append(args))
    web.open_window("http://127.0.0.1:1/?t=x")
    assert calls[0][0] == "C:/edge/msedge.exe" and calls[0][1] == "--app=http://127.0.0.1:1/?t=x"
    monkeypatch.setattr(web, "find_edge", lambda: None)
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", opened.append)
    web.open_window("http://x")
    assert opened == ["http://x"]


def test_concurrent_requests(client):
    results = []

    def hit():
        results.append(client.get("/api/state")[0])
    threads = [threading.Thread(target=hit) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 12
