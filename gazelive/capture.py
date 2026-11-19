"""Your input and your screen on macOS, handed to whichever GazeStream is current.

Input: a listen-only event tap (needs Input Monitoring) for the pointer, buttons, scrolling and that a key was pressed;
without that permission, the pointer and left button polled 60 times a second. Screen: pictures of the main display
through ScreenCaptureKit (needs Screen Recording), with this app's own windows left out, sent when the screen changes.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from typing import Callable

import numpy as np
from PIL import Image

log = logging.getLogger("gazelive")

PICTURE_SIZE = (1024, 640)   # the whole display, stretched onto this canvas
CAPTURE_HZ = 5
CHANGE_TAU = 0.001           # mean grey change (of a 96x60 thumbnail) that makes a picture new
JPEG_QUALITY = 70


def display_size() -> tuple[float, float]:
    """The main display (the one with the menu bar) in points: the units of every pointer position."""
    import Quartz as Q
    b = Q.CGDisplayBounds(Q.CGMainDisplayID())
    return float(b.size.width), float(b.size.height)


# ---------------------------------------------------------------- input

class InputTap:
    """Calls target().move/down/up/key/scroll for each input event; target() returns the current stream or None."""

    def __init__(self, target: Callable[[], object | None]):
        self.target = target
        self.recorded: tuple[str, ...] = ()
        self.scroll_y = 0.0
        self._errors = 0

    def start(self, ask: bool = True) -> str:
        """Start listening. Returns what is recorded, in words."""
        if self._start_tap():
            self.recorded = ("pointer", "keys", "scroll")
            return "pointer, clicks, keys and scrolling"
        import Quartz as Q
        if ask:
            Q.CGRequestListenEventAccess()   # the system prompt, once
        self.recorded = ("pointer",)
        threading.Thread(target=self._poll, daemon=True, name="pointer-poll").start()
        return "pointer and clicks only (allow Input Monitoring for keys and scrolling)"

    def _start_tap(self) -> bool:
        import Quartz as Q
        moves = {Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDragged, Q.kCGEventRightMouseDragged, Q.kCGEventOtherMouseDragged}
        downs = {Q.kCGEventLeftMouseDown, Q.kCGEventRightMouseDown, Q.kCGEventOtherMouseDown}
        ups = {Q.kCGEventLeftMouseUp, Q.kCGEventRightMouseUp, Q.kCGEventOtherMouseUp}
        mask = 0
        for k in moves | downs | ups | {Q.kCGEventScrollWheel, Q.kCGEventKeyDown}:
            mask |= Q.CGEventMaskBit(k)
        box: dict = {}

        def handle(typ, event):
            if typ in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
                Q.CGEventTapEnable(box["tap"], True)   # macOS switched it off: switch it back on
                return
            s = self.target()
            if s is None:
                return
            if typ in moves:
                p = Q.CGEventGetLocation(event); s.move(p.x, p.y)
            elif typ in downs:
                p = Q.CGEventGetLocation(event); s.down(p.x, p.y)
            elif typ in ups:
                p = Q.CGEventGetLocation(event); s.up(p.x, p.y)
            elif typ == Q.kCGEventScrollWheel:
                dy = Q.CGEventGetIntegerValueField(event, Q.kCGScrollWheelEventPointDeltaAxis1)
                if dy:
                    self.scroll_y -= dy           # wheel deltas move the content; the position grows going down
                    s.scroll(self.scroll_y)
            elif typ == Q.kCGEventKeyDown:
                if not Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventAutorepeat):
                    s.key()

        def callback(proxy, typ, event, refcon):
            try:
                handle(typ, event)
            except Exception:  # noqa: BLE001
                self._errors += 1
                if self._errors <= 3:
                    log.exception("input event failed")
            return event

        ready = threading.Event()

        def run():
            tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap, Q.kCGEventTapOptionListenOnly,
                                     mask, callback, None)
            box["tap"] = tap
            ready.set()
            if tap is None:
                return
            source = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
            Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), source, Q.kCFRunLoopCommonModes)
            Q.CGEventTapEnable(tap, True)
            Q.CFRunLoopRun()

        threading.Thread(target=run, daemon=True, name="input-tap").start()
        ready.wait(5)
        return box.get("tap") is not None

    def _poll(self):
        import Quartz as Q
        last, was_down = None, False
        while True:
            s = self.target()
            if s is not None:
                p = Q.CGEventGetLocation(Q.CGEventCreate(None))
                if (p.x, p.y) != last:
                    s.move(p.x, p.y)
                    last = (p.x, p.y)
                down = bool(Q.CGEventSourceButtonState(Q.kCGEventSourceStateCombinedSessionState, Q.kCGMouseButtonLeft))
                if down != was_down:
                    (s.down if down else s.up)(p.x, p.y)
                    was_down = down
            time.sleep(1 / 60)


# ---------------------------------------------------------------- screen

class ScreenGrabber:
    """Pictures of the main display through ScreenCaptureKit, without this process's windows (the heatmap)."""

    def __init__(self, size: tuple[int, int] = PICTURE_SIZE):
        import Quartz as Q
        import ScreenCaptureKit as SCK
        self.Q, self.SCK, self.size = Q, SCK, size
        self.filter = self.config = None
        self.excludes_self = False

    def _call(self, fn, *args, timeout: float = 5.0):
        done, box = threading.Event(), {}

        def handler(result, error):
            box["result"], box["error"] = result, error
            done.set()
        fn(*args, handler)
        done.wait(timeout)
        return box.get("result"), box.get("error")

    def setup(self) -> str | None:
        """(Re)build the capture filter. Returns a problem in words, or None."""
        SCK, Q = self.SCK, self.Q
        content, err = self._call(
            SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_,
            False, True)
        if content is None:
            return f"screen capture refused ({err})"
        main = [d for d in content.displays() if d.displayID() == Q.CGMainDisplayID()]
        if not main:
            return "the main display can't be captured"
        me = [a for a in content.applications() if a.processID() == os.getpid()]
        self.filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(main[0], me, [])
        # Capture in the display's own shape, then stretch onto the canvas: asking ScreenCaptureKit for the canvas size
        # letterboxes instead, which shifts everything on screen away from where the pointer says it is.
        dw, dh = float(main[0].width()), float(main[0].height())
        k = max(self.size[0] / dw, self.size[1] / dh)
        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setWidth_(round(dw * k)); cfg.setHeight_(round(dh * k)); cfg.setShowsCursor_(False)
        self.config = cfg
        self.excludes_self = bool(me)   # our windows are listed only once one is on screen
        return None

    def grab(self) -> np.ndarray | None:
        """One RGB picture [h, w, 3] at `size`, or None."""
        Q = self.Q
        img, _ = self._call(self.SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_,
                            self.filter, self.config, timeout=2)
        if img is None:
            return None
        w, h, bpr = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img), Q.CGImageGetBytesPerRow(img)
        raw = np.frombuffer(bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img))), np.uint8)
        px = raw[:h * bpr].reshape(h, bpr)[:, :w * 4].reshape(h, w, 4)   # rows can carry padding
        info = Q.CGImageGetBitmapInfo(img)
        alpha_first = (info & Q.kCGBitmapAlphaInfoMask) in (
            Q.kCGImageAlphaFirst, Q.kCGImageAlphaPremultipliedFirst, Q.kCGImageAlphaNoneSkipFirst)
        if (info & Q.kCGBitmapByteOrderMask) == Q.kCGBitmapByteOrder32Little:
            rgb = px[..., [2, 1, 0]]          # BGRA in memory
        else:
            rgb = px[..., [1, 2, 3]] if alpha_first else px[..., :3]
        picture = Image.fromarray(np.ascontiguousarray(rgb))
        if picture.size != self.size:
            picture = picture.resize(self.size, Image.BILINEAR)
        return np.asarray(picture)


class FakeScreen:
    """A synthetic page whose dark block jumps somewhere new every second (for --check: no permissions needed)."""

    def __init__(self, size: tuple[int, int] = PICTURE_SIZE):
        self.size, self.t0 = size, time.time()
        self.excludes_self = True

    def grab(self) -> np.ndarray:
        w, h = self.size
        img = np.full((h, w, 3), 235, np.uint8)
        k = int(time.time() - self.t0)
        x, y = int(w * (0.08 + (k * 0.217) % 0.72)), int(h * (0.1 + (k * 0.137) % 0.7))
        img[y:y + h // 7, x:x + w // 5] = (20, 40, 160)
        return img


class ScreenFeed:
    """Takes a picture CAPTURE_HZ times a second while there is a stream, and gives it a JPEG whenever the screen changed
    (and once to every new stream)."""

    def __init__(self, target: Callable[[], object | None], grabber=None, hz: float = CAPTURE_HZ):
        self.target, self.grabber, self.hz = target, grabber, hz
        self.status = "off" if grabber is None else "on"
        self._resetup = 0.0

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="screen").start()
        return self

    def reset(self):
        """The displays changed: set capture up again on the next picture."""
        if isinstance(self.grabber, ScreenGrabber):
            self.grabber = None

    def _ready(self) -> bool:
        """Set the grabber up once Screen Recording is allowed; False until then."""
        if self.grabber is not None:
            return True
        import Quartz as Q
        if not Q.CGPreflightScreenCaptureAccess():
            if self.status != "needs Screen Recording":
                Q.CGRequestScreenCaptureAccess()   # the system prompt, once
                self.status = "needs Screen Recording"
                log.warning("Screen Recording is not allowed: allow it for the app you ran gaze-live from "
                            "(System Settings > Privacy & Security > Screen & System Audio Recording), then restart it")
            return False
        grabber = ScreenGrabber()
        problem = grabber.setup()
        if grabber.filter is None:
            self.status = problem or "unavailable"
            return False
        self.grabber, self.status, self._resetup = grabber, "on", time.monotonic() + 2
        return True

    def _loop(self):
        last, last_target = None, None
        while True:
            t0 = time.monotonic()
            s = self.target()
            grabber = self.grabber if s is not None and self._ready() else None
            if grabber is not None:
                try:
                    if not grabber.excludes_self and time.monotonic() > self._resetup:
                        grabber.setup()               # the heatmap window exists now: leave it out of the pictures
                        self._resetup = time.monotonic() + 2
                    img = grabber.grab()
                except Exception as e:  # noqa: BLE001
                    self.status = f"capture failed ({e})"
                    log.exception("screen capture failed")
                    img = None
                    time.sleep(2)
                if img is not None:
                    picture = Image.fromarray(img)
                    g = np.asarray(picture.convert("L").resize((96, 60), Image.BOX), np.float32) / 255
                    if s is not last_target or last is None or float(np.abs(g - last).mean()) > CHANGE_TAU:
                        jpeg = io.BytesIO()
                        picture.save(jpeg, "JPEG", quality=JPEG_QUALITY)
                        s.picture(jpeg.getvalue())
                        last, last_target = g, s
            time.sleep(max(0.0, 1 / self.hz - (time.monotonic() - t0)))
