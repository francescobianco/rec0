"""Follow the focused window (X11) to drive scene switching."""

from __future__ import annotations

import threading
from typing import Callable

from gi.repository import GLib

from .x11 import X11, FocusedWindow, active_window

__all__ = ["FocusedWindow", "FocusTracker", "active_window"]


class FocusTracker:
    """Polls window state in a thread and calls, on the main loop,
    `callback(active, watched)` whenever something changes:

    active   the focused window (FocusedWindow | None)
    watched  {xid: FocusedWindow | None} for the windows in `watch` (None: closed)

    Title changes (browser tabs), moves and resizes all count as changes.
    """

    def __init__(self, callback: Callable, interval: float = 0.05):
        self.callback = callback
        self.interval = interval
        self.watch: set[int] = set()      # set from the main thread, read by the poller
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rec0-focus", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _loop(self):
        # Xlib connections must not be shared across threads: this one is ours.
        x = X11()
        last = object()
        try:
            while not self._stop.is_set():
                active = x.active_window()
                watched = {xid: x.window_info(xid) for xid in tuple(self.watch)}
                state = (active, tuple(sorted(watched.items())))
                if state != last:
                    last = state
                    GLib.idle_add(self._deliver, active, watched)
                self._stop.wait(self.interval)
        finally:
            x.close()

    def _deliver(self, active, watched):
        if not self._stop.is_set():
            self.callback(active, watched)
        return False
