"""Follow the focused window on Wayland, through rec0's GNOME Shell extension.

Wayland does not let applications see other windows. The extension in
data/gnome-shell/ runs inside GNOME Shell and publishes the focused window on
the session bus; ShellFocusTracker turns it into the same callbacks as the X11
FocusTracker, so the Director switches scenes the same way.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gio, GLib

from .project import Rect
from .x11 import FocusedWindow

BUS_NAME = "io.github.francescobianco.Rec0.Shell"
OBJECT_PATH = "/io/github/francescobianco/Rec0/Shell"
INTERFACE = BUS_NAME
EXTENSION_UUID = "rec0@francescobianco.github.io"


def session_bus() -> Gio.DBusConnection:
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def available() -> bool:
    """Whether the extension is running in this session."""
    try:
        reply = session_bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        return False
    return reply.unpack()[0]


def window_from(info: dict) -> FocusedWindow | None:
    """The FocusedWindow described by the extension (an empty dict: no window)."""
    if not info.get("id"):
        return None
    rect = Rect(info.get("x", 0), info.get("y", 0), info.get("width", 0), info.get("height", 0))
    return FocusedWindow(info["id"], info.get("title", ""), info.get("wm_class", ""), rect,
                         fullscreen=info.get("fullscreen", False), hidden=info.get("minimized", False))


class ShellFocusTracker:
    """Calls `callback(active, watched)` on the main loop whenever the focused
    window changes (title, moves and resizes included), like FocusTracker.

    The whole screen is captured on Wayland, so there are no windows to watch
    one by one: `watch` is accepted and ignored, and `watched` is always empty.
    """

    def __init__(self, callback: Callable):
        self.callback = callback
        self.watch: set[int] = set()
        self._bus: Gio.DBusConnection | None = None
        self._sub = 0
        self._last = object()

    def start(self):
        if self._bus:
            return
        self._bus = session_bus()
        self._sub = self._bus.signal_subscribe(BUS_NAME, INTERFACE, "FocusChanged", OBJECT_PATH, None,
                                               Gio.DBusSignalFlags.NONE, self._on_signal)
        # The current window, before any change happens.
        self._bus.call(BUS_NAME, OBJECT_PATH, INTERFACE, "GetFocus", None, GLib.VariantType("(a{sv})"),
                       Gio.DBusCallFlags.NONE, 1000, None, self._on_reply)

    def stop(self):
        if self._bus and self._sub:
            self._bus.signal_unsubscribe(self._sub)
        self._bus, self._sub = None, 0

    def _on_reply(self, bus, result):
        try:
            info = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            return
        self._deliver(info)

    def _on_signal(self, _bus, _sender, _path, _iface, _signal, params):
        self._deliver(params.unpack()[0])

    def _deliver(self, info: dict):
        if not self._bus:
            return
        win = window_from(info)
        if win != self._last:
            self._last = win
            self.callback(win, {})
