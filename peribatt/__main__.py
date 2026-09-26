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

INTERESTING_VIDS = {0x046D: "Logitech", 0x0951: "HyperX (Kingston)", 0x03F0: "HyperX (HP)",
                    0x1532: "Razer", 0x1038: "SteelSeries", 0x1B1C: "Corsair",
                    0x10F5: "Turtle Beach", 0x1E7D: "Roccat/Turtle Beach", 0x3329: "Audeze"}


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
    from .hidio import HidApi
    api = HidApi()
    print(f"{DISPLAY_NAME} {__version__} on {sys.platform}, Python {sys.version.split()[0]}\n")
    print("HID collections of known gaming brands:")
    for vid, brand in INTERESTING_VIDS.items():
        for d in api.enumerate(vid):
            print(f"  {brand:18} {vid:04x}:{d['product_id']:04x} if={d.get('interface_number')}"
                  f" usage={d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}"
                  f" '{d.get('product_string') or ''}'")
    readings, sources = collect(Store.load())
    print("\nProviders:")
    for s in sources:
        inner = getattr(s, "source", s)
        lines = getattr(inner, "log", [])
        print(f"  [{getattr(s, 'name', s)}]" + ("" if lines else " (nothing to report)"))
        for line in lines:
            print(f"    {line}")
    print("\nReadings:")
    for r in readings:
        print(f"  {r}")
    print("\nPaste this report into an issue to get a device supported.")
    return 0


def run_tray() -> int:
    from . import winshell
    from .app import App
    from .micmute import MicMuteWatcher
    from .model import HEADSET
    from .power import PowerWatcher
    from .sources import build_sources
    from .tray import PystrayBackend

    if not winshell.single_instance():
        log.info("already running")
        return 0
    store = Store.load()
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

    wire_windows(app)

    for target, name in ((app.poll_loop, "poll"), (app.flash_loop, "flash")):
        threading.Thread(target=target, name=name, daemon=True).start()
    log.info("%s %s started", DISPLAY_NAME, __version__)
    try:
        app.stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        power.stop()
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


def wire_windows(app) -> None:
    """Settings window and learn wizard: a local web UI, started on demand."""
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
        winshell.set_autostart(args.autostart == "on")
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
