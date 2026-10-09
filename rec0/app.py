"""GTK4 + Libadwaita interface."""

from __future__ import annotations

import math
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import config, settings  # noqa: E402
from . import audio, postprocess  # noqa: E402
from .bubble import Bubble  # noqa: E402
from .capture import CaptureError, Captures, x11_monitors  # noqa: E402
from .focus import FocusTracker  # noqa: E402
from .i18n import N_, SOURCE_ROOT, _  # noqa: E402
from .project import ProjectError, Rect, load, template  # noqa: E402
from .recorder import Director, Recorder  # noqa: E402
from .scenes import bubble_rect, scene_label  # noqa: E402

CSS = b"""
.preview { background: black; border-radius: 12px;
           /* Drawn over the picture, takes no space: a theme-coloured edge. */
           outline: 1px solid alpha(black, 0.5); outline-offset: -1px; }
.rec-idle { color: @error_color; }
.timer { font-feature-settings: "tnum"; font-weight: 600; }
.recording { color: @error_color; }
.rec-badge { background: rgba(0, 0, 0, 0.6); color: white; border-radius: 6px; padding: 2px 8px;
             font-weight: 700; font-size: 0.85em; }
.countdown { font-size: 120px; font-weight: 800; color: white; text-shadow: 0 2px 12px rgba(0,0,0,0.6); }
"""

SHORTCUTS = (
    (N_("Recording"), (
        (N_("Start or Stop Recording"), "<Control>r"),
        (N_("Automatic Scene"), "<Control>1"),
        (N_("Close-up Scene"), "<Control>2"),
        (N_("Share Scene"), "<Control>3"),
    )),
    (N_("Projects"), (
        (N_("New Project"), "<Control>n"),
        (N_("Open Project"), "<Control>o"),
        (N_("Edit Project File"), "<Control>e"),
    )),
    (N_("General"), (
        (N_("Preferences"), "<Control>comma"),
        (N_("Keyboard Shortcuts"), "<Control>question"),
        (N_("Close Window"), "<Control>w"),
        (N_("Quit"), "<Control>q"),
    )),
)


def _shortcuts_ui() -> str:
    def prop(name, value):
        return f'<property name="{name}">{GLib.markup_escape_text(value)}</property>'

    groups = "".join(
        f'<child><object class="GtkShortcutsGroup">{prop("title", _(title))}'
        + "".join(f'<child><object class="GtkShortcutsShortcut">{prop("title", _(label))}'
                  f'{prop("accelerator", accel)}</object></child>' for label, accel in items)
        + "</object></child>"
        for title, items in SHORTCUTS)
    return ('<interface><object class="GtkShortcutsWindow" id="shortcuts"><property name="modal">True</property>'
            '<child><object class="GtkShortcutsSection"><property name="section-name">shortcuts</property>'
            f"{groups}</object></child></object></interface>")


def _yaml_filter() -> Gio.ListStore:
    filters = Gio.ListStore.new(Gtk.FileFilter)
    f = Gtk.FileFilter(name=_("rec0 Projects"))
    for mime in ("application/yaml", "application/x-yaml"):
        f.add_mime_type(mime)
    f.add_suffix("yaml")
    f.add_suffix("yml")
    filters.append(f)
    return filters


def _pretty_path(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Application):
        super().__init__(application=app, title="rec0")
        self.settings = settings.get()
        self.project = None
        self.project_path: Path | None = None
        self.captures: Captures | None = None
        self.recorder: Recorder | None = None
        self.director: Director | None = None
        self.tracker: FocusTracker | None = None
        self.file_monitor: Gio.FileMonitor | None = None
        self.bubble: Bubble | None = None
        self.last_recording: Path | None = None
        self.last_report = None          # audio.Report of the last processed recording
        self.processing: Path | None = None
        self._close_after_stop = False
        self._timer = None
        self._countdown = None
        self._inhibit_cookie = 0

        if config.PROFILE == "development":
            self.add_css_class("devel")
        self.set_default_size(self.settings.get_int("window-width"), self.settings.get_int("window-height"))
        if self.settings.get_boolean("window-maximized"):
            self.maximize()

        self.title = Adw.WindowTitle(title="rec0")
        header = Adw.HeaderBar(title_widget=self.title)
        header.pack_start(Gtk.Button(label=_("_Open"), use_underline=True, action_name="win.open",
                                     tooltip_text=_("Open a Project")))
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._primary_menu(),
                                       primary=True, tooltip_text=_("Main Menu")))

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.stack)
        self.stack.add_named(self._build_start_page(), "start")
        self.stack.add_named(self._build_main(), "main")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        self._add_actions()
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.connect("close-request", self._on_close)
        self._fitted = False
        # While recording, leaving rec0 shows the webcam bubble on screen.
        self.connect("notify::is-active", lambda *_: self._update_bubble())
        Gtk.RecentManager.get_default().connect("changed", lambda *_: self._fill_recent())

    # ---- construction -----------------------------------------------------

    def _primary_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("_New Project…"), "win.new")
        section.append(_("_Edit Project File"), "win.edit")
        section.append(_("Show _Recordings"), "win.show-recordings")
        section.append(_("Last _Audio Report"), "win.audio-report")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("_Preferences"), "app.preferences")
        section.append(_("_Keyboard Shortcuts"), "win.show-help-overlay")
        section.append(_("_About rec0"), "app.about")
        menu.append_section(None, section)
        return menu

    def _build_start_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name=config.ICON_NAME, title=_("Record a Video"),
                              description=_("A project file describes what to record: open one to start, "
                                            "or create a new one."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(Gtk.Button(label=_("_Open Project…"), use_underline=True,
                                  css_classes=["pill", "suggested-action"], action_name="win.open"))
        buttons.append(Gtk.Button(label=_("_New Project…"), use_underline=True,
                                  css_classes=["pill"], action_name="win.new"))
        box.append(buttons)
        self.recent_group = Adw.PreferencesGroup(title=_("Recent Projects"))
        self.recent_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.recent_group.add(self.recent_list)
        box.append(Adw.Clamp(maximum_size=480, child=self.recent_group))
        page.set_child(box)
        self._fill_recent()
        return page

    def _fill_recent(self):
        self.recent_list.remove_all()
        items = [i for i in Gtk.RecentManager.get_default().get_items()
                 if i.has_application("rec0") and i.get_uri().endswith((".yaml", ".yml")) and i.exists()]
        items.sort(key=lambda i: i.get_modified().to_unix(), reverse=True)
        for item in items[:5]:
            path = Path(Gio.File.new_for_uri(item.get_uri()).get_path())
            row = Adw.ActionRow(title=GLib.markup_escape_text(path.stem),
                                subtitle=GLib.markup_escape_text(_pretty_path(path.parent)), activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, p=path: self.load_project(p))
            self.recent_list.append(row)
        self.recent_group.set_visible(bool(items))

    def _build_main(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Same aspect as the frame: COVER only trims sub-pixel rounding instead of leaving a gap.
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.countdown_label = Gtk.Label(css_classes=["countdown"], visible=False)
        self.rec_badge = Gtk.Label(label="● REC", css_classes=["rec-badge"], visible=False,
                                   halign=Gtk.Align.START, valign=Gtk.Align.START, margin_top=12, margin_start=12)
        overlay = Gtk.Overlay(child=self.picture)
        overlay.add_overlay(self.countdown_label)
        overlay.add_overlay(self.rec_badge)
        self.frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False, vexpand=True, css_classes=["preview"],
                                     margin_top=12, margin_start=12, margin_end=12)
        self.frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.frame.set_child(overlay)
        box.append(self.frame)

        # Media-player style control bar.
        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.rec_btn = Gtk.Button(icon_name="media-record-symbolic", action_name="win.record",
                                  css_classes=["circular", "rec-idle"], valign=Gtk.Align.CENTER,
                                  tooltip_text=_("Start Recording"))
        bar.append(self.rec_btn)
        self.timer = Gtk.Label(label=_clock(0), css_classes=["timer", "dim-label"], margin_start=6)
        bar.append(self.timer)
        self.status = Gtk.Label(label=_("Ready"), xalign=0, css_classes=["heading"], margin_start=12)
        bar.append(self.status)
        self.scene_label = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim-label"],
                                     ellipsize=Pango.EllipsizeMode.END)
        bar.append(self.scene_label)

        modes = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER, margin_end=6)
        self.mode_buttons = {}
        for mode, icon, tip in (("auto", "emblem-synchronizing-symbolic", _("Automatic Scene")),
                                ("camera", "avatar-default-symbolic", _("Close-up Scene")),
                                ("share", "video-display-symbolic", _("Share Scene"))):
            btn = Gtk.ToggleButton(icon_name=icon, tooltip_text=tip, action_name="win.scene",
                                   action_target=GLib.Variant("s", mode))
            modes.append(btn)
            self.mode_buttons[mode] = btn
        bar.append(modes)

        self.camera_btn = Gtk.MenuButton(icon_name="camera-web-symbolic", css_classes=["flat"],
                                         tooltip_text=_("Webcam"), valign=Gtk.Align.CENTER)
        self.camera_btn.set_create_popup_func(lambda b: b.set_menu_model(self._camera_menu()))
        bar.append(self.camera_btn)
        self.mic_btn = Gtk.MenuButton(icon_name="audio-input-microphone-symbolic", css_classes=["flat"],
                                      tooltip_text=_("Microphone"), valign=Gtk.Align.CENTER)
        self.mic_btn.set_create_popup_func(lambda b: b.set_menu_model(self._microphone_menu()))
        bar.append(self.mic_btn)
        self.level = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=64,
                                  tooltip_text=_("Microphone Level"))
        self.level.add_offset_value(Gtk.LEVEL_BAR_OFFSET_LOW, 0.75)
        self.level.add_offset_value(Gtk.LEVEL_BAR_OFFSET_HIGH, 0.9)
        bar.append(self.level)

        box.append(bar)
        return box

    def _camera_menu(self) -> Gio.Menu:
        from .capture import cameras

        menu = Gio.Menu()
        section = Gio.Menu()
        for cam in cameras():
            section.append(cam.name, f"win.camera::{cam.id}")
        menu.append_section(_("Webcam"), section)
        section = Gio.Menu()
        section.append(_("_Mirror"), "win.mirror")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("As Set in the Project"), "win.camera::")
        menu.append_section(None, section)
        return menu

    def _microphone_menu(self) -> Gio.Menu:
        from .capture import microphones

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Default Input"), "win.microphone::default")
        for mic in microphones():
            section.append(mic.name, f"win.microphone::{mic.id}")
        section.append(_("No Audio"), "win.microphone::none")
        menu.append_section(_("Microphone"), section)
        section = Gio.Menu()
        section.append(_("As Set in the Project"), "win.microphone::")
        menu.append_section(None, section)
        return menu

    def _on_mirror(self, action: Gio.SimpleAction, value: GLib.Variant):
        if self.recording or self._countdown:
            return
        action.set_state(value)
        self.settings.set_boolean("mirror-camera", value.get_boolean())
        if self.project_path:
            self.load_project(self.project_path)

    def _on_device(self, action: Gio.SimpleAction, value: GLib.Variant, key: str):
        if self.recording or self._countdown:
            return
        action.set_state(value)
        self.settings.set_string(key, value.get_string())
        if self.project_path:
            self.load_project(self.project_path)

    def _add_actions(self):
        app = self.get_application()
        simple = {
            "open": self._on_open, "new": self._on_new, "edit": self._on_edit,
            "record": lambda *_: self.toggle_record(), "show-recordings": lambda *_: self._open_folder(),
            "close": lambda *_: self.close(), "show-help-overlay": lambda *_: self._show_shortcuts(),
            "open-last-recording": lambda *_: self._open_last_recording(),
            "audio-report": lambda *_: self._show_audio_report(),
        }
        for name, cb in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        scene = Gio.SimpleAction.new_stateful("scene", GLib.VariantType("s"), GLib.Variant("s", "auto"))
        scene.connect("change-state", self._on_scene_mode)
        self.add_action(scene)
        for name, key in (("camera", "camera-device"), ("microphone", "microphone-device")):
            action = Gio.SimpleAction.new_stateful(name, GLib.VariantType("s"),
                                                   GLib.Variant("s", self.settings.get_string(key)))
            action.connect("change-state", self._on_device, key)
            self.add_action(action)
        mirror = Gio.SimpleAction.new_stateful("mirror", None,
                                               GLib.Variant("b", self.settings.get_boolean("mirror-camera")))
        mirror.connect("change-state", self._on_mirror)
        self.add_action(mirror)
        for name in ("record", "edit", "scene", "show-recordings", "camera", "microphone", "mirror"):
            self.lookup_action(name).set_enabled(False)

        for action, accels in {
            "win.open": ["<Control>o"], "win.new": ["<Control>n"], "win.edit": ["<Control>e"],
            "win.record": ["<Control>r"], "win.close": ["<Control>w"],
            "win.show-help-overlay": ["<Control>question"],
            "win.scene::auto": ["<Control>1"], "win.scene::camera": ["<Control>2"], "win.scene::share": ["<Control>3"],
        }.items():
            app.set_accels_for_action(action, accels)

    # ---- project ----------------------------------------------------------

    def load_project(self, path: str | Path):
        path = Path(path)
        if self.recording:
            self.toast(_("Stop recording before changing project"))
            return
        try:
            project = load(path)
        except ProjectError as e:
            self.alert(_("Invalid Project"), "\n".join(f"• {err}" for err in e.errors))
            return
        # Devices chosen in the window take precedence over the project file.
        camera = self.settings.get_string("camera-device")
        if camera and project.camera and project.camera.device != "test":
            project.camera.device = camera
        if project.camera and project.camera.mirror is None:
            project.camera.mirror = self.settings.get_boolean("mirror-camera")
        microphone = self.settings.get_string("microphone-device")
        if microphone and project.audio.microphone != "test":
            project.audio.microphone = None if microphone == "none" else microphone
        self._teardown()
        self.project, self.project_path = project, path
        self.title.set_title(project.name)
        self.title.set_subtitle(_pretty_path(path))
        self.frame.set_ratio(project.width / project.height)
        has_mic = bool(project.audio.microphone)
        self.level.set_visible(has_mic)
        self.level.set_value(0)
        self.stack.set_visible_child_name("main")
        if not self._fitted:
            # Once laid out, size the window so the preview has the video's exact
            # aspect ratio: no black bands around it.
            self._fitted = True
            self.frame.add_tick_callback(self._fit_when_ready)
        for name in ("record", "edit", "scene", "show-recordings", "camera", "microphone"):
            self.lookup_action(name).set_enabled(True)
        self.lookup_action("camera").set_enabled(project.camera is not None)
        self.lookup_action("mirror").set_enabled(project.camera is not None)
        self.settings.set_string("last-project", str(path.resolve()))
        # Registered under the application name ("rec0"), used to list recent projects.
        Gtk.RecentManager.get_default().add_item(path.resolve().as_uri())

        self.file_monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self.file_monitor.connect("changed", self._on_file_changed)

        self.captures = Captures(project, log=self.toast)
        self.recorder = Recorder(project, self.captures)
        self.recorder.on_preview = self._on_preview
        self.recorder.on_level = lambda db: self.level.set_value(min(1, max(0, (db + 60) / 60)))
        self.recorder.on_error = lambda msg: self.alert(_("Recording Error"), msg)
        self.recorder.on_finished = self._on_finished
        try:
            if project.launch:
                self.captures.launch_apps()
            self.captures.prepare()
        except CaptureError as e:
            self.alert(_("Sources Not Available"), str(e))
            return
        self.director = Director(self.recorder, on_scene=self._on_scene)
        self.mode_buttons["share"].set_sensitive(bool(project.windows))
        auto = self.captures.follows_focus
        self.mode_buttons["auto"].set_sensitive(auto)
        if auto:
            self.tracker = FocusTracker(self.director.focus_changed)
            self.tracker.start()
        elif project.windows:
            self.toast(_("Automatic scene switching is not available in this session"))
        self.lookup_action("scene").set_state(GLib.Variant("s", "auto" if auto else "camera"))
        self.director.set_mode("auto" if auto else "camera")
        self._on_scene(self.director.scene, None)
        self._start_preview()

    def _fit_when_ready(self, frame, _clock):
        # Wait until the preview has a real size (the webcam can take a while).
        if frame.get_mapped() and frame.get_height() > 1 and self.get_height() > 1:
            self._fit_to_preview()
            return False
        return True

    def _fit_to_preview(self):
        if self.is_maximized() or self.is_fullscreen() or not self.frame.get_mapped():
            return False
        extra_w = self.get_width() - self.frame.get_width()
        extra_h = self.get_height() - self.frame.get_height()
        # Preview width as a multiple of the aspect ratio's terms (16 for 16:9), so
        # the height is a whole number of pixels: no 1 px line from rounding.
        g = math.gcd(self.project.width, self.project.height)
        rw, rh = self.project.width // g, self.project.height // g
        units = max(1, (self.get_width() - extra_w) // rw)
        width, height = extra_w + units * rw, extra_h + units * rh
        if (width, height) != (self.get_width(), self.get_height()):
            self.set_default_size(width, height)
        return False

    def _on_file_changed(self, _mon, _file, _other, event):
        if event == Gio.FileMonitorEvent.CHANGES_DONE_HINT and not self.recording:
            self.load_project(self.project_path)
            self.toast(_("Project reloaded"))

    def _teardown(self):
        self._stop_bubble()
        if self.tracker:
            self.tracker.stop()
            self.tracker = None
        if self.file_monitor:
            self.file_monitor.cancel()
            self.file_monitor = None
        if self.recorder:
            self.recorder.on_finished = None
            self.recorder.stop()
        if self.captures:
            self.captures.close()
        self.recorder = self.director = self.captures = None

    # ---- preview / recording ---------------------------------------------

    def _start_preview(self):
        try:
            self.recorder.start(self.director.frame)
        except (RuntimeError, CaptureError) as e:
            self.alert(_("Could Not Start the Preview"), str(e))

    @property
    def recording(self) -> bool:
        return bool(self.recorder and self.recorder.recording)

    def toggle_record(self):
        if self._countdown:
            self._cancel_countdown()
        elif self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self, countdown: int | None = None):
        if self.recorder is None or self.recording or self._countdown:
            return
        seconds = self.settings.get_int("countdown") if countdown is None else countdown
        if seconds <= 0:
            self._begin_recording()
            return
        self.rec_btn.set_icon_name("process-stop-symbolic")
        self.rec_btn.set_tooltip_text(_("Cancel"))
        remaining = [seconds]

        def tick():
            if remaining[0] == 0:
                self._countdown = None
                self.countdown_label.set_visible(False)
                self._begin_recording()
                return False
            self.countdown_label.set_label(str(remaining[0]))
            self.countdown_label.set_visible(True)
            remaining[0] -= 1
            return True

        tick()
        self._countdown = GLib.timeout_add_seconds(1, tick)

    def _cancel_countdown(self):
        GLib.source_remove(self._countdown)
        self._countdown = None
        self.countdown_label.set_visible(False)
        self._set_idle_ui()

    def _begin_recording(self):
        rec = self.recorder
        output = self.project.output_path()
        if rec.pipeline:
            rec.stop()   # preview: stops synchronously, releasing the webcam
        try:
            rec.start(self.director.frame, output)
        except (RuntimeError, CaptureError) as e:
            self.alert(_("Could Not Record"), str(e))
            self._set_idle_ui()
            self._start_preview()
            return
        self._inhibit_cookie = self.get_application().inhibit(
            self, Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.IDLE
            | Gtk.ApplicationInhibitFlags.LOGOUT, _("Recording a video"))
        self._start_bubble()
        self.status.set_label(_("Recording"))
        self.status.add_css_class("recording")
        self.timer.remove_css_class("dim-label")
        self.rec_badge.set_visible(True)
        for name in ("camera", "microphone", "mirror"):
            self.lookup_action(name).set_enabled(False)
        self.rec_btn.remove_css_class("rec-idle")
        self.rec_btn.set_icon_name("media-playback-stop-symbolic")
        self.rec_btn.set_tooltip_text(_("Stop Recording"))
        self._timer = GLib.timeout_add(250, self._tick)

    def stop_recording(self):
        if self.recording:
            self.recorder.stop()
            self.status.set_label(_("Saving…"))
            self.rec_btn.set_sensitive(False)

    def _tick(self):
        if not self.recording:
            self._timer = None
            return False
        self.timer.set_label(_clock(self.recorder.position()))
        return True

    def _set_idle_ui(self):
        self.status.set_label(_("Ready"))
        self.status.remove_css_class("recording")
        self.timer.add_css_class("dim-label")
        self.rec_badge.set_visible(False)
        for name in ("camera", "microphone", "mirror"):
            self.lookup_action(name).set_enabled(self.project is not None)
        self.rec_btn.add_css_class("rec-idle")
        self.rec_btn.set_icon_name("media-record-symbolic")
        self.rec_btn.set_tooltip_text(_("Start Recording"))
        self.rec_btn.set_sensitive(True)
        self.timer.set_label(_clock(0))

    def _on_finished(self, path):
        if path is None:
            return   # preview stopped
        self._stop_bubble()
        if self._inhibit_cookie:
            self.get_application().uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0
        self._set_idle_ui()
        self.last_recording = path
        self.get_application().log(f"saved {path}")
        if self._close_after_stop:
            self.close()
            return
        self._start_preview()
        if self.project.audio.processing and self.settings.get_boolean("process-audio") and audio.available():
            self._process_audio(path)
        else:
            self._announce(path)

    def _process_audio(self, path: Path):
        self.processing = path
        self.status.set_label(_("Optimizing audio…"))

        def progress(_step, value):
            if self.processing == path:
                self.status.set_label(_("Optimizing audio… {pct:.0%}").format(pct=value))

        def done(report, error):
            self.processing = None
            self.status.set_label(_("Ready") if not self.recording else _("Recording"))
            if error:
                self.get_application().log(f"audio processing failed: {error}")
                self.toast(_("Audio optimization failed, the original recording was kept: {error}").format(error=error))
            else:
                self.last_report = report
                self.get_application().log("audio: " + "; ".join(
                    f"{s.name} ({s.reason})" for s in report.stages if s.enabled))
            self._announce(path, report)

        postprocess.finalize_async(self.project, path, progress, done)

    def _announce(self, path: Path, report=None):
        Gtk.RecentManager.get_default().add_item(path.as_uri())
        title = _("Saved {name}").format(name=path.name)
        if report:
            title = _("Saved {name}, audio at {lufs:.0f} LUFS").format(
                name=path.name, lufs=report.result["integrated_lufs"])
        toast = Adw.Toast(title=GLib.markup_escape_text(title), button_label=_("_Play"),
                          action_name="win.open-last-recording", timeout=8)
        self.toasts.add_toast(toast)
        if not self.is_active():
            note = Gio.Notification.new(_("Recording Saved"))
            note.set_body(path.name)
            note.set_default_action_and_target("app.play", GLib.Variant("s", str(path)))
            note.add_button_with_target(_("Show in Folder"), "app.show-file", GLib.Variant("s", str(path)))
            self.get_application().send_notification("recording-saved", note)

    def _show_audio_report(self):
        r = self.last_report
        if r is None:
            return
        lines = [("● " if s.enabled else "○ ") + f"{s.name}: {s.reason}" for s in r.stages]
        body = "\n".join(lines) + "\n\n" + _(
            "Result: {lufs:.1f} LUFS, true peak {tp:.1f} dBTP, loudness range {lra:.1f} LU").format(
            lufs=r.result["integrated_lufs"], tp=r.result["true_peak_dbtp"], lra=r.result["loudness_range_lu"])
        self.alert(_("Audio Optimization"), body)

    def _open_last_recording(self):
        if self.last_recording:
            Gtk.FileLauncher(file=Gio.File.new_for_path(str(self.last_recording))).launch(self, None, None)

    def _on_preview(self, data: bytes, w: int, h: int):
        stride = (w * 3 + 3) // 4 * 4   # GStreamer aligns RGB rows to 4 bytes
        tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(data), stride)
        self.picture.set_paintable(tex)

    def _on_scene(self, scene: str, title: str | None):
        self.scene_label.set_label("· " + scene_label(scene) + (f" — {title}" if title else ""))

    def _on_scene_mode(self, action: Gio.SimpleAction, value: GLib.Variant):
        mode = value.get_string()
        if mode == "auto" and not (self.captures and self.captures.follows_focus):
            return
        if mode == "share" and not (self.project and self.project.windows):
            return
        action.set_state(value)
        if self.director:
            self.director.set_mode(mode)

    # ---- bubble -----------------------------------------------------------

    def _start_bubble(self):
        cam = self.project.camera
        if not (cam and cam.bubble and self.settings.get_boolean("show-bubble")):
            return
        monitor = self.captures.monitor or self._primary_monitor()
        r = bubble_rect(cam.bubble, monitor)
        self.bubble = Bubble(r.width, r.x, r.y, on_move=self._on_bubble_moved)
        self.recorder.on_bubble_frame = self.bubble.frame
        self.bubble.start()
        self._update_bubble()

    def _stop_bubble(self):
        if self.bubble:
            if self.recorder:
                self.recorder.on_bubble_frame = None
            self.bubble.stop()
            self.bubble = None
        if self.director:
            self.director.set_bubble(None)

    def _update_bubble(self):
        if not self.bubble:
            return
        if self.is_active():
            self.bubble.hide()
            self.director.set_bubble(None)
        else:
            self.bubble.show()
            self._on_bubble_moved(*self.bubble.pos, self.bubble.size)

    def _on_bubble_moved(self, x: int, y: int, size: int):
        if self.bubble and self.bubble.visible and self.director:
            self.director.set_bubble(Rect(x, y, size, size))

    @staticmethod
    def _primary_monitor() -> Rect:
        mons = x11_monitors()
        m = next((m for m in mons if m["primary"]), mons[0] if mons else None)
        return Rect(m["x"], m["y"], m["width"], m["height"]) if m else Rect(0, 0, 1920, 1080)

    # ---- actions ----------------------------------------------------------

    def _on_open(self, *_args):
        dialog = Gtk.FileDialog(title=_("Open Project"), filters=_yaml_filter(), modal=True)
        dialog.open(self, None, self._opened)

    def _opened(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.load_project(file.get_path())

    def _on_new(self, *_args):
        dialog = Gtk.FileDialog(title=_("New Project"), initial_name=_("tutorial") + ".yaml",
                                filters=_yaml_filter(), modal=True)
        dialog.save(self, None, self._saved)

    def _saved(self, dialog, result):
        try:
            path = Path(dialog.save_finish(result).get_path())
        except GLib.Error:
            return
        if path.suffix not in (".yaml", ".yml"):
            path = path.with_suffix(".yaml")
        path.write_text(template(path.stem))
        self.load_project(path)
        self._on_edit()

    def _on_edit(self, *_args):
        if self.project_path:
            Gtk.FileLauncher(file=Gio.File.new_for_path(str(self.project_path))).launch(self, None, None)

    def _on_drop(self, _target, file: Gio.File, _x, _y):
        if file.get_path() and file.get_path().endswith((".yaml", ".yml")):
            self.load_project(file.get_path())
            return True
        self.toast(_("Only rec0 project files (.yaml) can be opened"))
        return False

    def _open_folder(self):
        if self.project:
            folder = self.project.output_path().parent
            folder.mkdir(parents=True, exist_ok=True)
            Gtk.FileLauncher(file=Gio.File.new_for_path(str(folder))).launch(self, None, None)

    def _show_shortcuts(self):
        builder = Gtk.Builder.new_from_string(_shortcuts_ui(), -1)
        win = builder.get_object("shortcuts")
        win.set_transient_for(self)
        win.present()

    def _on_close(self, *_args):
        if self._countdown:
            self._cancel_countdown()
        if self.recording:
            self._close_after_stop = True
            self.stop_recording()
            return True
        self.settings.set_boolean("window-maximized", self.is_maximized())
        if not self.is_maximized():
            w, h = self.get_default_size()
            self.settings.set_int("window-width", w)
            self.settings.set_int("window-height", h)
        self._teardown()
        return False

    def toast(self, message: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))
        self.get_application().log(message)

    def alert(self, heading: str, body: str):
        self.get_application().log(f"{heading}: {body}")
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", _("_Close"))
        dialog.present(self)


class Application(Adw.Application):
    def __init__(self, dev_api: bool = False):
        super().__init__(application_id=config.APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        # The window class must match the .desktop file for the shell to show our icon.
        GLib.set_prgname(config.APP_ID)
        GLib.set_application_name("rec0")
        self.dev_api = dev_api or bool(os.environ.get("REC0_DEV_API"))
        self.api = None
        self.messages: list[str] = []

    def log(self, message: str):
        self.messages.append(f"{GLib.DateTime.new_now_local().format('%H:%M:%S')} {message}")
        del self.messages[:-200]

    @property
    def window(self) -> Window | None:
        return self.get_active_window() or next(iter(self.get_windows()), None)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(CSS, len(CSS))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if not config.PKGDATADIR:
            # Running from the source tree: use the icons in data/.
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
                str(SOURCE_ROOT / "data" / "icons"))
        for name, cb, ptype in (
            ("quit", lambda *_: self._quit(), None),
            ("about", lambda *_: self._about(), None),
            ("preferences", lambda *_: self._preferences(), None),
            ("play", lambda _a, p: self._launch(p.get_string()), "s"),
            ("show-file", lambda _a, p: self._show_file(p.get_string()), "s"),
        ):
            action = Gio.SimpleAction.new(name, GLib.VariantType(ptype) if ptype else None)
            action.connect("activate", cb)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        if self.dev_api:
            from .devapi import DevApi
            self.api = DevApi(self)
            self.api.start()

    def do_shutdown(self):
        if self.api:
            self.api.stop()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        win = self.window
        if win is None:
            win = Window(self)
            last = win.settings.get_string("last-project")
            if win.settings.get_boolean("reopen-last-project") and last and Path(last).exists():
                win.load_project(last)
        win.present()

    def do_open(self, files, _n, _hint):
        win = self.window or Window(self)
        win.present()
        if files:
            win.load_project(files[0].get_path())

    def _quit(self):
        win = self.window
        if win and win.recording:
            win.close()   # finishes the recording, then closes
        else:
            self.quit()

    def _launch(self, path: str):
        Gtk.FileLauncher(file=Gio.File.new_for_path(path)).launch(self.window, None, None)

    def _show_file(self, path: str):
        Gtk.FileLauncher(file=Gio.File.new_for_path(path)).open_containing_folder(self.window, None, None)

    def _about(self):
        about = Adw.AboutDialog(
            application_name="rec0", application_icon=config.ICON_NAME, version=config.VERSION,
            developer_name="Francesco Bianco", developers=["Francesco Bianco"],
            website=config.WEBSITE, issue_url=config.ISSUE_URL, license_type=Gtk.License.MIT_X11,
            copyright="© 2026 Francesco Bianco",
            comments=_("Project-based video recorder: describe the session in a YAML file, press record."),
            translator_credits=_("translator-credits"),
        )
        about.present(self.window)

    def _preferences(self):
        s = settings.get()
        dialog = Adw.PreferencesDialog()
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("Recording"))
        countdown = Adw.SpinRow.new_with_range(0, 10, 1)
        countdown.set_title(_("Countdown"))
        countdown.set_subtitle(_("Seconds to wait before recording starts"))
        countdown.set_value(s.get_int("countdown"))
        countdown.connect("notify::value", lambda r, _p: s.set_int("countdown", int(r.get_value())))
        group.add(countdown)
        bubble = Adw.SwitchRow(title=_("Webcam Bubble"),
                               subtitle=_("Show your webcam on screen while recording, when rec0 is not focused"),
                               active=s.get_boolean("show-bubble"))
        bubble.connect("notify::active", lambda r, _p: s.set_boolean("show-bubble", r.get_active()))
        group.add(bubble)
        optimize = Adw.SwitchRow(title=_("Optimize Audio"),
                                 subtitle=_("Reduce noise, even out levels and set the loudness for online video "
                                            "after recording; the original is kept"),
                                 active=s.get_boolean("process-audio"))
        optimize.connect("notify::active", lambda r, _p: s.set_boolean("process-audio", r.get_active()))
        group.add(optimize)
        page.add(group)
        group = Adw.PreferencesGroup(title=_("Projects"))
        reopen = Adw.SwitchRow(title=_("Reopen Last Project"), subtitle=_("Open the last used project at startup"),
                               active=s.get_boolean("reopen-last-project"))
        reopen.connect("notify::active", lambda r, _p: s.set_boolean("reopen-last-project", r.get_active()))
        group.add(reopen)
        page.add(group)
        dialog.add(page)
        dialog.present(self.window)


def run(argv: list[str], dev_api: bool = False) -> int:
    """Run the GUI. `argv` are GApplication arguments: project files to open."""
    return Application(dev_api=dev_api).run(["rec0", *argv])
