"""Entry point.

    python -m peribatt            run the tray app
    python -m peribatt --once     print every device once and exit (--json for scripts)
    python -m peribatt --probe    diagnostics: HID devices, raw protocol replies
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from typing import List

from . import DISPLAY_NAME, __version__
from .config import Store, app_dir
from .model import Reading, merge

log = logging.getLogger("peribatt")

def setup_logging(to_file: bool, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = []
    if to_file:
        path = app_dir() / "peribatt.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=256_000, backupCount=1, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    install_crash_logging()


def install_crash_logging() -> None:
    """The packaged app has no console: without this, an error in any thread
    would vanish, and that thread (say, the one that creates the icons) with it."""
    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.critical("uncaught error in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def main_hook(exc_type, exc, tb):
        log.critical("uncaught error", exc_info=(exc_type, exc, tb))

    threading.excepthook = thread_hook
    sys.excepthook = main_hook


def collect(store: Store, settle: float = 1.5):
    """Poll every source once. Event-driven sources answer asynchronously, so
    they get ``settle`` seconds before their state is read back."""
    from .sources import build_sources
    sources = build_sources(store)
    results: List[Reading] = []
    for s in sources:
        try:
            results.extend(s.poll())
        except Exception as e:
            log.warning("%s: %s", getattr(s, "name", s), e)
    late = [s for s in sources if hasattr(s, "readings")]
    if late:
        time.sleep(settle)
        keys = set()
        for s in late:
            fresh = s.readings()
            keys |= {r.key for r in fresh}
            results = [r for r in results if r.key not in keys] + fresh
            if hasattr(s, "close"):
                s.close()
    return merge(results), sources


def cmd_once(as_json: bool) -> int:
    readings, _ = collect(Store.load())
    readings.sort(key=lambda r: r.name.lower())
    if as_json:
        print(json.dumps([asdict(r) for r in readings], indent=2))
        return 0
    if not readings:
        print("No devices with a readable battery were found.")
    from .app import describe
    for r in readings:
        print(describe(r))
    return 0


def cmd_probe() -> int:
    from . import diagnostics
    from .hidio import HidApi
    from .sources import supported_check
    readings, sources = collect(Store.load())
    text = diagnostics.report(api=HidApi(), sources=sources, readings=readings,
                              is_supported=supported_check(sources))
    if sys.stdout is None or not sys.stdout.isatty() and getattr(sys, "frozen", False):
        # the packaged app has no console: write the report and open it instead
        diagnostics.save_and_open(text)
    else:
        print(text)
    return 0


def run_tray() -> int:
    from . import winshell
    from .app import App
    from .devwatch import DeviceWatcher
    from .micmute import MicMuteWatcher
    from .model import HEADSET
    from .power import PowerWatcher
    from .sources import build_sources
    from .tray import PystrayBackend

    if not winshell.single_instance():
        log.info("already running")
        return 0
    store = Store.load()
    winshell.refresh_autostart()         # the app was moved: keep "Start with Windows" working
    first_launch = not store["welcomed"]
    if not store["first_run_done"]:
        # Start with Windows by default; the tray menu can switch it off again.
        try:
            winshell.set_autostart(True)
        except OSError as e:
            log.warning("autostart: %s", e)
        store["first_run_done"] = True
        store.save()
    backend = PystrayBackend(winshell.tray_icon_size())
    app = App(store, backend, light_taskbar=winshell.taskbar_is_light)
    backend.app = app
    app.sources = build_sources(store, on_change=app.update)

    def mic_wanted() -> bool:
        return store["windows_mute"] and any(
            r.kind == HEADSET and r.online for r in list(app.readings.values()))

    mic = MicMuteWatcher(app.set_windows_muted, wanted=mic_wanted)
    if mic.supported:
        app.mic_toggle = mic.toggle
        mic.start()

    def on_resume():
        app.resumed()
        mic.resync()

    power = PowerWatcher(on_resume)
    power.start()                        # no-op outside Windows; the poll loop also notices sleep
    devices = DeviceWatcher(app.devices_changed)
    devices.start()                      # plug/unplug: poll at once, and cache the HID device list
    from .updates import UpdateChecker
    app.update_checker = UpdateChecker(store, app.found_update)
    app.update_checker.start()           # once a day, if "Check for updates" is on

    wire_windows(app)
    app.open_diagnostics = lambda: threading.Thread(
        target=show_diagnostics, args=(app,), daemon=True, name="diagnostics").start()

    app.start()                          # an icon right away, before the first (slower) search
    if first_launch:
        welcome(app)

    for target, name in ((app.poll_loop, "poll"), (app.flash_loop, "flash")):
        threading.Thread(target=target, name=name, daemon=True).start()
    log.info("%s %s started", DISPLAY_NAME, __version__)
    try:
        app.stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        power.stop()
        devices.stop()
        app.update_checker.stop()
        app.stop()
        if getattr(app, "ui", None) is not None:
            app.ui.stop()
        mic.stop()
        for s in app.sources:
            if hasattr(s, "close"):
                s.close()
        for key in list(backend.icons):
            backend.remove(key)
        store.save()
    return 0


def welcome(app) -> None:
    """First launch: say where the icons are (Windows 11 hides new tray icons
    under the ^ arrow) and open the settings window so the app is visibly running."""
    from .app import PLACEHOLDER
    app.backend.notify(PLACEHOLDER, "Peripherals Battery is running",
                       "Your devices appear as icons next to the clock. If you can't see them, "
                       "click ^ by the clock and drag them onto the taskbar.")
    app.store["welcomed"] = True
    app.store.save()
    if app.open_settings:
        app.open_settings()


def show_diagnostics(app) -> None:
    from . import diagnostics
    from .sources import supported_check
    try:
        text = diagnostics.report(app, is_supported=supported_check(app.sources))
        path = diagnostics.save_and_open(text)
        log.info("diagnostics written to %s", path)
    except Exception:
        log.exception("diagnostics failed")


def wire_windows(app) -> None:
    """Settings window and learn wizard: a local web UI, started on demand."""
    from urllib.parse import quote

    from . import winshell
    from .sources import reload_recipes, supported_check
    from .web import WebUi

    def saved(_recipe):
        reload_recipes(app.sources)
        app.refresh_event.set()

    ui = WebUi(app, is_supported=supported_check(app.sources), on_recipe_saved=saved,
               set_autostart=winshell.set_autostart, autostart_enabled=winshell.autostart_enabled)
    app.ui = ui
    app.open_settings = lambda: ui.open()
    app.open_learn = lambda: ui.open("learn")
    app.open_rename = lambda key: ui.open(f"devices/rename/{quote(key, safe='')}")


def cmd_learn() -> int:
    """The wizard on its own: produces a recipe without the tray app running."""
    from .sources import build_sources, supported_check
    from .web import WebUi
    ui = WebUi(None, is_supported=supported_check(build_sources(Store.load())))
    print(f"Opening {ui.open('learn')}")
    ui.stopped.wait()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="peribatt",
                                description=f"{DISPLAY_NAME} - wireless gear battery in the tray")
    p.add_argument("--once", action="store_true", help="print all devices once and exit")
    p.add_argument("--json", action="store_true", help="with --once: JSON output")
    p.add_argument("--probe", action="store_true", help="diagnostics report")
    p.add_argument("--learn", action="store_true", help="teach the app an unsupported device")
    p.add_argument("--autostart", choices=("on", "off"), help="start with Windows (or not)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    if args.autostart:
        from . import winshell
        try:
            winshell.set_autostart(args.autostart == "on")
        except winshell.TemporaryFolder as e:
            print(e, file=sys.stderr)
            return 1
        store = Store.load()                  # the installer decided: don't override at first launch
        store["first_run_done"] = True
        store.save()
        print(f"Start with Windows: {args.autostart}")
        return 0
    if args.learn:
        setup_logging(to_file=False, verbose=args.verbose)
        return cmd_learn()
    if args.once or args.probe:
        setup_logging(to_file=False, verbose=args.verbose)
        return cmd_probe() if args.probe else cmd_once(args.json)
    setup_logging(to_file=True, verbose=args.verbose)
    return run_tray()


if __name__ == "__main__":
    sys.exit(main())
