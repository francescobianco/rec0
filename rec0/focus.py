"""Follow the focused window (X11) to drive scene switching."""

from __future__ import annotations

import threading
from typing import Callable

from gi.repository import GLib

from .x11 import X11, FocusedWindow, active_window

__all__ = ["FocusedWindow", "FocusTracker", "active_window"]


class FocusTracker:
    """Polls the active window in a thread; calls `callback(FocusedWindow|None)` on the
    main loop whenever focus, title (e.g. browser tab) or geometry changes."""

    def __init__(self, callback: Callable[[FocusedWindow | None], None], interval: float = 0.1):
        self.callback = callback
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: FocusedWindow | None = None

    def start(self):
        if self._thread:
            return
        self._stop.clear()
        self._last = None
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
        try:
            first = True
            while not self._stop.is_set():
                win = x.active_window()
                if first or win != self._last:
                    first = False
                    self._last = win
                    GLib.idle_add(self._deliver, win)
                self._stop.wait(self.interval)
        finally:
            x.close()

    def _deliver(self, win):
        if not self._stop.is_set():
            self.callback(win)
        return False
