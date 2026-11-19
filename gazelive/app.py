"""The macOS app: a click-through heatmap over the main display and a Gaze item in the menu bar to stop, start, switch
model and hide the heatmap."""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace

from .capture import InputTap, ScreenFeed, display_size
from .heatmap import Heatmap
from .stream import MODELS, Gaze, GazeStream

log = logging.getLogger("gazelive")

STALE_S = 2.0   # no map for this long: draw nothing rather than an old guess
MODEL_TITLES = {"gaze-live": "gaze-live (realtime)", "gaze-base": "gaze-base"}


def print_gaze(g: Gaze):
    print(json.dumps({"t": g.t, "x": g.x, "y": g.y, "on_screen": round(g.on_screen, 4)}), flush=True)


def build(key: str, model: str = "gaze-live", rate: int = 10, grid: str = "64x40", echo: bool = False):
    """Make the overlay, the menu-bar item, the input tap and the screen feed; nothing streams until ctl.start()."""
    import AppKit
    import objc
    import Quartz as Q
    from Foundation import NSData, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)   # menu bar only, no Dock icon

    # ---- the heatmap: a transparent, click-through window over the main display
    frame = AppKit.NSScreen.screens()[0].frame()      # the menu-bar display, as CGMainDisplayID
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
    win.setLevel_(AppKit.NSScreenSaverWindowLevel)
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.setHasShadow_(False)
    win.setIgnoresMouseEvents_(True)
    win.setSharingType_(AppKit.NSWindowSharingNone)    # ask other screen recorders to leave it out too
    win.setReleasedWhenClosed_(False)
    win.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                               | AppKit.NSWindowCollectionBehaviorStationary
                               | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                               | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    heat = Heatmap()
    rgb = Q.CGColorSpaceCreateDeviceRGB()

    class GazeOverlayView(AppKit.NSView):
        def isFlipped(self):
            return True

        def drawRect_(self, rect):
            cells = getattr(self, "cells", None)
            if cells is None:
                return
            ctx = AppKit.NSGraphicsContext.currentContext().CGContext()
            b = self.bounds()
            h, w = cells.shape[:2]
            data = NSData.dataWithBytes_length_(cells.tobytes(), cells.nbytes)
            img = Q.CGImageCreate(w, h, 8, 32, w * 4, rgb, Q.kCGImageAlphaPremultipliedLast,
                                  Q.CGDataProviderCreateWithCFData(data), None, True, Q.kCGRenderingIntentDefault)
            Q.CGContextSetInterpolationQuality(ctx, Q.kCGInterpolationHigh)
            Q.CGContextSaveGState(ctx)
            Q.CGContextTranslateCTM(ctx, 0, b.size.height)   # images draw bottom-up; this view runs top-down
            Q.CGContextScaleCTM(ctx, 1, -1)
            Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, b.size.width, b.size.height), img)
            Q.CGContextRestoreGState(ctx)

    view = GazeOverlayView.alloc().initWithFrame_(win.contentView().bounds())
    view.cells = None
    view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    win.setContentView_(view)

    ON, OFF = AppKit.NSControlStateValueOn, AppKit.NSControlStateValueOff

    class GazeController(NSObject):
        def init(self):
            self = objc.super(GazeController, self).init()
            if self is None:
                return None
            self.stream, self.model, self.shown = None, model, True
            self.last_tick, self.ticks, self.draw_errors, self.said = time.monotonic(), 0, 0, None
            return self

        # ---- streams
        @objc.python_method
        def live(self) -> GazeStream | None:
            s = self.stream
            return s if s is not None and s.stopped_reason is None else None

        @objc.python_method
        def start(self):
            self.stop()
            s = GazeStream(key, display_size(), model=self.model, rate=rate, grid=grid, recorded=tap.recorded)
            if echo:
                s.on_gaze = print_gaze
            self.stream = s.start()
            if self.shown:
                win.orderFrontRegardless()   # on screen before capture starts, so the pictures leave it out
            log.info("Started %s", self.model)

        @objc.python_method
        def stop(self, wait: bool = False):
            s, self.stream = self.stream, None
            if s is None:
                return
            if wait:
                s.close()
            else:
                threading.Thread(target=s.close, daemon=True).start()

        # ---- menu actions
        def startStop_(self, sender):
            if self.live() is not None:
                self.stop()
            else:
                self.start()
            self.refresh()

        def chooseModel_(self, sender):
            self.model = str(sender.representedObject())
            if self.live() is not None:
                self.start()                 # a model is chosen when a stream opens
            self.refresh()

        def toggleHeatmap_(self, sender):
            self.shown = not self.shown
            self.refresh()

        def quit_(self, sender):
            self.stop(wait=True)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)

        # ---- 30 times a second
        def tick_(self, timer):
            try:
                self.draw()
            except Exception:  # noqa: BLE001
                self.draw_errors += 1
                if self.draw_errors <= 3:
                    log.exception("drawing failed")

        @objc.python_method
        def draw(self):
            now = time.monotonic()
            dt, self.last_tick = now - self.last_tick, now
            s = self.live()
            g = s.gaze if s is not None else None
            if g is not None and now - g.received > STALE_S:
                g = None
            cells = heat.update(None if g is None else g.map, dt)
            if self.shown and s is not None:
                if not win.isVisible():
                    win.orderFrontRegardless()
                if cells is not None or view.cells is not None:
                    view.cells = cells
                    view.setNeedsDisplay_(True)
            else:
                if win.isVisible():
                    win.orderOut_(None)
                view.cells = None
            self.ticks += 1
            if self.ticks % 10 == 0:
                self.refresh()
            if self.ticks % 30 == 0 and s is not None:
                self.follow_display(s)

        @objc.python_method
        def follow_display(self, s):
            """A display plugged in or out, or a new resolution: cover the main display again and restart the stream
            with its new size."""
            size = display_size()
            if (round(size[0]), round(size[1])) == s.screen:
                return
            log.info("The main display changed to %.0fx%.0f: restarting", *size)
            win.setFrame_display_(AppKit.NSScreen.screens()[0].frame(), True)
            feed.reset()
            self.start()

        @objc.python_method
        def refresh(self):
            s, live = self.stream, self.live()
            if s is None:
                text, said = "Stopped", "Stopped"
            elif s.stopped_reason:
                text = said = f"Stopped: {s.stopped_reason}"
            elif feed.status == "needs Screen Recording":
                text = said = "Allow Screen Recording, then restart gaze-live"
            elif s.status == "Live":
                maps, lag = s.stats()
                text = f"{self.model}: {maps:.0f} maps/s" + (f", {lag:.0f} ms behind" if lag is not None else "")
                said = f"Live ({s.model_version or self.model})"
            else:
                text = said = s.status
            if said != self.said:            # log what changed, not every number
                log.info("%s", said)
                self.said = said
            status_item.setTitle_(text)
            start_item.setTitle_("Stop" if live is not None else "Start")
            heat_item.setState_(ON if self.shown else OFF)
            for it in model_items:
                it.setState_(ON if it.representedObject() == self.model else OFF)
            bar.button().setTitle_("◉ Gaze" if live is not None else "○ Gaze")

    ctl = GazeController.alloc().init()
    tap = InputTap(lambda: ctl.live())
    inputs = tap.start()
    log.info("Input: %s", inputs)
    feed = ScreenFeed(lambda: ctl.live()).start()

    # ---- the menu-bar item
    bar = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    bar.button().setTitle_("○ Gaze")
    menu = AppKit.NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)

    def item(title, action=None, parent=menu):
        it = parent.addItemWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            it.setTarget_(ctl)
        else:
            it.setEnabled_(False)
        return it

    status_item = item("Stopped")
    item(f"Input: {inputs}")
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    start_item = item("Start", "startStop:")
    models = AppKit.NSMenu.alloc().initWithTitle_("Model")
    model_items = []
    for name in MODELS:
        it = item(MODEL_TITLES.get(name, name), "chooseModel:", models)
        it.setRepresentedObject_(name)
        model_items.append(it)
    menu.addItemWithTitle_action_keyEquivalent_("Model", None, "").setSubmenu_(models)
    heat_item = item("Show heatmap", "toggleHeatmap:")
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    item("Quit Gaze Live", "quit:")
    bar.setMenu_(menu)

    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1 / 30, ctl, "tick:", None, True)
    NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    return SimpleNamespace(app=app, ctl=ctl, win=win, view=view, bar=bar, feed=feed, tap=tap)


def run(key: str, model: str = "gaze-live", rate: int = 10, grid: str = "64x40", echo: bool = False):
    """Start streaming and run until Quit or Ctrl-C."""
    ui = build(key, model=model, rate=rate, grid=grid, echo=echo)
    for sig in (signal.SIGINT, signal.SIGTERM):   # Ctrl-C in the terminal ends the stream cleanly
        signal.signal(sig, lambda *_: ui.ctl.quit_(None))
    ui.ctl.start()
    ui.ctl.refresh()
    ui.app.run()
