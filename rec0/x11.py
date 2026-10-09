"""Minimal X11 client over ctypes (libX11 + libXrandr).

Replaces xprop / xwininfo / xrandr, which are not available inside a Flatpak
runtime. Each X11 instance owns its own Display connection: use one per
thread (the focus tracker opens its own in its polling thread).
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_long, c_ubyte, c_ulong, c_void_p
from dataclasses import dataclass

from .project import Rect


@dataclass(frozen=True)
class FocusedWindow:
    xid: int
    title: str
    wm_class: str
    rect: Rect


@dataclass
class X11Window:
    xid: int
    title: str
    wm_class: str


Window = Atom = c_ulong
Display_p = c_void_p
Success = 0
AnyPropertyType = 0


class XErrorEvent(Structure):
    _fields_ = [("type", c_int), ("display", Display_p), ("resourceid", c_ulong), ("serial", c_ulong),
                ("error_code", c_ubyte), ("request_code", c_ubyte), ("minor_code", c_ubyte)]


class XWindowAttributes(Structure):
    _fields_ = [("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int),
                ("border_width", c_int), ("depth", c_int), ("visual", c_void_p), ("root", Window),
                ("class_", c_int), ("bit_gravity", c_int), ("win_gravity", c_int),
                ("backing_store", c_int), ("backing_planes", c_ulong), ("backing_pixel", c_ulong),
                ("save_under", c_int), ("colormap", c_ulong), ("map_installed", c_int),
                ("map_state", c_int), ("all_event_masks", c_long), ("your_event_mask", c_long),
                ("do_not_propagate_mask", c_long), ("override_redirect", c_int), ("screen", c_void_p)]


class XRRMonitorInfo(Structure):
    _fields_ = [("name", Atom), ("primary", c_int), ("automatic", c_int), ("noutput", c_int),
                ("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int),
                ("mwidth", c_int), ("mheight", c_int), ("outputs", POINTER(c_ulong))]


ErrorHandler = ctypes.CFUNCTYPE(c_int, Display_p, POINTER(XErrorEvent))

_lock = threading.Lock()
_xlib = None
_xrandr = None
_handler = None
_errors: dict[int, int] = {}    # display address -> last error code


def _on_error(display, event) -> int:
    # The default handler exits the process: a window vanishing mid-query
    # (BadWindow) is normal, so just remember it for the caller.
    _errors[display or 0] = event.contents.error_code
    return 0


def _load():
    """Load the libraries once; returns libX11 or None."""
    global _xlib, _xrandr, _handler
    with _lock:
        if _xlib is not None:
            return _xlib or None
        try:
            x = ctypes.CDLL("libX11.so.6")
        except OSError:
            _xlib = False
            return None
        x.XInitThreads.restype = c_int
        x.XOpenDisplay.argtypes = [c_char_p]
        x.XOpenDisplay.restype = Display_p
        x.XCloseDisplay.argtypes = [Display_p]
        x.XSetErrorHandler.argtypes = [ErrorHandler]
        x.XSetErrorHandler.restype = c_void_p
        x.XDefaultRootWindow.argtypes = [Display_p]
        x.XDefaultRootWindow.restype = Window
        x.XDefaultScreen.argtypes = [Display_p]
        x.XDisplayWidth.argtypes = x.XDisplayHeight.argtypes = [Display_p, c_int]
        x.XInternAtom.argtypes = [Display_p, c_char_p, c_int]
        x.XInternAtom.restype = Atom
        x.XGetAtomName.argtypes = [Display_p, Atom]
        x.XGetAtomName.restype = c_void_p      # must stay a raw pointer to XFree it
        x.XFree.argtypes = [c_void_p]
        x.XGetWindowProperty.argtypes = [
            Display_p, Window, Atom, c_long, c_long, c_int, Atom, POINTER(Atom), POINTER(c_int),
            POINTER(c_ulong), POINTER(c_ulong), POINTER(c_void_p)]
        x.XGetWindowProperty.restype = c_int
        x.XGetWindowAttributes.argtypes = [Display_p, Window, POINTER(XWindowAttributes)]
        x.XGetWindowAttributes.restype = c_int
        x.XTranslateCoordinates.argtypes = [Display_p, Window, Window, c_int, c_int,
                                            POINTER(c_int), POINTER(c_int), POINTER(Window)]
        x.XTranslateCoordinates.restype = c_int
        x.XInitThreads()
        _handler = ErrorHandler(_on_error)    # keep a reference: X calls it later
        x.XSetErrorHandler(_handler)
        try:
            r = ctypes.CDLL("libXrandr.so.2")
            r.XRRGetMonitors.argtypes = [Display_p, Window, c_int, POINTER(c_int)]
            r.XRRGetMonitors.restype = POINTER(XRRMonitorInfo)
            r.XRRFreeMonitors.argtypes = [POINTER(XRRMonitorInfo)]
            _xrandr = r
        except (OSError, AttributeError):
            _xrandr = None
        _xlib = x
        return x


class X11:
    """One Display connection. Not shareable between threads: open one per thread."""

    def __init__(self, display: str | None = None):
        self._x = _load()
        self._dpy = self._x.XOpenDisplay(display.encode() if display else None) if self._x else None
        self._atoms: dict[str, int] = {}
        self.root = self._x.XDefaultRootWindow(self._dpy) if self._dpy else 0

    @property
    def ok(self) -> bool:
        return bool(self._dpy)

    def close(self):
        if self._dpy:
            self._x.XCloseDisplay(self._dpy)
            _errors.pop(self._dpy, None)
            self._dpy = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:    # interpreter shutdown
            pass

    # -- low level

    def atom(self, name: str) -> int:
        a = self._atoms.get(name)
        if a is None:
            a = self._atoms[name] = self._x.XInternAtom(self._dpy, name.encode(), False)
        return a

    def _prop(self, win: int, name: str) -> tuple[int, int, bytes | list[int]] | None:
        """Return (type, format, data) of a property; format-32 data is a list of ints."""
        rtype, fmt, n, after, data = Atom(), c_int(), c_ulong(), c_ulong(), c_void_p()
        status = self._x.XGetWindowProperty(self._dpy, win, self.atom(name), 0, 1 << 24, False,
                                            AnyPropertyType, byref(rtype), byref(fmt), byref(n),
                                            byref(after), byref(data))
        if status != Success or not data.value:
            return None
        try:
            if rtype.value == 0:
                return None
            if fmt.value == 32:
                # Xlib hands format-32 data back as C longs (8 bytes on 64-bit).
                return rtype.value, 32, list(ctypes.cast(data, POINTER(c_long))[:n.value])
            size = n.value * fmt.value // 8
            return rtype.value, fmt.value, ctypes.string_at(data, size)
        finally:
            self._x.XFree(data)

    def _text(self, win: int, name: str) -> str | None:
        p = self._prop(win, name)
        if not p or p[1] != 8:
            return None
        return p[2].decode("utf-8" if p[0] == self.atom("UTF8_STRING") else "latin-1", "replace")

    def _cardinals(self, win: int, name: str) -> list[int]:
        p = self._prop(win, name)
        return [v & 0xFFFFFFFF for v in p[2]] if p and p[1] == 32 else []

    def _title(self, win: int) -> str:
        return (self._text(win, "_NET_WM_NAME") or self._text(win, "WM_NAME") or "").rstrip("\0")

    def _wm_class(self, win: int) -> str:
        cls = self._text(win, "WM_CLASS") or ""
        return " ".join(s for s in cls.split("\0") if s)

    # -- queries

    def active_window(self) -> FocusedWindow | None:
        if not self._dpy:
            return None
        _errors.pop(self._dpy, None)
        ids = self._cardinals(self.root, "_NET_ACTIVE_WINDOW")
        if not ids or not ids[0]:
            return None
        wid = ids[0]
        attrs = XWindowAttributes()
        if not self._x.XGetWindowAttributes(self._dpy, wid, byref(attrs)):
            return None
        rx, ry, child = c_int(), c_int(), Window()
        # Same as xwininfo's "Absolute upper-left": outer corner of the border.
        bw = attrs.border_width
        if not self._x.XTranslateCoordinates(self._dpy, wid, self.root, -bw, -bw,
                                             byref(rx), byref(ry), byref(child)):
            return None
        title, wm_class = self._title(wid), self._wm_class(wid)
        frame = self._cardinals(wid, "_NET_FRAME_EXTENTS")
        gtk = self._cardinals(wid, "_GTK_FRAME_EXTENTS")
        if self._dpy in _errors:     # the window went away while we were reading it
            return None
        x, y, w, h = rx.value, ry.value, attrs.width, attrs.height
        # Server-side decorations (title bar) belong to the window visually...
        fl, fr, ft, fb = frame if len(frame) == 4 else (0, 0, 0, 0)
        # ...client-side shadows do not.
        gl, gr, gt, gb = gtk if len(gtk) == 4 else (0, 0, 0, 0)
        x, y = x - fl + gl, y - ft + gt
        w, h = w + fl + fr - gl - gr, h + ft + fb - gt - gb
        return FocusedWindow(xid=wid, title=title, wm_class=wm_class, rect=Rect(x, y, w, h))

    def client_windows(self) -> list[X11Window]:
        if not self._dpy:
            return []
        out = []
        for wid in self._cardinals(self.root, "_NET_CLIENT_LIST"):
            _errors.pop(self._dpy, None)
            title, wm_class = self._title(wid), self._wm_class(wid)
            if self._dpy not in _errors:
                out.append(X11Window(wid, title, wm_class))
        return out

    def monitors(self) -> list[dict]:
        if not self._dpy:
            return []
        out = []
        if _xrandr is not None:
            n = c_int()
            info = _xrandr.XRRGetMonitors(self._dpy, self.root, True, byref(n))
            if info:
                try:
                    for i in range(n.value):
                        m = info[i]
                        ptr = self._x.XGetAtomName(self._dpy, m.name) if m.name else None
                        name = ctypes.string_at(ptr).decode("utf-8", "replace") if ptr else str(i)
                        if ptr:
                            self._x.XFree(ptr)
                        out.append({"index": i, "primary": bool(m.primary), "name": name,
                                    "width": m.width, "height": m.height, "x": m.x, "y": m.y})
                finally:
                    _xrandr.XRRFreeMonitors(info)
        if not out:
            screen = self._x.XDefaultScreen(self._dpy)
            out.append({"index": 0, "primary": True, "name": "default",
                        "width": self._x.XDisplayWidth(self._dpy, screen),
                        "height": self._x.XDisplayHeight(self._dpy, screen), "x": 0, "y": 0})
        return out


# Shared connection for one-off queries from the main thread.
_default: X11 | None = None
_default_lock = threading.Lock()


def _shared(fn):
    global _default
    with _default_lock:
        if _default is None or not _default.ok:
            _default = X11()
        return fn(_default)


def active_window() -> FocusedWindow | None:
    return _shared(X11.active_window)


def client_windows() -> list[X11Window]:
    return _shared(X11.client_windows)


def monitors() -> list[dict]:
    return _shared(X11.monitors)
