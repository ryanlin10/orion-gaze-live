"""GazeStream against a local stand-in for Orion's /v1/stream (no key, no network)."""
import asyncio
import json
import threading
import time
from http import HTTPStatus

import numpy as np
import pytest

from gazelive import stream as S


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Relay:
    """Behaves as the docs describe: status waiting_for_gpu, then live, ready, and gaze maps `rate` times a second;
    answers pings; records everything it receives. `refuse` = an HTTP status for the handshake, or a fatal message."""

    def __init__(self, refuse=None, fatal=None, gpu_after=0.3):
        self.got, self.request_path, self.headers = [], None, None
        self.refuse, self.fatal, self.gpu_after = refuse, fatal, gpu_after
        self.connections = 0
        self.ended = threading.Event()
        ready = threading.Event()
        threading.Thread(target=lambda: asyncio.run(self._serve(ready)), daemon=True).start()
        assert ready.wait(5)

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/v1/stream"

    async def _serve(self, ready):
        from websockets.asyncio.server import serve

        def process_request(connection, request):
            self.request_path, self.headers = request.path, request.headers
            if self.refuse:
                body = json.dumps({"error": {"status": self.refuse, "message": "Send your API key as X-API-Key"}})
                return connection.respond(HTTPStatus(self.refuse), body)
            return None

        async with serve(self._handler, "127.0.0.1", 0, process_request=process_request, max_size=2 ** 24) as server:
            self.port = server.sockets[0].getsockname()[1]
            ready.set()
            await asyncio.Future()

    async def _handler(self, ws):
        self.connections += 1
        if self.fatal:
            await ws.send(json.dumps({"type": "error", "fatal": True, "status": 429, "message": self.fatal}))
            await ws.close(4429)
            return
        state = {"attached": False}

        async def gpu():
            await asyncio.sleep(self.gpu_after)
            state["attached"] = True
            await ws.send(json.dumps({"type": "status", "state": "live"}))
            await ws.send(json.dumps({"type": "ready", "model": "gaze-live@test", "grid": {"cols": 64, "rows": 40}}))
            while True:
                await asyncio.sleep(0.1)
                m = np.zeros(64 * 40)
                m[5 * 64 + 10] = 0.9
                await ws.send(json.dumps({"type": "gaze", "t": time.time() * 1000, "on_screen": 0.9, "x": 237, "y": 123,
                                          "map": m.tolist()}))

        await ws.send(json.dumps({"type": "status", "state": "waiting_for_gpu"}))
        task = asyncio.create_task(gpu())
        try:
            async for raw in ws:
                msg = ("picture", raw) if isinstance(raw, bytes) else json.loads(raw)
                self.got.append((state["attached"], msg))
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "t": msg["t"], "at": time.time() * 1000}))
                if isinstance(msg, dict) and msg.get("type") == "end":
                    self.ended.set()
        except Exception:  # noqa: BLE001
            pass
        finally:
            task.cancel()

    def messages(self, kind):
        return [m for _, m in self.got if isinstance(m, dict) and m.get("type") == kind]

    def pictures(self):
        return [m[1] for _, m in self.got if isinstance(m, tuple)]


def test_parse_gaze():
    m = np.zeros(64 * 40)
    m[3 * 64 + 7] = 0.5
    g = S.parse_gaze({"type": "gaze", "t": 1.0, "on_screen": 0.5, "x": 100, "y": 50, "map": m.tolist()}, 64, 40)
    assert g.map.shape == (40, 64) and g.map[3, 7] == pytest.approx(0.5)
    assert (g.x, g.y, g.on_screen) == (100, 50, 0.5)
    assert S.parse_gaze({"type": "gaze", "map": [0.1] * 10}, 64, 40) is None
    assert S.parse_gaze({"type": "gaze"}, 64, 40) is None
    g = S.parse_gaze({"map": [0.25] * 640}, 32, 20)
    assert g.on_screen == pytest.approx(160) and g.x is None


def test_options_are_checked():
    with pytest.raises(ValueError):
        S.GazeStream("k", (100, 100), model="nope")
    with pytest.raises(ValueError):
        S.GazeStream("k", (100, 100), rate=50)
    with pytest.raises(ValueError):
        S.GazeStream("", (100, 100))


def test_stream_end_to_end():
    relay = Relay()
    s = S.GazeStream("secret", (1512, 982.4), recorded=("pointer", "keys"), url=relay.url).start()
    s.move(1, 1)                                   # before a GPU has the stream: dropped
    s.picture(b"\xff\xd8first")
    s.picture(b"\xff\xd8second")                   # newer: replaces the first, unsent
    assert wait_for(lambda: s.gaze is not None)
    for i in range(5):
        s.move(10 + i, 20)
    s.key()
    s.scroll(120)
    assert wait_for(lambda: len([e for m in relay.messages("events") for e in m["events"]]) >= 7)
    s.close()
    assert relay.ended.is_set() and not s.running

    assert relay.request_path == "/v1/stream?model=gaze-live"
    assert relay.headers["X-API-Key"] == "secret" and relay.headers["User-Agent"].startswith("gaze-live/")
    start = relay.got[0][1]
    assert start == {"type": "start", "rate": 10, "grid": "64x40", "screen": {"width": 1512, "height": 982},
                     "recorded": ["pointer", "keys"]}
    events = [e for m in relay.messages("events") for e in m["events"]]
    assert {"type": "move", "x": 1, "y": 1} not in events
    assert events[-2:] == [{"type": "key"}, {"type": "scroll", "y": 120}]
    assert all("t" not in e for e in events)       # the API times input when it arrives
    assert relay.pictures() == [b"\xff\xd8second"]
    assert all(attached for attached, m in relay.got if isinstance(m, dict) and m.get("type") == "events")


def test_pictures_at_most_one_a_second():
    relay = Relay(gpu_after=0)
    s = S.GazeStream("k", (100, 100), url=relay.url).start()
    t_end = time.monotonic() + 2.5
    i = 0
    while time.monotonic() < t_end:
        s.picture(b"\xff\xd8%d" % i)
        i += 1
        time.sleep(0.05)
    s.close()
    assert 2 <= len(relay.pictures()) <= 3


def test_refused_key_stops_for_good():
    relay = Relay(refuse=401)
    s = S.GazeStream("bad", (100, 100), url=relay.url).start()
    assert wait_for(lambda: not s.running)
    assert s.stopped_reason == "the API refused this key (revoked or mistyped)"
    assert s.status.startswith("Stopped")
    s.move(1, 1)                                   # nothing piles up after it stopped
    assert s._events == []


def test_day_used_up_stops_for_good():
    relay = Relay(fatal="This account has streamed its 240 minutes for today (UTC)")
    s = S.GazeStream("k", (100, 100), url=relay.url).start()
    assert wait_for(lambda: not s.running)
    assert "for today" in s.stopped_reason and relay.connections == 1


def test_other_errors_reconnect():
    relay = Relay(fatal="the stream reached its limit of 60 minutes: open a new one to go on")
    s = S.GazeStream("k", (100, 100), url=relay.url).start()
    assert wait_for(lambda: relay.connections >= 2, timeout=6)
    assert s.stopped_reason is None and s.status.startswith(("Reconnecting", "Connect"))
    s.close()
