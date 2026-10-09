"""The Wayland focus tracker, against a stand-in for the GNOME Shell extension."""

import threading

import pytest
from gi.repository import Gio, GLib

from rec0 import shell
from rec0.project import Rect

IFACE = Gio.DBusNodeInfo.new_for_xml(f"""<node><interface name="{shell.INTERFACE}">
  <method name="GetFocus"><arg type="a{{sv}}" direction="out" name="window"/></method>
  <method name="GetWindow"><arg type="t" direction="in" name="id"/><arg type="a{{sv}}" direction="out" name="window"/></method>
  <signal name="FocusChanged"><arg type="a{{sv}}" name="window"/></signal>
  <property name="Version" type="u" access="read"/>
</interface></node>""").interfaces[0]


def window(id, title, wm_class, x=0, y=0, w=800, h=600, **flags):
    v = {"id": GLib.Variant("t", id), "title": GLib.Variant("s", title), "wm_class": GLib.Variant("s", wm_class),
         "x": GLib.Variant("i", x), "y": GLib.Variant("i", y),
         "width": GLib.Variant("i", w), "height": GLib.Variant("i", h)}
    v.update({k: GLib.Variant("b", b) for k, b in flags.items()})
    return v


def _connect(address):
    return Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)


@pytest.fixture
def bus(monkeypatch):
    """A private session bus, so the test never meets a real extension."""
    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()
    conn = _connect(test_bus.get_bus_address())
    monkeypatch.setattr(shell, "session_bus", lambda: conn)
    yield test_bus.get_bus_address()
    for ext in FakeExtension.running:
        ext.stop()
    FakeExtension.running.clear()
    conn.close_sync(None)
    test_bus.down()


class FakeExtension:
    """Stands in for GNOME Shell: its own connection, thread and main loop, like a
    separate process (rec0 makes synchronous calls to it)."""

    running: list = []

    def __init__(self, address, focus, windows=None):
        self.focus, self.windows = focus, windows or {}
        ready = threading.Event()
        self.loop = None

        def serve():
            ctx = GLib.MainContext.new()
            ctx.push_thread_default()
            self.conn = _connect(address)
            self.conn.register_object(shell.OBJECT_PATH, IFACE, self._call, lambda *a: GLib.Variant("u", 2), None)
            Gio.bus_own_name_on_connection(self.conn, shell.BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
            self.loop = GLib.MainLoop.new(ctx, False)
            ready.set()
            self.loop.run()
            ctx.pop_thread_default()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        ready.wait(5)
        FakeExtension.running.append(self)

    def _call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetWindow":
            invocation.return_value(GLib.Variant("(a{sv})", (self.windows.get(params.unpack()[0], {}),)))
        else:
            invocation.return_value(GLib.Variant("(a{sv})", (self.focus,)))

    def emit(self, focus):
        self.focus = focus
        self.conn.emit_signal(None, shell.OBJECT_PATH, shell.INTERFACE, "FocusChanged",
                              GLib.Variant("(a{sv})", (focus,)))

    def stop(self):
        self.loop.quit()
        self.thread.join(2)


def spin(ms):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def test_window_from():
    assert shell.window_from({}) is None
    w = shell.window_from({"id": 7, "title": "a", "wm_class": "b", "x": 1, "y": 2, "width": 3, "height": 4,
                           "fullscreen": True})
    assert (w.xid, w.title, w.wm_class, w.rect, w.fullscreen, w.hidden) == (7, "a", "b", Rect(1, 2, 3, 4), True, False)


def test_available_only_with_the_extension(bus):
    assert not shell.available() and shell.version() == 0
    FakeExtension(bus, {})
    spin(100)
    assert shell.available() and shell.version() == 2


def test_window_crop_from_buffer_and_stream_canvas():
    info = {"id": 3, "x": 182, "y": 121, "width": 500, "height": 300,     # frame
            "buffer_x": 168, "buffer_y": 109, "buffer_width": 528, "buffer_height": 329}
    # The capture shows the buffer: the shadows around the frame get cropped.
    assert shell.window_from(info).shadow == (14, 12, 14, 17)
    # A Mutter stream as large as the monitor, the buffer in its top-left corner.
    w = shell.window_from(info, canvas=(800, 600))
    assert w.shadow == (14, 12, 800 - 14 - 500, 600 - 12 - 300)
    assert w.area == Rect(182, 121, 500, 300)


def test_tracker_follows_watched_windows(bus):
    term = window(5, "~ - Terminal", "org.gnome.Terminal", x=10)
    ext = FakeExtension(bus, term, {5: term})
    spin(100)
    seen = []
    tracker = shell.ShellFocusTracker(lambda win, watched: seen.append(
        (win and win.xid, {k: v and v.rect.x for k, v in watched.items()})), interval=0.05)
    tracker.watch = {0, 5}             # 0 is the whole screen: never asked for
    tracker.start()
    spin(300)
    ext.windows[5] = window(5, "~ - Terminal", "org.gnome.Terminal", x=300)   # moved
    spin(300)
    del ext.windows[5]                                                     # closed
    spin(300)
    tracker.stop()
    assert seen[-3:] == [(5, {5: 10}), (5, {5: 300}), (5, {5: None})]


def test_tracker_follows_the_extension(bus):
    ext = FakeExtension(bus, window(1, "Docs - Mozilla Firefox", "firefox"))
    spin(100)
    seen = []
    tracker = shell.ShellFocusTracker(lambda win, watched: seen.append((win and win.title, watched)))
    tracker.start()
    spin(200)                                                      # the current window, at start
    ext.emit(window(2, "~ - Terminal", "gnome-terminal", x=50))
    spin(200)
    ext.emit(window(2, "~ - Terminal", "gnome-terminal", x=50))    # nothing changed: not delivered
    spin(200)
    ext.emit({})                                                   # the desktop, or a shell popup
    spin(200)
    tracker.stop()
    ext.emit(window(1, "Docs - Mozilla Firefox", "firefox"))       # stopped: ignored
    spin(200)
    assert seen == [("Docs - Mozilla Firefox", {}), ("~ - Terminal", {}), (None, {})]
