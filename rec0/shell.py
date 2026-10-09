"""Windows on Wayland, through rec0's GNOME Shell extension and Mutter.

Wayland does not let applications see other windows. The extension in
data/gnome-shell/ runs inside GNOME Shell and publishes the focused window on
the session bus; ShellFocusTracker turns it into the same callbacks as the X11
FocusTracker, so the Director switches scenes the same way.

With version 2 of the extension each window can also be followed by its id,
and WindowCast records it on its own through Mutter's screencast API (no
dialog): shared windows are then captured one by one, as on X11.
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
PER_WINDOW_VERSION = 2   # GetWindow and the buffer rectangle

MUTTER_CAST = "org.gnome.Mutter.ScreenCast"
MUTTER_CAST_PATH = "/org/gnome/Mutter/ScreenCast"
CURSOR_HIDDEN, CURSOR_EMBEDDED = 0, 1


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


def version() -> int:
    """The running extension's interface version (0: not running)."""
    try:
        reply = session_bus().call_sync(
            BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (INTERFACE, "Version")), GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        return 0
    return reply.unpack()[0]


def monitors() -> list[dict]:
    """Monitors in layout coordinates, from Mutter (same shape as x11.monitors())."""
    reply = session_bus().call_sync(
        "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig",
        "GetCurrentState", None, None, Gio.DBusCallFlags.NONE, 2000, None)
    _serial, physical, logical, props = reply.unpack()
    modes = {}   # connector -> current mode size in pixels
    for (connector, *_ids), mode_list, _p in physical:
        current = next((m for m in mode_list if m[6].get("is-current")), None)
        if current:
            modes[connector] = (current[1], current[2])
    # Layout mode 1 is "logical" (sizes divided by the scale), 2 "physical".
    logical_layout = props.get("layout-mode", 1) == 1
    out = []
    for i, (x, y, scale, _transform, primary, ids, _p) in enumerate(logical):
        connector = ids[0][0] if ids else str(i)
        w, h = modes.get(connector, (0, 0))
        if logical_layout and scale:
            w, h = round(w / scale), round(h / scale)
        out.append({"index": i, "primary": bool(primary), "name": connector, "x": x, "y": y, "width": w, "height": h})
    return out


def window_from(info: dict, canvas: tuple[int, int] | None = None) -> FocusedWindow | None:
    """The FocusedWindow described by the extension (an empty dict: no window).

    `shadow` is what a capture of the window shows around it: the client-side
    shadows (buffer minus frame) and, for a Mutter window stream, the rest of
    its `canvas` (width, height), where the window sits in the top-left corner.
    """
    if not info.get("id"):
        return None
    rect = Rect(info.get("x", 0), info.get("y", 0), info.get("width", 0), info.get("height", 0))
    shadow = (0, 0, 0, 0)
    if "buffer_width" in info:
        bx, by = info["buffer_x"], info["buffer_y"]
        bw, bh = info["buffer_width"], info["buffer_height"]
        left, top = max(0, rect.x - bx), max(0, rect.y - by)
        right = max(0, bx + bw - rect.x - rect.width)
        bottom = max(0, by + bh - rect.y - rect.height)
        if canvas:
            right, bottom = max(0, canvas[0] - left - rect.width), max(0, canvas[1] - top - rect.height)
        shadow = (left, top, right, bottom)
    return FocusedWindow(info["id"], info.get("title", ""), info.get("wm_class", ""), rect,
                         fullscreen=info.get("fullscreen", False), content=rect, shadow=shadow,
                         hidden=info.get("minimized", False))


class ShellFocusTracker:
    """Calls `callback(active, watched)` on the main loop whenever something
    changes, like FocusTracker:

    active   the focused window (FocusedWindow | None), from the extension's signal
    watched  {id: FocusedWindow | None} for the windows in `watch` (None: closed),
             polled every `interval` s (extension version 2 only)
    """

    def __init__(self, callback: Callable, canvas: tuple[int, int] | None = None, interval: float = 0.1):
        self.callback = callback
        self.canvas = canvas
        self.interval = interval
        self.watch: set[int] = set()
        self._bus: Gio.DBusConnection | None = None
        self._sub = 0
        self._poll = 0
        self._active: FocusedWindow | None | object = object()   # nothing delivered yet
        self._watched: dict[int, FocusedWindow | None] = {}

    def start(self):
        if self._bus:
            return
        self._bus = session_bus()
        self._sub = self._bus.signal_subscribe(BUS_NAME, INTERFACE, "FocusChanged", OBJECT_PATH, None,
                                               Gio.DBusSignalFlags.NONE, self._on_signal)
        # The current window, before any change happens.
        self._bus.call(BUS_NAME, OBJECT_PATH, INTERFACE, "GetFocus", None, GLib.VariantType("(a{sv})"),
                       Gio.DBusCallFlags.NONE, 1000, None, self._on_focus_reply)
        self._poll = GLib.timeout_add(int(self.interval * 1000), self._poll_watched)

    def stop(self):
        if self._bus and self._sub:
            self._bus.signal_unsubscribe(self._sub)
        if self._poll:
            GLib.source_remove(self._poll)
        self._bus, self._sub, self._poll = None, 0, 0

    def _on_focus_reply(self, bus, result):
        try:
            info = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            return
        self._set_active(info)

    def _on_signal(self, _bus, _sender, _path, _iface, _signal, params):
        self._set_active(params.unpack()[0])

    def _set_active(self, info: dict):
        if not self._bus:
            return
        win = window_from(info, self.canvas)
        if win != self._active:
            self._active = win
            self._deliver()

    def _poll_watched(self):
        if not self._bus:
            return False
        # Key 0 is the whole screen (Director.SCREEN_KEY), not a window.
        for wid in [w for w in self.watch if w]:
            self._bus.call(BUS_NAME, OBJECT_PATH, INTERFACE, "GetWindow", GLib.Variant("(t)", (wid,)),
                           GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, 1000, None,
                           self._on_window_reply, wid)
        for gone in [w for w in self._watched if w not in self.watch]:
            del self._watched[gone]
        return True

    def _on_window_reply(self, bus, result, wid):
        try:
            info = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            return   # extension version 1, or busy: keep what we know
        if not self._bus or wid not in self.watch:
            return
        win = window_from(info, self.canvas)
        if self._watched.get(wid, object()) != win:
            self._watched[wid] = win
            self._deliver()

    def _deliver(self):
        if self._active is not None and not isinstance(self._active, FocusedWindow):
            return   # the focus is not known yet
        self.callback(self._active, dict(self._watched))


class WindowCast:
    """One window recorded on its own by Mutter (org.gnome.Mutter.ScreenCast):
    a PipeWire stream as large as the monitor, with the window (shadows included)
    in its top-left corner. The stream lives as long as this object's session."""

    def __init__(self, window_id: int, cursor: bool):
        self.window_id = window_id
        self.cursor = cursor
        self.node_id: int | None = None
        self._bus = session_bus()
        self._session: str | None = None

    def open(self, timeout: float = 3.0) -> int:
        """Start the stream; returns its PipeWire node id."""
        bus = self._bus
        self._session = bus.call_sync(MUTTER_CAST, MUTTER_CAST_PATH, MUTTER_CAST, "CreateSession",
                                      GLib.Variant("(a{sv})", ({},)), GLib.VariantType("(o)"),
                                      Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        # Never embedded: Mutter draws a moving pointer onto a recycled buffer that still
        # holds an older frame, so the window jumps back in time and pointers double.
        props = {"window-id": GLib.Variant("t", self.window_id),
                 "cursor-mode": GLib.Variant("u", CURSOR_HIDDEN)}
        stream = bus.call_sync(MUTTER_CAST, self._session, MUTTER_CAST + ".Session", "RecordWindow",
                               GLib.Variant("(a{sv})", (props,)), GLib.VariantType("(o)"),
                               Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        found: list[int] = []
        sub = bus.signal_subscribe(None, MUTTER_CAST + ".Stream", "PipeWireStreamAdded", stream, None,
                                   Gio.DBusSignalFlags.NONE, lambda *a: found.append(a[-1].unpack()[0]))
        try:
            bus.call_sync(MUTTER_CAST, self._session, MUTTER_CAST + ".Session", "Start", None, None,
                          Gio.DBusCallFlags.NONE, 2000, None)
            # The node id arrives as a signal: wait for it here (the main context is ours).
            ctx = GLib.MainContext.default()
            deadline = GLib.get_monotonic_time() + int(timeout * 1e6)
            while not found and GLib.get_monotonic_time() < deadline:
                ctx.iteration(False) or GLib.usleep(5000)
        finally:
            bus.signal_unsubscribe(sub)
        if not found:
            self.close()
            raise GLib.Error("no stream from Mutter for window %d" % self.window_id)
        self.node_id = found[0]
        return self.node_id

    def close(self):
        if self._session:
            try:
                self._bus.call_sync(MUTTER_CAST, self._session, MUTTER_CAST + ".Session", "Stop", None, None,
                                    Gio.DBusCallFlags.NONE, 1000, None)
            except GLib.Error:
                pass   # already gone with its window
            self._session = None
