"""Build and run the GStreamer pipeline that composes and records a project.

The pipeline is always the same three layers on a compositor:

    sink_0  virtual desktop (background)
    sink_1  monitor capture, cropped to the shared window
    sink_2  webcam

Scenes are switched by changing pad properties at runtime (see Director).
"""

from __future__ import annotations

import math
import re
import threading
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

gi.require_foreign("cairo")
import cairo  # noqa: E402

from .bubble import draw_ring  # noqa: E402
from .i18n import _  # noqa: E402

from .capture import Captures, background_description, find_microphone, frame_rgb, q  # noqa: E402
from .focus import FocusedWindow  # noqa: E402
from .project import Project  # noqa: E402
from .project import Rect  # noqa: E402
from .scenes import Frame, Shown, compose, interpolate, window_layer  # noqa: E402

Gst.init(None)

PREVIEW_WIDTH = 1280            # preview frames (RGB) go through Python: big enough to look sharp
PREVIEW_FPS = 30
CAMERA_PAD = "sink_1"
CAMERA_Z = 1000                 # above every window layer
# Screen frames are held back this long before reaching the compositor, so that
# when focus moves to a private page the screen layer is frozen before any of
# its frames can be composed (focus is polled every 100 ms).
SCREEN_DELAY = 0.4
BUBBLE_FPS = 20


def has_element(name: str) -> bool:
    return Gst.ElementFactory.find(name) is not None


def video_encoder(bitrate: int, fps: int) -> str:
    if has_element("x264enc"):
        return (f"x264enc bitrate={bitrate} speed-preset=veryfast tune=zerolatency key-int-max={fps * 2} "
                f"! video/x-h264,profile=high")
    if has_element("openh264enc"):
        return f"openh264enc bitrate={bitrate * 1000} complexity=low gop-size={fps * 2}"
    raise RuntimeError(_("no H.264 encoder available (install gstreamer1.0-plugins-ugly)"))


def audio_encoder(bitrate: int) -> str:
    for name in ("fdkaacenc", "avenc_aac", "voaacenc"):
        if has_element(name):
            return f"{name} bitrate={bitrate * 1000}"
    raise RuntimeError(_("no AAC encoder available (install gstreamer1.0-libav)"))


def _pad_props(pad: str, layer, fit: str | None = None) -> str:
    r = layer.rect
    props = f"{pad}::xpos={r.x} {pad}::ypos={r.y} {pad}::width={r.width} {pad}::height={r.height} {pad}::alpha={layer.alpha}"
    if fit:
        props += f" {pad}::sizing-policy={'none' if fit == 'stretch' else 'keep-aspect-ratio'}"
    return props


class Recorder:
    """Runs one pipeline: either a live preview or a recording (with preview).

    Callbacks are invoked from the GLib main loop:
      on_preview(rgba_bytes, width, height)
      on_level(db)            microphone peak level in dB
      on_error(message)
      on_finished(path|None)  recording finalised (or preview stopped)
    """

    def __init__(self, project: Project, captures: Captures):
        self.project = project
        self.captures = captures
        self.pipeline: Gst.Pipeline | None = None
        self.output: Path | None = None
        self.recording = False
        self.frame: Frame | None = None
        self._stopping = False
        self._bus_watch = None
        self._camcrop = None
        self._camcrop_aspect = None
        self._mix = None
        self._playing = False
        self._mask_size = (0, 0)
        self.circle = False              # draw the webcam as a circle (share overlay)
        # Window layers: key -> gst-launch source, kept across pipelines (preview -> record).
        self.sources: dict[int, str] = {}
        self.frozen: dict[int, bool] = {}   # per window: stop updating (privacy)
        self._branches: dict[int, dict] = {}
        self._crops: dict[int, tuple] = {}
        self.on_window_lost: Callable | None = None   # (key) a window's capture failed
        self.frame_rgb = frame_rgb(project.screen.frame)   # frame around windows; None = no frame
        self.on_bubble_frame: Callable | None = None   # (bgra, w, h) from a streaming thread
        self.on_preview: Callable | None = None
        self.on_level: Callable | None = None
        self.on_error: Callable | None = None
        self.on_finished: Callable | None = None

    # ---- description ------------------------------------------------------

    def describe(self, output: Path | None, frame: Frame, preview: bool = True) -> str:
        """Return the gst-launch description. `output=None` means preview only."""
        p = self.project
        fps = p.fps
        self.captures.prepare()
        cam = p.camera
        pads = ["sink_0::zorder=0"]
        parts = [f"{background_description(p)} ! queue ! mix.sink_0"]
        norm = f"videorate ! video/x-raw,framerate={fps}/1,pixel-aspect-ratio=1/1"

        camera = self.captures.camera_source((cam.closeup.rect.width, cam.closeup.rect.height)) if cam else None
        if camera and frame.camera:
            pl = cam.closeup if frame.camera.rect == cam.closeup.rect or not cam.overlay else cam.overlay
            self._camcrop_aspect = self._aspect(pl)
            self.circle = pl.circle
            pads.append(f"{CAMERA_PAD}::zorder={CAMERA_Z} " + _pad_props(CAMERA_PAD, frame.camera, pl.fit))
            parts.append(
                # No scaling before the tee: caps are negotiated across both branches,
                # and the small bubble branch would drag the video branch down with it.
                f"{camera} ! queue max-size-buffers=3 leaky=downstream ! videoconvert "
                # Mirrored before the tee: the video and the on-screen bubble agree.
                f"{'! videoflip video-direction=horiz ' if cam.mirror is not False else ''}! tee name=camt "
                f"camt. ! queue max-size-buffers=3 leaky=downstream "
                f"! aspectratiocrop name=camcrop aspect-ratio={self._camcrop_aspect} ! videoscale ! videoconvert "
                f"! video/x-raw,format=BGRA ! cairooverlay name=cammask ! {norm} ! mix.{CAMERA_PAD}")
            if cam.bubble:
                d = cam.bubble.size
                parts.append(
                    # Height only: the bubble crops the centre square itself
                    # (aspectratiocrop does not crop when the output size is fixed).
                    f"camt. ! queue max-size-buffers=1 leaky=downstream ! videorate drop-only=true "
                    f"! videoscale ! videoconvert "
                    f"! video/x-raw,format=BGRA,height={d},pixel-aspect-ratio=1/1,framerate={BUBBLE_FPS}/1 "
                    f"! appsink name=bubblesink emit-signals=true max-buffers=1 drop=true sync=false")

        parts.insert(0,
            f"compositor name=mix background=black ignore-inactive-pads=true "
            f"latency={int((SCREEN_DELAY + 0.1) * Gst.SECOND) if p.windows else 0} {' '.join(pads)} "
            f"! video/x-raw,format=AYUV,width={p.width},height={p.height},framerate={fps}/1 ! tee name=vt")

        if preview:
            pw = min(PREVIEW_WIDTH, p.width)
            ph = round(pw * p.height / p.width) // 2 * 2
            parts.append(
                f"vt. ! queue max-size-buffers=2 leaky=downstream ! videorate drop-only=true ! videoscale ! videoconvert "
                f"! video/x-raw,format=RGB,width={pw},height={ph},framerate={PREVIEW_FPS}/1 "
                f"! appsink name=preview emit-signals=true max-buffers=1 drop=true sync=false")

        if output is None:
            if not preview:
                parts.append("vt. ! queue ! fakesink sync=false")
            parts.extend(self._audio(None))
            return " ".join(parts)

        o = p.output
        mux = "mp4mux name=mux faststart=true" if o.format == "mp4" else "matroskamux name=mux"
        parts.append(
            f"vt. ! queue max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0 "
            f"! videoconvert ! video/x-raw,format=I420 ! {video_encoder(o.video_bitrate, fps)} ! h264parse ! queue ! mux.")
        parts.append(f"{mux} ! filesink location={q(output)}")
        parts.extend(self._audio(f"{audio_encoder(o.audio_bitrate)} ! aacparse ! queue ! mux."))
        return " ".join(parts)

    @staticmethod
    def _aspect(placement) -> str:
        # fit=cover crops the camera to the placement's aspect ratio; 0/1 disables cropping.
        if placement.fit != "cover":
            return "0/1"
        return f"{placement.rect.width}/{placement.rect.height}"

    def _audio(self, encode: str | None) -> list[str]:
        a = self.project.audio
        inputs = []
        convert = "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
        if a.microphone == "test":
            inputs.append(f"audiotestsrc is-live=true wave=sine volume=0.1 ! {convert} "
                          f"! level name=miclevel interval=100000000 ! volume volume={a.microphone_volume}")
        elif a.microphone:
            dev = find_microphone(a.microphone)
            device = f" device={q(dev)}" if dev else ""
            inputs.append(f"pulsesrc{device} client-name=rec0 ! {convert} "
                          f"! level name=miclevel interval=100000000 ! volume volume={a.microphone_volume}")
        if a.desktop:
            inputs.append(f"pulsesrc device=@DEFAULT_MONITOR@ client-name=rec0 ! {convert} "
                          f"! volume volume={a.desktop_volume}")
        if not inputs:
            return []
        out = f"audioconvert ! audio/x-raw,channels=2 ! queue ! {encode or 'fakesink sync=false'}"
        if len(inputs) == 1:
            # A single input needs no mixer. audiomixer drops and resyncs buffers that
            # arrive late for its deadline (measured: thousands of 10 ms cuts in the
            # GUI with a USB microphone): audible clicks.
            return [f"{inputs[0]} ! queue ! {out}"]
        # Two inputs: give late buffers 200 ms instead of dropping them.
        return [f"audiomixer name=amix latency={200 * Gst.MSECOND} ! {out}",
                *(f"{i} ! queue ! amix." for i in inputs)]

    # ---- scenes -----------------------------------------------------------

    def apply(self, frame: Frame, placement=None):
        """Move the layers. `placement` (camera Placement) updates fit/crop of the webcam."""
        self.frame = frame
        if not self.pipeline:
            return
        layers = dict(frame.windows)
        order = {k: i for i, (k, _l) in enumerate(frame.windows)}
        for key, b in self._branches.items():
            layer = layers.get(key)
            pad = b["pad"]
            if layer is None:
                pad.set_property("alpha", 0.0)
                continue
            r = layer.rect
            for prop, value in (("xpos", r.x), ("ypos", r.y), ("width", r.width), ("height", r.height),
                                ("alpha", layer.alpha), ("zorder", 10 + order[key])):
                pad.set_property(prop, value)
            b["edge"], b["radius"] = layer.edge, layer.radius
            # Changing the margins while the branch negotiates fails (not-negotiated):
            # only once it has produced a frame.
            if layer.crop and b["size"] != (0, 0):
                for name, value in zip(("left", "top", "right", "bottom"), layer.crop):
                    if b["box"].get_property(name) != value:
                        b["box"].set_property(name, value)
        if frame.camera and self._mix.get_static_pad(CAMERA_PAD):
            pad = self._set_pad(CAMERA_PAD, frame.camera)
            if placement is not None:
                self.circle = placement.circle
                pad.set_property("sizing-policy", 0 if placement.fit == "stretch" else 1)
                aspect = self._aspect(placement)
                if self._camcrop and aspect != self._camcrop_aspect:
                    Gst.util_set_object_arg(self._camcrop, "aspect-ratio", aspect)
                    self._camcrop_aspect = aspect

    # ---- window layers ----------------------------------------------------

    def add_window(self, key: int, source: str, crop: tuple[int, int, int, int] | None = None):
        """Register a window capture; it starts frozen (see set_window_frozen)."""
        self.sources[key] = source
        if crop:
            self._crops[key] = crop
        self.frozen.setdefault(key, True)
        if self.pipeline and key not in self._branches:
            self._add_branch(key)

    def remove_window(self, key: int):
        self.sources.pop(key, None)
        self._crops.pop(key, None)
        self.frozen.pop(key, None)
        self._remove_branch(key)

    def set_window_frozen(self, key: int, frozen: bool):
        """Freeze a window layer on its last frame (the valve drops new ones)."""
        self.frozen[key] = frozen
        b = self._branches.get(key)
        if b:
            b["valve"].set_property("drop", frozen)

    def _add_branch(self, key: int):
        fps = self.project.fps
        crop = next((l.crop for k, l in (self.frame.windows if self.frame else ()) if k == key), None) \
            or self._crops.get(key) or (0, 0, 0, 0)
        desc = (
            f"{self.sources[key]} ! queue max-size-buffers=3 leaky=downstream ! videoconvert ! video/x-raw,format=I420 "
            # Held back SCREEN_DELAY: privacy decisions apply before frames are composed.
            f"! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 "
            f"min-threshold-time={int(SCREEN_DELAY * Gst.SECOND)} "
            f"! valve name=valve drop={'true' if self.frozen.get(key, True) else 'false'} "
            f"! videoconvert ! video/x-raw,format=BGRA "
            # Crops client-side shadows, adds transparent room for the frame.
            f"! videobox name=box border-alpha=0 left={crop[0]} top={crop[1]} right={crop[2]} bottom={crop[3]} "
            f"! cairooverlay name=mask "
            # Repeats the last frame while the valve is closed (frozen window).
            f"! videoscale ! imagefreeze name=freeze is-live=true allow-replace=true "
            # Explicit capsfilter: a bare caps string at the end of a bin is read as an element.
            f'! videorate ! capsfilter caps="video/x-raw,framerate={fps}/1,pixel-aspect-ratio=1/1"')
        try:
            bin_ = Gst.parse_bin_from_description(desc, True)
        except GLib.Error as e:
            self._emit(self.on_error, e.message)
            return
        bin_.set_name(f"win{key}")
        self.pipeline.add(bin_)
        pad = self._mix.request_pad_simple("sink_%u")
        pad.set_property("alpha", 0.0)
        pad.set_property("sizing-policy", 0)
        bin_.get_static_pad("src").link(pad)
        b = {"bin": bin_, "pad": pad, "box": bin_.get_by_name("box"), "valve": bin_.get_by_name("valve"),
             "freeze": bin_.get_by_name("freeze"), "size": (0, 0), "edge": 0.0, "radius": 0.0}
        mask = bin_.get_by_name("mask")
        mask.connect("caps-changed", self._on_window_caps, b)
        mask.connect("draw", self._on_window_draw, b)
        self._branches[key] = b
        bin_.sync_state_with_parent()

    def _remove_branch(self, key: int):
        b = self._branches.pop(key, None)
        if not b or not self.pipeline:
            return
        src = b["bin"].get_static_pad("src")
        peer = src.get_peer()
        if peer:
            src.unlink(peer)
            self._mix.release_request_pad(peer)
        b["bin"].set_state(Gst.State.NULL)
        self.pipeline.remove(b["bin"])

    def _window_of(self, obj) -> int | None:
        """Key of the window branch an element belongs to, if any."""
        while obj is not None:
            name = obj.get_name() if hasattr(obj, "get_name") else ""
            if name.startswith("win") and name[3:].isdigit() and int(name[3:]) in self._branches:
                return int(name[3:])
            obj = obj.get_parent()
        return None

    def _set_pad(self, name, layer):
        pad = self._mix.get_static_pad(name)
        r = layer.rect
        for prop, value in (("xpos", r.x), ("ypos", r.y), ("width", r.width), ("height", r.height),
                            ("alpha", layer.alpha)):
            pad.set_property(prop, value)
        return pad

    # ---- running ----------------------------------------------------------

    def start(self, frame: Frame, output: Path | None = None):
        """Start recording to `output`, or a preview when output is None."""
        if self.pipeline:
            raise RuntimeError("pipeline already running")
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
        desc = self.describe(output, frame, preview=self.on_preview is not None)
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            raise RuntimeError(_("invalid pipeline: {error}").format(error=e.message)) from e
        self.frame = frame
        self.output = output
        self.recording = output is not None
        self._stopping = False
        self._playing = False
        self._mix = self.pipeline.get_by_name("mix")
        self._camcrop = self.pipeline.get_by_name("camcrop")
        self._branches = {}
        for key in list(self.sources):
            self._add_branch(key)

        appsink = self.pipeline.get_by_name("preview")
        if appsink is not None:
            appsink.connect("new-sample", self._on_sample)
        bubblesink = self.pipeline.get_by_name("bubblesink")
        if bubblesink is not None:
            bubblesink.connect("new-sample", self._on_bubble_sample)
        mask = self.pipeline.get_by_name("cammask")
        if mask is not None:
            mask.connect("caps-changed", self._on_mask_caps)
            mask.connect("draw", self._on_mask_draw)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch = bus.connect("message", self._on_message)

        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            err = self._pop_error()
            self._teardown()
            raise RuntimeError(err or _("cannot start the pipeline"))

    def stop(self):
        """Ask the pipeline to finish. Recordings are finalised via EOS so the file is valid."""
        if not self.pipeline or self._stopping:
            return
        self._stopping = True
        if self.recording:
            self.pipeline.send_event(Gst.Event.new_eos())
            # imagefreeze ignores EOS from its (already finished) source: end it by hand.
            freeze = self.pipeline.get_by_name("bgfreeze")
            if freeze is not None:
                freeze.get_static_pad("src").push_event(Gst.Event.new_eos())
            # Window branches end at their imagefreeze too (and a closed valve drops EOS).
            for b in self._branches.values():
                b["freeze"].get_static_pad("src").push_event(Gst.Event.new_eos())
            # Safety net: never hang forever if a source ignores EOS.
            GLib.timeout_add_seconds(10, self._force_stop)
        else:
            self._finish()

    def position(self) -> float:
        if not self.pipeline:
            return 0.0
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else 0.0

    def _force_stop(self):
        if self.pipeline and self._stopping:
            self._emit(self.on_error, _("timed out while finishing the file"))
            self._finish()
        return False

    def _finish(self):
        output = self.output if self.recording and self._playing else None
        self._teardown()
        self._emit(self.on_finished, output)

    def _teardown(self):
        if not self.pipeline:
            return
        bus = self.pipeline.get_bus()
        if self._bus_watch is not None:
            bus.disconnect(self._bus_watch)
            bus.remove_signal_watch()
            self._bus_watch = None
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self._mix = self._camcrop = None
        self._branches = {}
        self.recording = False

    def _pop_error(self) -> str | None:
        msg = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if msg:
            err, _dbg = msg.parse_error()
            return err.message
        return None

    def _on_message(self, _bus, msg: Gst.Message):
        t = msg.type
        if t == Gst.MessageType.EOS:
            self._finish()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipeline and not self._playing:
            if msg.parse_state_changed()[1] == Gst.State.PLAYING:
                self._playing = True
                if self.frame:
                    self.apply(self.frame)
        elif t == Gst.MessageType.ERROR and self._window_of(msg.src) is not None:
            # A window closed or was minimized: drop its layer, keep recording.
            key = self._window_of(msg.src)
            self._remove_branch(key)
            self._emit(self.on_window_lost, key)
        elif t == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            name = msg.src.get_name() if msg.src else "?"
            self._emit(self.on_error, f"{name}: {err.message}")
            if self.recording and self._playing and not self._stopping:
                # The failing branch can no longer carry EOS: push it straight into
                # the muxer so what was recorded so far is still a valid file. From
                # another thread: send_event can block on the muxer's stream lock.
                self._stopping = True
                mux = self.pipeline.get_by_name("mux")

                def push_eos():
                    for pad in mux.sinkpads:
                        pad.send_event(Gst.Event.new_eos())

                threading.Thread(target=push_eos, name="rec0-eos", daemon=True).start()
                GLib.timeout_add_seconds(5, self._force_stop)
            elif not self._stopping or not self.recording:
                # Never started (e.g. webcam busy): nothing to save.
                self._finish()
        elif t == Gst.MessageType.ELEMENT and self.on_level:
            s = msg.get_structure()
            if s and s.get_name() == "level":
                # GValueArray fields are not introspectable: read them from the string form.
                m = re.search(r"peak=\([^)]*\)[<{]\s*([-\w.]+)", s.to_string())
                if m:
                    try:
                        self.on_level(max(-90.0, float(m[1])))
                    except ValueError:
                        pass

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None or self.on_preview is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            buf.unmap(info)
            GLib.idle_add(self._deliver_frame, data, w, h)
        return Gst.FlowReturn.OK

    def _on_bubble_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is not None and self.on_bubble_frame is not None:
            s = sample.get_caps().get_structure(0)
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                data = bytes(info.data)
                buf.unmap(info)
                self.on_bubble_frame(data, s.get_value("width"), s.get_value("height"))
        return Gst.FlowReturn.OK

    def _on_mask_caps(self, _overlay, caps):
        s = caps.get_structure(0)
        self._mask_size = (s.get_value("width"), s.get_value("height"))

    @staticmethod
    def _on_window_caps(_overlay, caps, b):
        st = caps.get_structure(0)
        b["size"] = (st.get_value("width"), st.get_value("height"))

    def _on_window_draw(self, _overlay, cr, _ts, _dur, b):
        """Rounded frame around a window, in the transparent room videobox added."""
        w, h = b["size"]
        e, r = b["edge"], max(b["radius"], b["edge"])
        if e <= 0 or not w:
            return

        def rounded(x, y, rw, rh, rad):
            cr.new_sub_path()
            cr.arc(x + rw - rad, y + rad, rad, -math.pi / 2, 0)
            cr.arc(x + rw - rad, y + rh - rad, rad, 0, math.pi / 2)
            cr.arc(x + rad, y + rh - rad, rad, math.pi / 2, math.pi)
            cr.arc(x + rad, y + rad, rad, math.pi, 3 * math.pi / 2)
            cr.close_path()

        # Transparent outside the rounded outline (the window's own corners included)...
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.rectangle(0, 0, w, h)
        rounded(0, 0, w, h, r)
        cr.fill()
        if self.frame_rgb is None:
            return    # rounded corners only, no visible frame
        # ...and an opaque ring between the outline and the window.
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgb(*self.frame_rgb)
        rounded(0, 0, w, h, r)
        rounded(e, e, w - 2 * e, h - 2 * e, max(1.0, r - e))
        cr.fill()

    def _on_mask_draw(self, _overlay, cr, _ts, _dur):
        w, h = self._mask_size
        if not self.circle or not w:
            return
        d = min(w, h)
        cr.translate((w - d) / 2, (h - d) / 2)
        # Clear everything outside the circle so the compositor sees it as transparent.
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(-w, -h, 3 * w, 3 * h)
        cr.arc(d / 2, d / 2, d / 2, 0, 2 * math.pi)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        draw_ring(cr, d)

    def _deliver_frame(self, data, w, h):
        if self.on_preview and self.pipeline:
            self.on_preview(data, w, h)
        return False

    @staticmethod
    def _emit(cb, *args):
        if cb:
            cb(*args)


class Director:
    """Decides what the video shows and animates the changes.

    mode "auto" follows window focus; "camera" / "share" force a scene.
    on_scene(scene, detail|None) is called when the scene or its subject changes.

    Windows that have been shared stay in the presentation, at their real place,
    until they are closed: focusing a second terminal does not hide the first,
    it only brings the second on top. Each window is captured on its own.

    Privacy: a window showing a private page (see privacy.py) freezes on its
    last frame and never starts sharing. Showing new content waits SCREEN_DELAY,
    so frames captured before a change are discarded; hiding is immediate.
    """

    SCREEN_KEY = 0   # the whole usable area, when windows cannot be captured one by one

    def __init__(self, recorder: Recorder, on_scene: Callable | None = None):
        self.recorder = recorder
        self.project = recorder.project
        self.captures = recorder.captures
        self.on_scene = on_scene
        self.mode = "auto"
        self.focused: FocusedWindow | None = None   # currently focused window, any
        self.bubble: Rect | None = None             # on-screen bubble, when shown
        self.private = None                         # privacy rule holding the scene, if any
        self.windows: dict[int, FocusedWindow] = {} # presented windows, bottom to top
        self.revealed: set[int] = set()             # past the privacy delay
        self.watched: set[int] = set()              # shared with the focus tracker
        self.scene = "camera"
        self._anim = None
        self._reveals: dict[int, int] = {}
        self._last_note = None
        self.frame = self._target()
        recorder.on_window_lost = self._lost

    @property
    def per_window(self) -> bool:
        """X11 (and test) capture windows one by one; otherwise the whole area is shown."""
        return self.captures.backend == "x11" or self.project.screen.monitor == "test"

    @property
    def area(self) -> Rect | None:
        return self.captures.area or self.captures.monitor

    # ---- composition ------------------------------------------------------

    def _shown(self) -> list[Shown]:
        out = []
        for key, w in self.windows.items():
            if key == self.SCREEN_KEY:
                out.append(Shown(key, self.area, fullscreen=True, visible=key in self.revealed))
            else:
                out.append(Shown(key, w.area, w.shadow, w.fullscreen, key in self.revealed and not w.hidden))
        return out

    def _target(self) -> Frame:
        return compose(self.project, self.scene, self.area, self._shown(),
                       previous=getattr(self, "frame", None), bubble=self.bubble)

    def detail(self) -> str | None:
        if self.private:
            return _("paused, {domain} is private").format(domain=self.private.domain)
        top = self.focused if self.focused and self.focused.xid in self.windows else None
        return top.title if self.scene == "share" and top else None

    def _placement(self, scene: str):
        cam = self.project.camera
        if not cam:
            return None
        return cam.overlay if scene == "share" and cam.overlay else cam.closeup

    # ---- inputs -----------------------------------------------------------

    def focus_changed(self, win: FocusedWindow | None, watched: dict | None = None):
        """From the focus tracker: the active window, and the presented ones."""
        for key, info in (watched or {}).items():
            if key not in self.windows:
                continue
            if info is None:
                self._forget(key)
            else:
                self.windows[key] = info
        self.focused = win
        project_window = (win is not None and not win.hidden
                          and self.project.match_window(win.title, win.wm_class) is not None)
        rule = self.project.private(win.title) if win is not None else None
        if win is not None and win.xid in self.windows:
            self.windows[win.xid] = win
            if rule is not None:
                self._freeze(win.xid)        # this window only; the others keep playing
            elif win.xid not in self.revealed:
                self._reveal_later(win.xid)
        if self.mode == "camera" or (self.mode == "auto" and not project_window):
            self._set_private(None)
            self._go("camera")
            return
        if rule is not None:
            self._set_private(rule)          # hold the scene as it is
            self._update()
            return
        self._set_private(None)
        if project_window:
            self._present(win)
        elif self.mode == "share" and not self.per_window:
            self._present(None)
        self._go("share")

    def set_bubble(self, rect: Rect | None):
        """The webcam overlay follows the on-screen bubble (None: configured position)."""
        if rect != self.bubble:
            self.bubble = rect
            self._update()

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "share" and not self.per_window:
            self._present(None)
        self.focus_changed(self.focused)

    # ---- presentation -----------------------------------------------------

    def _present(self, win: FocusedWindow | None):
        """Bring a window into the presentation (or on top of it)."""
        if self.area is None:
            return
        if not self.per_window or win is None:
            key, source = self.SCREEN_KEY, self.captures.screen_source()
            info = win or FocusedWindow(0, "", "", self.area, True, self.area)
        else:
            key, source, info = win.xid, self.captures.window_source(win), win
        if source is None:
            return
        if key not in self.windows:
            layer = (window_layer(self.project, self.area, info.area, info.shadow, info.fullscreen)
                     if key != self.SCREEN_KEY else None)
            self.recorder.add_window(key, source, layer.crop if layer else None)
            self._reveal_later(key)
        self.windows.pop(key, None)
        self.windows[key] = info          # last = on top
        self.watched.add(key)

    def _forget(self, key: int):
        self.windows.pop(key, None)
        self.revealed.discard(key)
        self.watched.discard(key)
        src = self._reveals.pop(key, None)
        if src:
            GLib.source_remove(src)
        self.recorder.remove_window(key)

    def _lost(self, key: int):
        # Capture failed (window closed or minimized): it can come back when focused again.
        self._forget(key)
        self._update()

    def _freeze(self, key: int):
        src = self._reveals.pop(key, None)
        if src:
            GLib.source_remove(src)
        self.recorder.set_window_frozen(key, True)

    def _reveal_later(self, key: int):
        """Unfreeze after SCREEN_DELAY: frames captured before now never show."""
        if key in self._reveals:
            return
        self.recorder.set_window_frozen(key, True)

        def reveal():
            self._reveals.pop(key, None)
            if key in self.windows:
                self.recorder.set_window_frozen(key, False)
                self.revealed.add(key)
                self._update(animate=True)
            return False

        self._reveals[key] = GLib.timeout_add(int(SCREEN_DELAY * 1000) + 50, reveal)

    # ---- scene ------------------------------------------------------------

    def _set_private(self, rule):
        if rule != self.private:
            self.private = rule
            self._notify()

    def _go(self, scene: str):
        if scene == "share" and (self.area is None or not self.windows):
            scene = "camera" if self.scene == "camera" else self.scene
        changed = scene != self.scene
        self.scene = scene
        self._update(animate=changed, placement=self._placement(scene) if changed else None)

    def _update(self, animate: bool = False, placement=None):
        target = self._target()
        if animate:
            self._animate(target, placement)
        elif self._anim is None:
            self.frame = target
            self.recorder.apply(target)
        else:
            self._anim_target = target   # picked up by the running animation
        self._notify()

    def _notify(self):
        note = (self.scene, self.detail())
        if self.on_scene and note != self._last_note:
            self._last_note = note
            self.on_scene(*note)

    def _animate(self, target: Frame, placement):
        start, t0 = self.frame, time.monotonic()
        duration = max(0.0, self.project.transition)
        self._anim_target = target
        if self._anim:
            GLib.source_remove(self._anim)
            self._anim = None
        self.recorder.apply(self.frame, placement)

        def step():
            t = 1.0 if duration == 0 else min(1.0, (time.monotonic() - t0) / duration)
            goal = self._anim_target
            self.frame = interpolate(start, goal, t) if t < 1 else goal
            self.recorder.apply(self.frame)
            if t >= 1:
                self._anim = None
                return False
            return True

        if step():
            self._anim = GLib.timeout_add(16, step)
