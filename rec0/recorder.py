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

from .capture import Captures, background_description, find_microphone, q  # noqa: E402
from .focus import FocusedWindow  # noqa: E402
from .project import Project  # noqa: E402
from .project import Rect  # noqa: E402
from .scenes import Frame, compose, interpolate  # noqa: E402

Gst.init(None)

PREVIEW_SIZE = (640, 360)
PREVIEW_FPS = 15
SCREEN_PAD, CAMERA_PAD = "sink_1", "sink_2"
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
        self._crop = None
        self._camcrop = None
        self._camcrop_aspect = None
        self._mix = None
        self._playing = False
        self._mask_size = (0, 0)
        self.circle = False              # draw the webcam as a circle (share overlay)
        self.frozen = True               # screen layer stops updating (privacy)
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

        screen = self.captures.screen_source()
        if screen and frame.screen:
            crop = frame.screen.crop or (0, 0, 0, 0)
            pads.append(f"{SCREEN_PAD}::zorder=1 " + _pad_props(SCREEN_PAD, frame.screen, "stretch"))
            parts.append(
                f"{screen} ! queue max-size-buffers=3 leaky=downstream ! videoconvert ! video/x-raw,format=I420 "
                f"! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 "
                f"min-threshold-time={int(SCREEN_DELAY * Gst.SECOND)} "
                f"! valve name=screenvalve drop={'true' if self.frozen else 'false'} "
                f"! videocrop name=screencrop left={crop[0]} top={crop[1]} right={crop[2]} bottom={crop[3]} "
                # Repeats the last frame while the valve is closed (frozen screen).
                f"! videoscale ! imagefreeze name=screenfreeze is-live=true allow-replace=true "
                f"! {norm} ! mix.{SCREEN_PAD}")

        camera = self.captures.camera_source((cam.closeup.rect.width, cam.closeup.rect.height)) if cam else None
        if camera and frame.camera:
            pl = cam.closeup if frame.camera.rect == cam.closeup.rect or not cam.overlay else cam.overlay
            self._camcrop_aspect = self._aspect(pl)
            self.circle = pl.circle
            pads.append(f"{CAMERA_PAD}::zorder=2 " + _pad_props(CAMERA_PAD, frame.camera, pl.fit))
            parts.append(
                f"{camera} ! queue max-size-buffers=3 leaky=downstream ! videoconvert ! videoscale ! tee name=camt "
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
            f"latency={int((SCREEN_DELAY + 0.1) * Gst.SECOND) if screen else 0} {' '.join(pads)} "
            f"! video/x-raw,format=AYUV,width={p.width},height={p.height},framerate={fps}/1 ! tee name=vt")

        if preview:
            pw, ph = PREVIEW_SIZE
            parts.append(
                f"vt. ! queue max-size-buffers=2 leaky=downstream ! videorate drop-only=true ! videoscale ! videoconvert "
                f"! video/x-raw,format=RGBA,width={pw},height={ph},framerate={PREVIEW_FPS}/1 "
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
        return [f"audiomixer name=amix ! audioconvert ! audio/x-raw,channels=2 ! queue ! {encode or 'fakesink sync=false'}",
                *(f"{i} ! queue ! amix." for i in inputs)]

    # ---- scenes -----------------------------------------------------------

    def apply(self, frame: Frame, placement=None):
        """Move the layers. `placement` (camera Placement) updates fit/crop of the webcam."""
        self.frame = frame
        if not self.pipeline:
            return
        if frame.screen and self._mix.get_static_pad(SCREEN_PAD):
            self._set_pad(SCREEN_PAD, frame.screen)
            # Changing the crop while the source is still negotiating fails with
            # not-negotiated: wait for PLAYING (see _on_message).
            if frame.screen.crop and self._crop and self._playing:
                for name, value in zip(("left", "top", "right", "bottom"), frame.screen.crop):
                    if self._crop.get_property(name) != value:
                        self._crop.set_property(name, value)
        if frame.camera and self._mix.get_static_pad(CAMERA_PAD):
            pad = self._set_pad(CAMERA_PAD, frame.camera)
            if placement is not None:
                self.circle = placement.circle
                pad.set_property("sizing-policy", 0 if placement.fit == "stretch" else 1)
                aspect = self._aspect(placement)
                if self._camcrop and aspect != self._camcrop_aspect:
                    Gst.util_set_object_arg(self._camcrop, "aspect-ratio", aspect)
                    self._camcrop_aspect = aspect

    def set_screen_frozen(self, frozen: bool):
        """Freeze the screen layer on its last frame (the valve drops new ones)."""
        self.frozen = frozen
        valve = self.pipeline.get_by_name("screenvalve") if self.pipeline else None
        if valve is not None:
            valve.set_property("drop", frozen)

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
        self._crop = self.pipeline.get_by_name("screencrop")
        self._camcrop = self.pipeline.get_by_name("camcrop")

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
            # A closed valve drops EOS too: end the screen branch past it.
            screenfreeze = self.pipeline.get_by_name("screenfreeze")
            if screenfreeze is not None:
                screenfreeze.get_static_pad("src").push_event(Gst.Event.new_eos())
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
        output = self.output if self.recording else None
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
        self._mix = self._crop = self._camcrop = None
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
        elif t == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            name = msg.src.get_name() if msg.src else "?"
            self._emit(self.on_error, f"{name}: {err.message}")
            if self.recording:
                # The failing branch can no longer carry EOS: push it straight into
                # the muxer so what was recorded so far is still a valid file.
                self._stopping = True
                mux = self.pipeline.get_by_name("mux")
                for pad in mux.sinkpads:
                    pad.send_event(Gst.Event.new_eos())
                GLib.timeout_add_seconds(5, self._force_stop)
            else:
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
    """Decides the scene and animates the layers between scenes.

    mode "auto" follows window focus; "camera" / "share" force a scene.
    on_scene(scene, detail|None) is called when the scene or its subject changes.

    Privacy: a project window showing a private page (see privacy.py) never
    starts the share scene; if sharing is already on, the screen layer freezes
    on its last frame. Hiding is immediate, while showing new screen content
    waits SCREEN_DELAY so frames captured before the switch are discarded.
    """

    def __init__(self, recorder: Recorder, on_scene: Callable | None = None):
        self.recorder = recorder
        self.project = recorder.project
        self.on_scene = on_scene
        self.mode = "auto"
        self.window: FocusedWindow | None = None    # project window shown in the share scene
        self.focused: FocusedWindow | None = None   # currently focused window, any
        self.bubble: Rect | None = None             # on-screen bubble, when shown
        self.private = None                         # privacy rule holding the scene, if any
        # Start on the camera scene: with no camera it is an empty virtual desktop,
        # never an unintended view of the screen.
        self.scene = "camera"
        self._anim = None
        self._reveal = None
        self.frame = self._target(self.scene)

    @property
    def monitor(self):
        return self.recorder.captures.monitor

    def _target(self, scene: str) -> Frame:
        rect = self.window.rect if (scene == "share" and self.window) else None
        return compose(self.project, scene, self.monitor, rect, previous=getattr(self, "frame", None),
                       bubble=self.bubble)

    def _placement(self, scene: str):
        cam = self.project.camera
        if not cam:
            return None
        return cam.overlay if scene == "share" and cam.overlay else cam.closeup

    def detail(self) -> str | None:
        if self.private:
            return _("paused, {domain} is private").format(domain=self.private.domain)
        return self.window.title if self.scene == "share" and self.window else None

    def focus_changed(self, win: FocusedWindow | None):
        self.focused = win
        project_window = win is not None and self.project.match_window(win.title, win.wm_class) is not None
        rule = self.project.private(win.title) if win is not None else None
        if self.mode == "camera" or (self.mode == "auto" and not project_window):
            self._set_private(None)
            self._go("camera")
        elif rule is not None:
            self._hold(rule)
        elif self.mode == "share" and not project_window:
            self._set_private(None)
            self._go("share", reveal=self.recorder.frozen)
        else:
            new = self.window is None or (win.xid, win.title) != (self.window.xid, self.window.title)
            self.window = win
            self._set_private(None)
            self._go("share", reveal=new or self.recorder.frozen or self.scene != "share")

    def set_bubble(self, rect: Rect | None):
        """The webcam overlay follows the on-screen bubble (None: configured position)."""
        if rect != self.bubble:
            self.bubble = rect
            if self._reveal is None:
                self._go(self.scene)

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "auto" or self.focused is not None:
            self.focus_changed(self.focused)
        else:
            self._go(mode, reveal=mode == "share")

    def _set_private(self, rule):
        if rule != self.private:
            self.private = rule
            self._notify()

    def _hold(self, rule):
        """Keep the current scene; never show what is on screen now."""
        self._cancel_reveal()
        self.recorder.set_screen_frozen(True)
        self._set_private(rule)

    def _cancel_reveal(self):
        if self._reveal:
            GLib.source_remove(self._reveal)
            self._reveal = None

    def _go(self, scene: str, reveal: bool = False):
        if self.monitor is None and scene == "share":
            return
        self._cancel_reveal()
        if scene == "share" and reveal:
            self.recorder.set_screen_frozen(True)

            def show():
                self._reveal = None
                self.recorder.set_screen_frozen(False)
                self._switch(scene)
                return False

            self._reveal = GLib.timeout_add(int(SCREEN_DELAY * 1000) + 50, show)
            return
        self._switch(scene)

    def _switch(self, scene: str):
        target = self._target(scene)
        changed = scene != self.scene
        self.scene = scene
        if changed:
            self._animate(target, self._placement(scene))
            self._notify()
        else:
            if target != self.frame and self._anim is None:
                # Same scene, window moved/resized or another tab: follow it immediately.
                self.frame = target
                self.recorder.apply(target)
            self._notify()

    def _notify(self):
        note = (self.scene, self.detail())
        if self.on_scene and note != getattr(self, "_last_note", None):
            self._last_note = note
            self.on_scene(*note)

    def _animate(self, target: Frame, placement):
        start, t0 = self.frame, time.monotonic()
        duration = max(0.0, self.project.transition)
        if self._anim:
            GLib.source_remove(self._anim)
            self._anim = None
        self.recorder.apply(self.frame, placement)

        def step():
            t = 1.0 if duration == 0 else min(1.0, (time.monotonic() - t0) / duration)
            self.frame = interpolate(start, target, t) if t < 1 else target
            self.recorder.apply(self.frame)
            if t >= 1:
                self._anim = None
                return False
            return True

        if step():
            self._anim = GLib.timeout_add(16, step)
