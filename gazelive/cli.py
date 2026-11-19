"""gaze-live: the command line."""
from __future__ import annotations

import argparse
import getpass
import logging
import math
import platform
import sys
import threading
import time

from . import __version__
from .config import ENV_VAR, KEYS_PAGE, key_file, load_key, save_key, verify_key
from .stream import GRIDS, MODELS, GazeStream

log = logging.getLogger("gazelive")


def ask_for_key() -> str | None:
    """Read a key (prompted on a terminal, else one line of stdin), check it with the API and save it."""
    if sys.stdin.isatty():
        print(f"Make an API key at {KEYS_PAGE}: your profile picture, then API, then Create API key.", file=sys.stderr)
        key = getpass.getpass("Paste it here (it won't show): ").strip()
    else:
        key = sys.stdin.readline().strip()
    if not key:
        print("No key given.", file=sys.stderr)
        return None
    ok, note = verify_key(key)
    if ok is False:
        print(f"Not saved: {note}.", file=sys.stderr)
        return None
    path = save_key(key)
    print(f"Saved to {path}" + (f" ({note})." if ok else f". Couldn't check it: {note}."), file=sys.stderr)
    return key


def check(key: str, seconds: float, model: str, rate: int, grid: str) -> int:
    """Stream a synthetic screen and a circling pointer: proves the key and the connection, needs no permissions."""
    from .capture import FakeScreen, ScreenFeed
    W, H = 1440.0, 900.0
    stream = GazeStream(key, (W, H), model=model, rate=rate, grid=grid, recorded=("pointer",)).start()
    stop = threading.Event()

    def pointer():
        t0 = time.monotonic()
        while not stop.is_set():
            a = (time.monotonic() - t0) * 0.6
            stream.move(W * (0.5 + 0.35 * math.cos(a)), H * (0.5 + 0.35 * math.sin(a)))
            time.sleep(1 / 60)

    threading.Thread(target=pointer, daemon=True).start()
    ScreenFeed(lambda: stream, grabber=FakeScreen()).start()
    said, first, deadline = None, None, time.monotonic() + 300
    try:
        while True:
            time.sleep(1)
            if stream.status != said and stream.status != "Live":
                log.info("%s", stream.status)
                said = stream.status
            if stream.stopped_reason:
                return 1
            g = stream.gaze
            if g is not None:
                if first is None:
                    first = time.monotonic()
                    log.info("First map from %s", stream.model_version or model)
                maps, lag = stream.stats()
                where = f"gaze ({g.x:.0f}, {g.y:.0f})" if g.x is not None and g.y is not None else "gaze off screen"
                log.info("%.0f maps/s, %s, %s, on screen %.2f", maps,
                         "lag unknown" if lag is None else f"{lag:.0f} ms behind", where, g.on_screen)
                if time.monotonic() - first >= seconds:
                    log.info("OK: the key works and gaze is streaming.")
                    return 0
            elif time.monotonic() > deadline:
                log.error("No map within 5 minutes (last status: %s).", stream.status)
                return 1
    except KeyboardInterrupt:
        return 130
    finally:
        stop.set()
        stream.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gaze-live",
        description="A live heatmap of where you are looking, predicted by Orion's Gaze API from your screen, "
                    "pointer, clicks, scrolling and key presses. No webcam.")
    ap.add_argument("--model", choices=MODELS, default="gaze-live",
                    help="gaze-live reads only the past, as a realtime model must (default); gaze-base is the backfill model")
    ap.add_argument("--rate", type=int, default=10, metavar="N", help="maps a second, 1 to 20 (default 10)")
    ap.add_argument("--grid", choices=GRIDS, default="64x40", help="map resolution (default 64x40)")
    ap.add_argument("--print", dest="echo", action="store_true",
                    help="also print every gaze point to stdout as a JSON line: t, x, y, on_screen")
    ap.add_argument("--check", type=float, nargs="?", const=10.0, metavar="SECONDS",
                    help="test the key and the stream with a synthetic screen and pointer, then exit (no permissions "
                         "needed; the first map can take ~2 minutes)")
    ap.add_argument("--set-key", action="store_true", help=f"save your API key to {key_file()} and exit")
    ap.add_argument("--version", action="version", version=f"gaze-live {__version__}")
    a = ap.parse_args(argv)
    if not 1 <= a.rate <= 20:
        ap.error("--rate is 1 to 20")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)

    if a.set_key:
        return 0 if ask_for_key() else 1
    key = load_key()
    if not key:
        if not sys.stdin.isatty():
            print(f"No API key: run gaze-live --set-key, or set {ENV_VAR}.", file=sys.stderr)
            return 1
        key = ask_for_key()
        if not key:
            return 1
    if a.check is not None:
        return check(key, a.check, a.model, a.rate, a.grid)
    if sys.platform != "darwin":
        print("The heatmap app runs on macOS only; --check works anywhere.", file=sys.stderr)
        return 1
    if int((platform.mac_ver()[0] or "0").split(".")[0]) < 14:
        print("The heatmap app needs macOS 14 (Sonoma) or later; --check works anywhere.", file=sys.stderr)
        return 1
    from .app import run
    log.info("Gaze Live %s: the ○ Gaze menu-bar item stops, starts and quits. Ctrl-C quits.", __version__)
    run(key, model=a.model, rate=a.rate, grid=a.grid, echo=a.echo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
