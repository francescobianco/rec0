"""The Wayland focus tracker, against a stand-in for the GNOME Shell extension."""

import pytest
from gi.repository import Gio, GLib

from rec0 import shell
from rec0.project import Rect

IFACE = Gio.DBusNodeInfo.new_for_xml(f"""<node><interface name="{shell.INTERFACE}">
  <method name="GetFocus"><arg type="a{{sv}}" direction="out" name="window"/></method>
  <signal name="FocusChanged"><arg type="a{{sv}}" name="window"/></signal>
</interface></node>""").interfaces[0]


def window(id, title, wm_class, x=0, y=0, w=800, h=600, **flags):
    v = {"id": GLib.Variant("t", id), "title": GLib.Variant("s", title), "wm_class": GLib.Variant("s", wm_class),
         "x": GLib.Variant("i", x), "y": GLib.Variant("i", y),
         "width": GLib.Variant("i", w), "height": GLib.Variant("i", h)}
    v.update({k: GLib.Variant("b", b) for k, b in flags.items()})
    return v


@pytest.fixture
def bus(monkeypatch):
    """A private session bus, so the test never meets a real extension."""
    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()
    conn = Gio.DBusConnection.new_for_address_sync(
        test_bus.get_bus_address(),
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    monkeypatch.setattr(shell, "session_bus", lambda: conn)
    yield conn
    conn.close_sync(None)
    test_bus.down()


class FakeExtension:
    def __init__(self, conn, focus):
        self.conn, self.focus = conn, focus
        conn.register_object(shell.OBJECT_PATH, IFACE, self._call)
        self.owner = Gio.bus_own_name_on_connection(conn, shell.BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)

    def _call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        invocation.return_value(GLib.Variant("(a{sv})", (self.focus,)))

    def emit(self, focus):
        self.focus = focus
        self.conn.emit_signal(None, shell.OBJECT_PATH, shell.INTERFACE, "FocusChanged",
                              GLib.Variant("(a{sv})", (focus,)))


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
    assert not shell.available()
    FakeExtension(bus, {})
    spin(100)
    assert shell.available()


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
