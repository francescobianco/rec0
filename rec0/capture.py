"""Resolve project sources into GStreamer source descriptions.

Two desktop backends are supported:

* X11: windows are matched by title/WM_CLASS and captured with ximagesrc,
  fully automatically.
* Wayland: windows and screens go through the XDG Desktop Portal ScreenCast
  interface + PipeWire. The user picks the window once; the portal's
  restore token is stored so later sessions can skip the dialog.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

from . import x11  # noqa: E402
from .project import Project, Rect, _color  # noqa: E402
from .x11 import X11Window  # noqa: E402,F401  (re-exported)

Gst.init(None)


class CaptureError(Exception):
    pass


def session_type() -> str:
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "none"


def q(value) -> str:
    """Quote a value for a gst-launch description."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------
# Devices

@dataclass
class Device:
    name: str
    id: str          # /dev/videoN for cameras, node name for audio
    caps: str = ""


def _monitor(cls: str) -> list[Gst.Device]:
    mon = Gst.DeviceMonitor.new()
    mon.add_filter(cls, None)
    mon.start()
    try:
        return mon.get_devices() or []
    finally:
        mon.stop()


def _prop(dev: Gst.Device, *names: str) -> str | None:
    props = dev.get_properties()
    if props is None:
        return None
    for n in names:
        if props.has_field(n):
            v = props.get_value(n)
            if isinstance(v, str):
                return v
    return None


def cameras() -> list[Device]:
    out = []
    for d in _monitor("Video/Source"):
        path = _prop(d, "api.v4l2.path", "device.path")
        if path and path.startswith("/dev/video") and not any(c.id == path for c in out):
            out.append(Device(d.get_display_name(), path, d.get_caps().to_string() if d.get_caps() else ""))
    return out


def microphones() -> list[Device]:
    out = []
    for d in _monitor("Audio/Source"):
        node = _prop(d, "node.name", "device.name")
        # PulseAudio monitors of sinks also show up as sources: keep real inputs only.
        if node and not node.endswith(".monitor"):
            out.append(Device(d.get_display_name(), node))
    return out


def find_camera(spec: str) -> Device:
    cams = cameras()
    if spec.startswith("/dev/"):
        for c in cams:
            if c.id == spec:
                return c
        if os.path.exists(spec):
            return Device(spec, spec)
        raise CaptureError(f"webcam non trovata: {spec}")
    if not cams:
        raise CaptureError("nessuna webcam trovata")
    if spec == "default":
        return cams[0]
    for c in cams:
        if spec.casefold() in c.name.casefold():
            return c
    raise CaptureError(f"nessuna webcam corrisponde a '{spec}' (disponibili: {', '.join(c.name for c in cams)})")


def find_microphone(spec: str) -> str | None:
    """Return the pulsesrc device name, or None for the default input."""
    if spec == "default":
        return None
    mics = microphones()
    for m in mics:
        if spec == m.id:
            return m.id
    for m in mics:
        if spec.casefold() in m.name.casefold() or spec.casefold() in m.id.casefold():
            return m.id
    raise CaptureError(f"nessun microfono corrisponde a '{spec}' (disponibili: {', '.join(m.name for m in mics)})")


def pick_camera_caps(caps: str, fps: int, want: tuple[int, int]) -> str | None:
    """Choose the camera mode that reaches `fps` with the smallest size >= `want`.

    Webcams usually only deliver high resolutions at full frame rate as MJPEG,
    so letting v4l2src negotiate on its own often ends up at 5 fps.
    """
    modes = []
    for struct in caps.split(";"):
        struct = struct.strip()
        m_type = re.match(r"(image/jpeg|video/x-raw)", struct)
        w = re.search(r"width=\(int\)(\d+)", struct)
        h = re.search(r"height=\(int\)(\d+)", struct)
        fr = re.search(r"framerate=\(fraction\)(.*?)(?:, \w[\w-]*=|$)", struct)
        if not (m_type and w and h and fr):
            continue
        rates = [int(a) / int(b) for a, b in re.findall(r"(\d+)/(\d+)", fr[1]) if int(b)]
        if not rates:
            continue
        fmt = re.search(r"format=\(string\)(\w+)", struct)
        modes.append((m_type[1], int(w[1]), int(h[1]), max(rates), fmt[1] if fmt else None))
    if not modes:
        return None

    def score(mode):
        kind, w, h, rate, _ = mode
        fast = rate >= fps - 0.5
        big = w >= want[0] and h >= want[1]
        # fast first, then big enough, then smallest area that is big enough
        # (or largest if none is), prefer raw on ties to skip decoding.
        return (not fast, not big, w * h if big else -w * h, kind != "video/x-raw")

    kind, w, h, rate, fmt = min(modes, key=score)
    rate = min(int(rate), fps) if rate >= fps - 0.5 else int(rate)
    if kind == "image/jpeg":
        return f"image/jpeg,width={w},height={h},framerate={rate}/1 ! jpegdec"
    return f"video/x-raw,format={fmt},width={w},height={h},framerate={rate}/1" if fmt else \
        f"video/x-raw,width={w},height={h},framerate={rate}/1"


# --------------------------------------------------------------------------
# X11

def x11_windows() -> list[X11Window]:
    return x11.client_windows()


def find_x11_window(match: str) -> X11Window | None:
    needle = match.casefold()
    wins = x11_windows()
    # A class match ("firefox") is more intentional than a title substring.
    for w in wins:
        if needle in w.wm_class.casefold().split():
            return w
    for w in wins:
        if needle in w.title.casefold() or needle in w.wm_class.casefold():
            return w
    return None


def x11_monitors() -> list[dict]:
    return x11.monitors()


# --------------------------------------------------------------------------
# Wayland: XDG Desktop Portal ScreenCast

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
TYPE_MONITOR, TYPE_WINDOW = 1, 2
CURSOR_EMBEDDED = 2
PERSIST_PERMANENT = 2


def _token_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "rec0" / "portal-tokens.json"


def _load_tokens() -> dict:
    try:
        return json.loads(_token_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_token(key: str, token: str | None):
    tokens = _load_tokens()
    if token:
        tokens[key] = token
    else:
        tokens.pop(key, None)
    f = _token_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(tokens, indent=2))


def forget_tokens(project: str | None = None):
    tokens = _load_tokens()
    for k in list(tokens):
        if project is None or k.startswith(project + ":"):
            _save_token(k, None)


class PortalSession:
    """One ScreenCast session, yielding a PipeWire fd + node id."""

    _counter = 0

    def __init__(self, kind: int, cursor: bool, token_key: str):
        self.kind = kind
        self.cursor = cursor
        self.token_key = token_key
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session_handle: str | None = None
        self.node_id: int | None = None
        self.stream: dict = {}
        self.fd: int | None = None

    def _token(self) -> str:
        PortalSession._counter += 1
        return f"rec0_{os.getpid()}_{PortalSession._counter}"

    def _property(self, name: str):
        try:
            v = self.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (SCREENCAST, name)), GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE, -1, None)
            return v.unpack()[0]
        except GLib.Error:
            return None

    def _request(self, method: str, signature: str, args: tuple) -> dict:
        """Call a portal method that answers through a Request object and wait for Response.

        The last element of `args` is the options dict (values already GLib.Variant).
        """
        token = self._token()
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        request_path = f"{PORTAL_PATH}/request/{sender}/{token}"
        result: dict = {}
        loop = GLib.MainLoop()

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            result["code"], result["results"] = params.unpack()
            loop.quit()

        # Subscribe before calling, or a fast Response could be missed.
        sub = self.bus.signal_subscribe(
            PORTAL_BUS, "org.freedesktop.portal.Request", "Response", request_path,
            None, Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)
        try:
            options = {**args[-1], "handle_token": GLib.Variant("s", token)}
            self.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, SCREENCAST, method,
                GLib.Variant(signature, (*args[:-1], options)), None, Gio.DBusCallFlags.NONE, -1, None)
            loop.run()
        except GLib.Error as e:
            raise CaptureError(f"portale ScreenCast ({method}): {e.message}") from e
        finally:
            self.bus.signal_unsubscribe(sub)
        if result.get("code") == 1:
            raise CaptureError("condivisione schermo annullata dall'utente")
        if result.get("code") != 0:
            raise CaptureError(f"portale ScreenCast ({method}) ha risposto con errore {result.get('code')}")
        return result["results"]

    def open(self):
        if self._property("version") is None:
            raise CaptureError("portale ScreenCast non disponibile (serve xdg-desktop-portal-gnome)")
        res = self._request("CreateSession", "(a{sv})", ({
            "session_handle_token": GLib.Variant("s", self._token()),
        },))
        self.session_handle = res["session_handle"]

        cursor_modes = self._property("AvailableCursorModes") or 0
        options = {
            "types": GLib.Variant("u", self.kind),
            "multiple": GLib.Variant("b", False),
            "persist_mode": GLib.Variant("u", PERSIST_PERMANENT),
        }
        if self.cursor and cursor_modes & CURSOR_EMBEDDED:
            options["cursor_mode"] = GLib.Variant("u", CURSOR_EMBEDDED)
        token = _load_tokens().get(self.token_key)
        if token:
            options["restore_token"] = GLib.Variant("s", token)
        self._request("SelectSources", "(oa{sv})", (self.session_handle, options))

        res = self._request("Start", "(osa{sv})", (self.session_handle, "", {}))
        streams = res.get("streams") or []
        if not streams:
            raise CaptureError("il portale non ha restituito alcuno stream")
        self.node_id, self.stream = streams[0]
        _save_token(self.token_key, res.get("restore_token"))

        reply, fds = self.bus.call_with_unix_fd_list_sync(
            PORTAL_BUS, PORTAL_PATH, SCREENCAST, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_handle, {})), GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE, -1, None, None)
        self.fd = fds.get(reply.unpack()[0])

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.session_handle:
            try:
                self.bus.call_sync(PORTAL_BUS, self.session_handle, "org.freedesktop.portal.Session",
                                   "Close", None, None, Gio.DBusCallFlags.NONE, -1, None)
            except GLib.Error:
                pass
            self.session_handle = None


# --------------------------------------------------------------------------
# Capture manager

class Captures:
    """Resolves the project's camera and monitor into gst-launch fragments.

    Portal sessions stay open across pipelines (preview -> record), so on
    Wayland the user is asked at most once per run.
    """

    TEST_MONITOR = Rect(0, 0, 1920, 1080)

    def __init__(self, project: Project, backend: str | None = None, log=print):
        self.project = project
        self.backend = backend or session_type()
        self.log = log
        self.camera: Device | None = None
        self.monitor: Rect | None = None      # absolute geometry of the captured monitor
        self.portal: PortalSession | None = None
        self.prepared = False

    @property
    def follows_focus(self) -> bool:
        """Whether scenes can switch automatically on window focus."""
        return self.backend == "x11" and self.project.screen.monitor != "test" and bool(self.project.windows)

    def launch_apps(self):
        for item in self.project.launch:
            self.log(f"avvio: {item.command}")
            try:
                subprocess.Popen(shlex.split(os.path.expanduser(item.command)), cwd=item.cwd,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                raise CaptureError(f"impossibile avviare '{item.command}': {e.strerror}") from e
            if item.delay:
                time.sleep(item.delay)

    def prepare(self):
        if self.prepared:
            return
        p = self.project
        if p.camera and p.camera.device != "test":
            self.camera = find_camera(p.camera.device)
        if p.windows:
            self.monitor = self._prepare_monitor()
        self.prepared = True

    def _prepare_monitor(self) -> Rect:
        spec = self.project.screen.monitor
        if spec == "test":
            return self.TEST_MONITOR
        if self.backend == "x11":
            mons = x11_monitors()
            if not mons:
                raise CaptureError("impossibile determinare la geometria dei monitor")
            if spec == "primary":
                m = next((m for m in mons if m["primary"]), mons[0])
            else:
                m = next((m for m in mons if spec in (str(m["index"]), m["name"])), None)
                if m is None:
                    raise CaptureError(f"monitor '{spec}' non trovato (disponibili: {', '.join(m['name'] for m in mons)})")
            return Rect(m["x"], m["y"], m["width"], m["height"])
        if self.backend == "wayland":
            self.log("seleziona lo schermo da condividere nella finestra di dialogo del sistema (solo la prima volta)")
            self.portal = PortalSession(TYPE_MONITOR, self.project.screen.cursor, f"{self.project.name}:screen")
            self.portal.open()
            w, h = self.portal.stream.get("size", (1920, 1080))
            x, y = self.portal.stream.get("position", (0, 0))
            return Rect(x, y, w, h)
        raise CaptureError("nessuna sessione grafica rilevata per catturare lo schermo")

    def camera_source(self, want: tuple[int, int]) -> str | None:
        cam = self.project.camera
        if cam is None:
            return None
        if cam.device == "test":
            return "videotestsrc is-live=true pattern=ball"
        caps = pick_camera_caps(self.camera.caps, self.project.fps, cam.capture_size or want)
        return f"v4l2src device={q(self.camera.id)} do-timestamp=true" + (f" ! {caps}" if caps else " ! decodebin")

    def screen_source(self) -> str | None:
        if self.monitor is None:
            return None
        fps = self.project.fps
        m = self.monitor
        if self.project.screen.monitor == "test":
            return f"videotestsrc is-live=true pattern=smpte ! video/x-raw,width={m.width},height={m.height}"
        if self.backend == "x11":
            cursor = "true" if self.project.screen.cursor else "false"
            return (f"ximagesrc use-damage=false show-pointer={cursor} startx={m.x} starty={m.y} "
                    f"endx={m.x + m.width - 1} endy={m.y + m.height - 1}")
        # pipewiresrc takes ownership of the fd, so hand each pipeline its own copy.
        fd = os.dup(self.portal.fd)
        return (f"pipewiresrc fd={fd} path={self.portal.node_id} do-timestamp=true "
                f"keepalive-time={max(1, 1000 // fps)} always-copy=true")

    def close(self):
        if self.portal:
            self.portal.close()
            self.portal = None
        self.prepared = False


def background_description(project: Project) -> str:
    bg = project.background
    fps = project.fps
    caps = f"video/x-raw,width={project.width},height={project.height},framerate={fps}/1"
    color = {"black": 0xFF000000, "white": 0xFFFFFFFF}.get(bg) or _color(bg)
    if color is not None:
        return f"videotestsrc is-live=true pattern=solid-color foreground-color={color} ! {caps}"
    image = prepared_background(bg, project.width, project.height)
    return (f"filesrc location={q(image)} ! pngdec ! videoconvert ! imagefreeze name=bgfreeze is-live=true ! "
            f"{caps},pixel-aspect-ratio=1/1")


def prepared_background(path: str, width: int, height: int) -> str:
    """Crop (cover) and scale the background image to the canvas once, cached as PNG.

    Photos can be tens of megapixels: decoding and scaling them at every pipeline
    start (preview -> record) would be slow.
    """
    import hashlib

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}:{st.st_mtime_ns}:{st.st_size}:{width}x{height}".encode()).hexdigest()
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "rec0" / f"bg-{key[:16]}.png"
    if cache.exists():
        return str(cache)
    try:
        src = GdkPixbuf.Pixbuf.new_from_file(path).apply_embedded_orientation()
    except GLib.Error as e:
        raise CaptureError(f"impossibile leggere lo sfondo {path}: {e.message}") from e
    k = max(width / src.get_width(), height / src.get_height())
    scaled = src.scale_simple(max(width, round(src.get_width() * k)), max(height, round(src.get_height() * k)),
                              GdkPixbuf.InterpType.HYPER)
    x = (scaled.get_width() - width) // 2
    y = (scaled.get_height() - height) // 2
    out = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    scaled.copy_area(x, y, width, height, out, 0, 0)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    out.savev(str(tmp), "png", [], [])
    tmp.replace(cache)
    return str(cache)
