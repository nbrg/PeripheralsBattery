"""The real settings UI in a real browser, against the real server with simulated
devices. Needs ``pip install playwright`` and a Chromium (``playwright install
chromium``, or PERIBATT_CHROMIUM pointing at one); skipped otherwise."""
import glob
import json
import os
import threading
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from peribatt import web
from peribatt.app import App
from peribatt.config import Store
from peribatt.model import HEADSET, KEYBOARD, MOUSE, Reading

from .fakes import FakeApi, QueueHandle
from .test_app import FakeBackend

if not (web.STATIC / "index.html").is_file():
    pytest.skip("web UI not built", allow_module_level=True)

SHOTS = Path(os.environ.get("PERIBATT_SCREENSHOTS", "")) if os.environ.get("PERIBATT_SCREENSHOTS") else None


def _chromium():
    exe = os.environ.get("PERIBATT_CHROMIUM")
    if not exe:
        found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
        exe = found[-1] if found else None
    return exe


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=_chromium())
        except Exception as e:                       # no browser on this machine
            pytest.skip(f"no Chromium: {e}")
        yield b
        b.close()


@pytest.fixture
def demo(tmp_path):
    """An app with the three devices from the README, and one unknown dongle."""
    app = App(Store(tmp_path / "settings.json"), FakeBackend())
    t0 = time.time() - 3 * 3600
    for i, level in enumerate((90, 86, 82, 78)):                  # ~8%/h -> an estimate
        app.estimator.record(Reading("logi-1", "PRO Wireless", MOUSE, level), now=t0 + i * 1800)
    app.clock = time.time
    app.apply([Reading("logi-1", "PRO Wireless", MOUSE, 76),
               Reading("hyperx-16ea", "HyperX Cloud Flight S", HEADSET, 28, charging=True),
               Reading("bt-k8", "Keychron K8 Pro", KEYBOARD, 15)])
    app.update(Reading("hyperx-16ea", "HyperX Cloud Flight S", HEADSET, 28, charging=True, muted=True))
    api = FakeApi()
    dongle = QueueHandle()
    api.add(0x1234, 0x0001, b"v", dongle, usage_page=0xFF00, product="Stealth Pro Dongle")
    api.infos[-1]["manufacturer_string"] = "Turtle Beach"
    saved = []
    ui = web.WebUi(app, api=api, recipes_path=tmp_path / "recipes.json", on_recipe_saved=saved.append,
                   opener=lambda u: None, set_autostart=lambda on: None, autostart_enabled=lambda: True)
    ui.start()
    yield app, ui, dongle, saved
    ui.stop()


def new_page(browser, color_scheme="dark"):
    # Playwright's own helpers use eval, which the app's Content-Security-Policy forbids.
    return browser.new_page(viewport={"width": 1000, "height": 720}, color_scheme=color_scheme,
                            bypass_csp=True)


def shot(page, name):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"))


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_devices_page(browser, demo, scheme):
    app, ui, _, _ = demo
    page = new_page(browser, color_scheme=scheme)
    page.goto(ui.url)
    cards = page.get_by_test_id("device-card")
    sync_api.expect(cards).to_have_count(3)
    sync_api.expect(page.get_by_text("Mic muted")).to_be_visible()
    sync_api.expect(page.get_by_text("Charging").first).to_be_visible()
    sync_api.expect(page.get_by_text("15%")).to_be_visible()
    # every device icon actually loaded (the <img> carries the token in its URL)
    page.wait_for_function("[...document.images].every(i => i.complete && i.naturalWidth > 0)")
    shot(page, f"devices-{scheme}")

    # rename through the menu and dialog
    page.get_by_role("button", name="Actions for Keychron K8 Pro").click()
    page.get_by_role("menuitem", name="Rename").click()
    page.get_by_label("Device name").fill("Office keyboard")
    page.get_by_role("button", name="Save").click()
    sync_api.expect(page.get_by_role("heading", name="Office keyboard")).to_be_visible()
    assert app.store["names"]["bt-k8"] == "Office keyboard"
    page.close()


def test_settings_pages_save_instantly(browser, demo):
    app, ui, _, _ = demo
    page = new_page(browser, color_scheme="dark")
    page.goto(ui.url + "#/general")
    sync_api.expect(page.get_by_role("heading", name="General")).to_be_visible()
    shot(page, "general")

    page.get_by_role("tab", name="Appearance").click()
    page.get_by_role("switch", name="Blink the headset icon while its mic is muted").click()
    sync_api.expect(page.locator(".fui-ToastTitle", has_text="Saved")).to_be_visible()
    assert app.store["flash_on_mute"] is False
    shot(page, "appearance")

    page.get_by_role("tab", name="Alerts & colours").click()
    preview = page.get_by_test_id("icon-preview")
    sync_api.expect(preview).to_be_visible()
    page.wait_for_function("[...document.images].every(i => i.complete && i.naturalWidth > 0)")
    shot(page, "alerts")
    # keyboard on the slider: move the red limit up by 5
    red = page.get_by_role("slider", name="Red below")
    red.focus()
    for _ in range(5):
        red.press("ArrowRight")
    sync_api.expect(red).to_have_value("25")
    deadline = time.time() + 5
    while app.store["low"] != 25 and time.time() < deadline:
        time.sleep(0.05)
    assert app.store["low"] == 25

    page.get_by_role("tab", name="Sources").click()
    shot(page, "sources")
    page.close()


def test_learn_wizard_in_the_browser(browser, demo):
    app, ui, dongle, saved = demo
    page = new_page(browser, color_scheme="dark")
    page.goto(ui.url.replace("?t=", "?fast=1&t=") + "#/learn")

    # the dongle: pushes a battery report while listening, and mute reports per phase
    stop = threading.Event()

    def headset():
        while not stop.is_set():
            cap = ui.learning.capture
            if cap is not None:
                ph = cap.phase
                if ph == "battery":
                    cap.feed(0, [0x0B, 0, 0xBB, 0x02, 0, 0, 0, 57])
                elif ph in ("unmuted", "muted", "unmuted2"):
                    cap.feed(0, [0x0B, 0, 0xBB, 0x08, int(ph == "muted"), 0, 0, 0])
            time.sleep(0.02)
    threading.Thread(target=headset, daemon=True).start()
    try:
        sync_api.expect(page.get_by_role("option", name="Stealth Pro Dongle")).to_be_visible()
        shot(page, "learn-pick")
        page.get_by_role("option", name="Stealth Pro Dongle").click()
        page.get_by_role("button", name="Next").click()

        page.get_by_label("Battery now").fill("57")
        page.get_by_role("button", name="Listen", exact=True).click()
        sync_api.expect(page.get_by_text("Found the battery level")).to_be_visible(timeout=15000)
        shot(page, "learn-battery")
        page.get_by_role("button", name="Next").click()

        page.get_by_role("button", name="Start").click()
        sync_api.expect(page.get_by_text("Found the mute signal")).to_be_visible(timeout=15000)
        page.get_by_role("button", name="Next").click()
        page.get_by_role("button", name="Skip").click()               # charging

        page.get_by_label("Name").fill("Stealth Pro")
        sync_api.expect(page.get_by_test_id("recipe")).to_contain_text('"name": "Stealth Pro"')
        shot(page, "learn-save")
        page.get_by_role("button", name="Save").click()
        sync_api.expect(page.get_by_text("Saved", exact=True)).to_be_visible()
    finally:
        stop.set()
    recipe = json.loads(ui.recipes_path.read_text())[0]
    assert recipe["name"] == "Stealth Pro" and recipe["kind"] == "headset"
    assert {r.get("muted", {}).get("byte") for r in recipe["listen"]} >= {4}
    assert saved
    end = time.time() + 5                              # its reader thread releases the handle
    while not dongle.closed and time.time() < end:
        time.sleep(0.02)
    assert dongle.closed
    page.close()


def test_the_page_keeps_the_server_alive(browser, demo):
    app, ui, _, _ = demo
    ui.idle_seconds = 1.5
    page = new_page(browser)
    page.goto(ui.url)
    time.sleep(4)                       # the page polls every 2 s
    assert ui.running
    page.close()


def test_mouse_dpi_and_polling_rate_in_the_browser(browser, tmp_path):
    from peribatt.hidpp import LogitechSource

    from .fakes import FakeClock, FakeHidppChannel, FakeLogiDevice
    mouse = FakeLogiDevice(settings=True)
    api = FakeApi()
    api.add(0x046D, 0xC539, b"long", FakeHidppChannel({1: mouse}), 0xFF00, 0x0002)
    app = App(Store(tmp_path / "settings.json"), FakeBackend(),
              sources=[LogitechSource(api=api, clock=FakeClock())])
    app.poll_once()
    ui = web.WebUi(app, api=FakeApi(), opener=lambda u: None)
    ui.start()
    try:
        page = new_page(browser)
        page.goto(ui.url)
        page.get_by_role("button", name="DPI & polling rate").click()
        panel = page.get_by_test_id("device-settings")
        sync_api.expect(panel).to_be_visible()
        sync_api.expect(page.get_by_label("Sensitivity value")).to_have_value("800 DPI")
        page.wait_for_timeout(400)                                           # dialog fade-in
        shot(page, "device-settings")

        page.get_by_role("button", name="1600", exact=True).click()           # a DPI preset
        sync_api.expect(page.locator(".fui-ToastTitle", has_text="Sensitivity saved")).to_be_visible()
        assert mouse.dpi == 1600

        page.get_by_role("combobox", name="Polling rate").click()
        page.get_by_role("option", name="500 Hz").click()
        sync_api.expect(page.locator(".fui-ToastTitle", has_text="Polling rate saved")).to_be_visible()
        assert mouse.rate == 2

        page.get_by_role("switch", name="Onboard profiles").click()
        sync_api.expect(page.locator(".fui-ToastTitle", has_text="Onboard profiles saved")).to_be_visible()
        assert mouse.mode == 2
        page.close()
    finally:
        ui.stop()
