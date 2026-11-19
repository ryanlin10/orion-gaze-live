"""A client for Orion's realtime Gaze API, wss://api.paradigmmachines.ai/v1/stream.

Screen pictures and input go up as they happen; gaze maps come back `rate` times a second. No message carries a time:
the API times each one when it arrives (the stream runs on the server's clock). Reference:
https://app.paradigmmachines.ai/docs/gaze-api

    stream = GazeStream(key, screen=(1440, 900))
    stream.start()
    stream.picture(jpeg_bytes)        # whenever the screen changes
    stream.move(300, 200)             # pointer, in the same points as `screen`
    stream.gaze                       # the newest Gaze, or None
    stream.close()

All input methods are thread-safe and never block. The connection runs on its own thread, reconnects by itself and
stops for good only when the API refuses the key or the account's minutes for the day are used up (`stopped_reason`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .config import USER_AGENT, tls

log = logging.getLogger("gazelive")

API_URL = "wss://api.paradigmmachines.ai/v1/stream"
MODELS = ("gaze-live", "gaze-base")
GRIDS = {"64x40": (64, 40), "32x20": (32, 20)}
STATES = {
    "waiting_for_gpu": "Waiting for a GPU (about 2 minutes when none is running)",
    "starting": "A GPU is starting",
    "live": "Loading the model",
    "waiting_for_picture": "Waiting for the first screen picture",
}
PICTURE_EVERY_S = 1.0   # the API reads about one new picture a second, always the newest: sending more only costs upload
KEEPALIVE_S = 45        # the API ends a stream after 2 minutes with nothing received
PING_EVERY_S = 5        # pongs carry the server's clock, for the lag shown
SEND_EVERY_S = 0.02
MAX_HELD_EVENTS = 5000


@dataclass(frozen=True)
class Gaze:
    map: np.ndarray       # [rows, cols]: the chance gaze is in each cell, row by row from the top left; sums to on_screen
    on_screen: float      # the chance the person is looking at the screen at all
    x: float | None       # centre of the most likely cell, in screen points
    y: float | None
    t: float | None       # server time of the map, ms
    received: float       # time.monotonic() when it arrived


class ApiError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status, self.message = status, message

    @property
    def final(self) -> bool:
        """No point retrying: a refused key, or the day's minutes used up."""
        return self.status in (401, 403) or "for today" in self.message


def parse_gaze(msg: dict, cols: int, rows: int, received: float | None = None) -> Gaze | None:
    """A 'gaze' message -> Gaze, or None when it carries no map of the expected size."""
    try:
        m = np.asarray(msg["map"], np.float32)
    except (KeyError, TypeError, ValueError):
        return None
    if m.size != cols * rows:
        return None
    m = m.reshape(rows, cols)
    num = lambda k: float(msg[k]) if isinstance(msg.get(k), (int, float)) else None
    on = num("on_screen")
    return Gaze(map=m, on_screen=float(m.sum()) if on is None else on, x=num("x"), y=num("y"), t=num("t"),
                received=time.monotonic() if received is None else received)


def _now_ms() -> float:
    return time.monotonic_ns() / 1e6


class GazeStream:
    def __init__(self, key: str, screen: tuple[float, float], model: str = "gaze-live", rate: int = 10,
                 grid: str = "64x40", recorded: tuple[str, ...] = ("pointer",), url: str = API_URL):
        if not key:
            raise ValueError("an API key is needed")
        if model not in MODELS:
            raise ValueError(f"model must be one of {', '.join(MODELS)}")
        if grid not in GRIDS:
            raise ValueError(f"grid must be one of {', '.join(GRIDS)}")
        if not 1 <= int(rate) <= 20:
            raise ValueError("rate is 1 to 20 maps a second")
        self._api_key, self.model, self.rate, self.grid = key, model, int(rate), grid
        self.cols, self.rows = GRIDS[grid]
        self.screen = (round(screen[0]), round(screen[1]))
        self.recorded = [k for k in ("pointer", "keys", "scroll") if k in recorded]
        self.url = f"{url.rstrip('/')}?model={model}"
        self.on_gaze: Callable[[Gaze], None] | None = None   # called on the network thread for every map

        self.gaze: Gaze | None = None
        self.status = "Connecting"
        self.model_version: str | None = None
        self.stopped_reason: str | None = None

        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._picture: bytes | None = None     # newest picture not sent yet
        self._latest: bytes | None = None      # newest picture of all: a new connection starts with it
        self._taking = False                   # input is kept only while a GPU has the stream
        self._stop, self._done = threading.Event(), threading.Event()
        self._recv: deque[tuple[float, float | None]] = deque(maxlen=256)
        self._offset: float | None = None      # server clock = this clock + offset
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ input, from any thread

    def move(self, x: float, y: float):
        self._event({"type": "move", "x": round(x, 1), "y": round(y, 1)})

    def down(self, x: float, y: float):
        self._event({"type": "down", "x": round(x, 1), "y": round(y, 1)})

    def up(self, x: float, y: float):
        self._event({"type": "up", "x": round(x, 1), "y": round(y, 1)})

    def key(self):
        self._event({"type": "key"})          # that a key was pressed; never which

    def scroll(self, y: float):
        self._event({"type": "scroll", "y": round(y, 1)})   # scroll position, growing as the page goes down

    def picture(self, jpeg: bytes):
        with self._lock:
            self._picture = self._latest = jpeg

    def _event(self, e: dict):
        with self._lock:
            # Input waits for a GPU: what the API holds meanwhile reaches the model all at one moment.
            if self._taking and len(self._events) < MAX_HELD_EVENTS:
                self._events.append(e)

    # ------------------------------------------------------------ control

    def start(self) -> GazeStream:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="gaze-stream")
            self._thread.start()
        return self

    def close(self, timeout: float = 3.0):
        """End the stream (so it stops counting toward the account's streams at once) and wait for that."""
        self._stop.set()
        if self._thread is not None:
            self._done.wait(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._done.is_set()

    def stats(self) -> tuple[float, float | None]:
        """(maps a second, median lag in ms) over the last 2 seconds. Lag = the map's age when it arrived."""
        cut = time.monotonic() - 2
        recent = [lag for t, lag in list(self._recv) if t >= cut]
        lags = [lag for lag in recent if lag is not None]
        return len(recent) / 2, (float(np.median(lags)) if lags else None)

    # ------------------------------------------------------------ network thread

    def _run(self):
        try:
            asyncio.run(self._forever())
        except Exception:  # noqa: BLE001
            log.exception("gaze stream thread failed")
            self.status = "Stopped: internal error (see the log)"
        finally:
            self._done.set()

    async def _forever(self):
        delay = 1.0
        while not self._stop.is_set():
            got_maps = False
            try:
                got_maps = await self._session()
            except ApiError as e:
                if e.final:
                    self.stopped_reason = e.message
                    self.status = f"Stopped: {e.message}"
                    log.error("%s", self.status)
                    return
                self.status = f"Reconnecting: {e.message}"
            except Exception as e:  # noqa: BLE001
                self.status = f"Reconnecting ({type(e).__name__}: {str(e)[:100]})"
            finally:
                with self._lock:
                    self._taking = False
                    self._events.clear()
                self.gaze = None
            if self._stop.is_set():
                break
            log.info("%s", self.status)
            delay = 1.0 if got_maps else min(delay * 2, 30.0)
            end = time.monotonic() + delay
            while time.monotonic() < end and not self._stop.is_set():
                await asyncio.sleep(0.1)
        self.status = "Stopped"

    async def _session(self) -> bool:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        self.status, self._offset = "Connecting", None
        secure = {"ssl": tls()} if self.url.startswith("wss:") else {}
        try:
            ws = await connect(self.url, additional_headers={"X-API-Key": self._api_key}, user_agent_header=USER_AGENT,
                               max_size=2 ** 24, compression=None, open_timeout=20, ping_interval=20, ping_timeout=30,
                               **secure)
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403):
                raise ApiError(code, "the API refused this key (revoked or mistyped)") from None
            raise ApiError(code, _http_message(e.response)) from None
        got_maps = False
        try:
            try:
                await ws.send(json.dumps({"type": "start", "rate": self.rate, "grid": self.grid,
                                          "screen": {"width": self.screen[0], "height": self.screen[1]},
                                          "recorded": self.recorded}))
            except ConnectionClosed:
                pass   # a refused stream says why, then closes at once: read that below rather than retry blindly
            with self._lock:
                self._picture = self._latest       # a new stream knows nothing yet: give it the screen again
            self.status = "Connected: waiting for the API"
            sender = asyncio.create_task(self._send(ws))
            pings: deque[tuple[float, float]] = deque(maxlen=12)
            send_error = None
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw) if isinstance(raw, str) else {}
                    except ValueError:
                        msg = {}
                    kind = msg.get("type") if isinstance(msg, dict) else None
                    if kind == "gaze":
                        self._taking = True
                        g = parse_gaze(msg, self.cols, self.rows)
                        if g is not None:
                            got_maps = True
                            self.gaze = g
                            lag = (_now_ms() + self._offset - g.t) if self._offset is not None and g.t else None
                            self._recv.append((g.received, lag))
                            self.status = "Live"
                            if self.on_gaze is not None:
                                try:
                                    self.on_gaze(g)
                                except Exception:  # noqa: BLE001
                                    log.exception("on_gaze callback failed")
                                    self.on_gaze = None
                    elif kind == "pong" and isinstance(msg.get("t"), (int, float)) and msg.get("at"):
                        back = _now_ms()
                        pings.append((back - msg["t"], msg["at"] - (msg["t"] + back) / 2))
                        self._offset = min(pings)[1]          # the fastest round trip says the offset best
                    elif kind == "ready":
                        self._taking = True
                        self.model_version = msg.get("model")
                        self.status = "Ready: waiting for the first map"
                    elif kind == "status":
                        state = msg.get("state")
                        if state in ("starting", "live"):
                            self._taking = True
                        self.status = STATES.get(state, str(state))
                    elif kind == "error":
                        if msg.get("fatal"):
                            raise ApiError(msg.get("status"), str(msg.get("message", "the stream ended")))
                        log.warning("API: %s", msg.get("message"))
                    if sender.done() and not isinstance(sender.exception(), ConnectionClosed):
                        break                  # Stop, or sending failed: reading on is pointless. (If the API closed
                                               # the socket, read on: it may have said why.)
            finally:
                sender.cancel()
                try:
                    await sender
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # noqa: BLE001
                    send_error = e
            if self._stop.is_set():
                return got_maps
            if send_error is not None:
                raise ConnectionError(f"sending failed ({type(send_error).__name__}: {send_error})")
            reason = f"{ws.close_code} {ws.close_reason or ''}".strip() if ws.close_code else "no reason given"
            raise ConnectionError(f"the API closed the stream ({reason})")
        finally:
            await ws.close()

    async def _send(self, ws):
        last_picture = last_ping = 0.0
        last_sent = time.monotonic()           # pings don't count: only pictures and input keep a stream open
        sent_picture: bytes | None = None
        while True:
            if self._stop.is_set():            # Stop or quit: end the stream now rather than when it idles out
                try:
                    await ws.send(json.dumps({"type": "end"}))
                finally:
                    await ws.close()
                return
            now = time.monotonic()
            if now - last_ping >= PING_EVERY_S:
                await ws.send(json.dumps({"type": "ping", "t": _now_ms()}))
                last_ping = now
            with self._lock:
                events, self._events = self._events, []
                picture = None
                if self._picture is not None and now - last_picture >= PICTURE_EVERY_S:
                    picture, self._picture = self._picture, None
            if events:
                await ws.send(json.dumps({"type": "events", "events": events}))
                last_sent = now
            if picture is None and sent_picture is not None and now - last_sent >= KEEPALIVE_S:
                picture = sent_picture         # the same picture again: no screen change, but the stream stays open
            if picture is not None:
                await ws.send(picture)         # binary JPEG, timed by the API when it arrives
                sent_picture, last_picture, last_sent = picture, now, now
            await asyncio.sleep(SEND_EVERY_S)


def _http_message(response) -> str:
    """The message of a refused WebSocket handshake ({"error": {"message": ...}}), or its status."""
    try:
        return str(json.loads(response.body)["error"]["message"])
    except (TypeError, ValueError, KeyError):
        return f"the API answered {response.status_code}"
