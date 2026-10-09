"""GTK4 + Libadwaita interface: open a project, watch the preview, press REC."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .bubble import Bubble  # noqa: E402
from .capture import CaptureError, Captures, x11_monitors  # noqa: E402
from .focus import FocusTracker  # noqa: E402
from .project import TEMPLATE, ProjectError, Rect, load  # noqa: E402
from .recorder import Director, Recorder  # noqa: E402
from .scenes import SCENE_LABELS, bubble_rect  # noqa: E402

APP_ID = "io.github.rec0"

CSS = b"""
.preview { background: black; border-radius: 12px; }
.rec-button { min-width: 96px; min-height: 96px; border-radius: 999px; font-size: 18px; font-weight: 800; }
.timer { font-size: 28px; font-weight: 700; font-feature-settings: "tnum"; }
.recording-dot { color: @error_color; }
"""


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="rec0", default_width=860, default_height=720)
        self.project = None
        self.captures: Captures | None = None
        self.recorder: Recorder | None = None
        self.director: Director | None = None
        self.tracker: FocusTracker | None = None
        self.monitor: Gio.FileMonitor | None = None
        self._close_after_stop = False
        self._timer = None
        self.bubble: Bubble | None = None

        self.title = Adw.WindowTitle(title="rec0", subtitle="nessun progetto")
        header = Adw.HeaderBar(title_widget=self.title)
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Apri progetto (Ctrl+O)")
        open_btn.set_action_name("win.open")
        header.pack_start(open_btn)
        new_btn = Gtk.Button(icon_name="document-new-symbolic", tooltip_text="Nuovo progetto")
        new_btn.set_action_name("win.new")
        header.pack_start(new_btn)
        self.folder_btn = Gtk.Button(icon_name="folder-videos-symbolic", tooltip_text="Apri la cartella dei video",
                                     sensitive=False)
        self.folder_btn.connect("clicked", lambda *_: self._open_folder())
        header.pack_end(self.folder_btn)

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.stack)

        empty = Adw.StatusPage(icon_name="camera-video-symbolic", title="rec0",
                               description="Apri un progetto YAML per iniziare a registrare.")
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        b = Gtk.Button(label="Apri progetto…", css_classes=["pill", "suggested-action"], action_name="win.open")
        buttons.append(b)
        buttons.append(Gtk.Button(label="Nuovo progetto…", css_classes=["pill"], action_name="win.new"))
        empty.set_child(buttons)
        self.stack.add_named(empty, "empty")
        self.stack.add_named(self._build_main(), "main")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        for name, cb, accel in (("open", self._on_open, "<Control>o"), ("new", self._on_new, None),
                                ("record", lambda *_: self._toggle_record(), "<Control>r")):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accel:
                app.set_accels_for_action(f"win.{name}", [accel])
        self.connect("close-request", self._on_close)
        # While recording, leaving rec0 shows the webcam bubble on screen.
        self.connect("notify::is-active", lambda *_: self._update_bubble())

    def _build_main(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=18, margin_bottom=24, margin_start=24, margin_end=24)

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False, vexpand=True, css_classes=["preview"])
        self.frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.frame.set_child(self.picture)
        box.append(self.frame)

        # Scene mode: automatic (follows focus) or forced.
        modes = Gtk.Box(css_classes=["linked"], halign=Gtk.Align.CENTER)
        self.mode_buttons = {}
        group = None
        for mode, label in (("auto", "Automatica"), ("camera", "Primo piano"), ("share", "Condivisione")):
            btn = Gtk.ToggleButton(label=label, group=group)
            group = group or btn
            btn.connect("toggled", self._on_mode, mode)
            modes.append(btn)
            self.mode_buttons[mode] = btn
        box.append(modes)

        bottom = Gtk.Box(spacing=24)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
        self.status = Gtk.Label(label="Pronto", xalign=0, css_classes=["title-3"])
        self.scene_label = Gtk.Label(xalign=0, css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.END)
        mic = Gtk.Box(spacing=8)
        self.mic_icon = Gtk.Image(icon_name="audio-input-microphone-symbolic")
        self.level = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True, valign=Gtk.Align.CENTER)
        self.level.add_offset_value(Gtk.LEVEL_BAR_OFFSET_LOW, 0.75)
        self.level.add_offset_value(Gtk.LEVEL_BAR_OFFSET_HIGH, 0.9)
        mic.append(self.mic_icon)
        mic.append(self.level)
        info.append(self.status)
        info.append(self.scene_label)
        info.append(mic)
        bottom.append(info)

        self.timer = Gtk.Label(label="00:00:00", css_classes=["timer"], valign=Gtk.Align.CENTER)
        bottom.append(self.timer)
        self.rec_btn = Gtk.Button(label="REC", css_classes=["rec-button", "destructive-action"],
                                  valign=Gtk.Align.CENTER, tooltip_text="Avvia/ferma la registrazione (Ctrl+R)")
        self.rec_btn.connect("clicked", lambda *_: self._toggle_record())
        bottom.append(self.rec_btn)
        box.append(bottom)
        return box

    # ---- project ----------------------------------------------------------

    def load_project(self, path: str | Path):
        if self.recorder and self.recorder.recording:
            self.toast("Ferma la registrazione prima di cambiare progetto")
            return
        try:
            project = load(path)
        except ProjectError as e:
            self._alert("Progetto non valido", "\n".join(f"• {err}" for err in e.errors))
            return
        self._teardown()
        self.project = project
        self.title.set_subtitle(Path(path).name)
        self.frame.set_ratio(project.width / project.height)
        self.folder_btn.set_sensitive(True)
        self.mic_icon.set_visible(bool(project.audio.microphone))
        self.level.set_visible(bool(project.audio.microphone))
        self.stack.set_visible_child_name("main")

        self.captures = Captures(project, log=self.toast)
        self.recorder = Recorder(project, self.captures)
        self.recorder.on_preview = self._on_preview
        self.recorder.on_level = lambda db: self.level.set_value(min(1, max(0, (db + 60) / 60)))
        self.recorder.on_error = lambda msg: self._alert("Errore", msg)
        self.recorder.on_finished = self._on_finished

        auto = self.mode_buttons["auto"]
        auto.set_sensitive(True)
        auto.set_active(True)
        self.mode_buttons["share"].set_sensitive(bool(project.windows))
        self.mode_buttons["camera"].set_sensitive(True)

        self.monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", self._on_file_changed, str(path))

        try:
            if project.launch:
                self.captures.launch_apps()
            self.captures.prepare()
        except CaptureError as e:
            self._alert("Sorgenti non disponibili", str(e))
            return
        self.director = Director(self.recorder, on_scene=self._on_scene)
        if self.captures.follows_focus:
            self.tracker = FocusTracker(self.director.focus_changed)
            self.tracker.start()
        else:
            auto.set_sensitive(False)
            self.mode_buttons["camera"].set_active(True)
            if project.windows:
                self.toast("Cambio scena automatico non disponibile in questa sessione: usa i pulsanti")
        self._on_scene(self.director.scene, None)
        self._start_preview()

    def _on_file_changed(self, _mon, _file, _other, event, path):
        if event == Gio.FileMonitorEvent.CHANGES_DONE_HINT and not (self.recorder and self.recorder.recording):
            self.load_project(path)
            self.toast("Progetto ricaricato")

    def _teardown(self):
        self._stop_bubble()
        if self.tracker:
            self.tracker.stop()
            self.tracker = None
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None
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
            self._alert("Impossibile avviare l'anteprima", str(e))

    def _toggle_record(self):
        rec = self.recorder
        if rec is None or self.director is None:
            return
        if rec.recording:
            rec.stop()
            self.status.set_label("Finalizzazione…")
            self.rec_btn.set_sensitive(False)
            return
        output = self.project.output_path()
        if rec.pipeline:
            rec.stop()   # preview: stops synchronously, releasing the webcam
        try:
            rec.start(self.director.frame, output)
        except (RuntimeError, CaptureError) as e:
            self._alert("Impossibile registrare", str(e))
            self._start_preview()
            return
        self._start_bubble()
        self.status.set_label("● Registrazione")
        self.status.add_css_class("recording-dot")
        self.rec_btn.set_label("STOP")
        self._timer = GLib.timeout_add(250, self._tick)

    def _tick(self):
        if not (self.recorder and self.recorder.recording):
            self._timer = None
            return False
        s = int(self.recorder.position())
        self.timer.set_label(f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}")
        return True

    def _on_finished(self, path):
        if path is None:
            return   # preview stopped
        self._stop_bubble()
        self.status.set_label("Pronto")
        self.status.remove_css_class("recording-dot")
        self.rec_btn.set_label("REC")
        self.rec_btn.set_sensitive(True)
        self.timer.set_label("00:00:00")
        if self._close_after_stop:
            self.close()
            return
        toast = Adw.Toast(title=f"Salvato: {path.name}", button_label="Apri", timeout=8)
        toast.connect("button-clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(path.as_uri(), None))
        self.toasts.add_toast(toast)
        self._start_preview()

    def _start_bubble(self):
        cam = self.project.camera
        if not (cam and cam.bubble):
            return
        monitor = self.captures.monitor or self._primary_monitor()
        r = bubble_rect(cam.bubble, monitor)
        self.bubble = Bubble(r.width, r.x, r.y, on_move=self._on_bubble_moved)
        self.recorder.on_bubble_frame = self.bubble.frame
        self.bubble.start()
        self._update_bubble()

    def _stop_bubble(self):
        if self.bubble:
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

    def _on_preview(self, data: bytes, w: int, h: int):
        tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), w * 4)
        self.picture.set_paintable(tex)

    def _on_scene(self, scene: str, title: str | None):
        text = SCENE_LABELS[scene] + (f" — {title}" if title else "")
        self.scene_label.set_label(f"Scena: {text}")

    def _on_mode(self, btn: Gtk.ToggleButton, mode: str):
        if btn.get_active() and self.director:
            self.director.set_mode(mode)

    # ---- actions ----------------------------------------------------------

    def _on_open(self, *_):
        dialog = Gtk.FileDialog(title="Apri progetto")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="Progetti rec0 (YAML)")
        f.add_pattern("*.yaml")
        f.add_pattern("*.yml")
        filters.append(f)
        dialog.set_filters(filters)
        dialog.open(self, None, self._opened)

    def _opened(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.load_project(file.get_path())

    def _on_new(self, *_):
        dialog = Gtk.FileDialog(title="Nuovo progetto", initial_name="tutorial.yaml")
        dialog.save(self, None, self._saved)

    def _saved(self, dialog, result):
        try:
            path = Path(dialog.save_finish(result).get_path())
        except GLib.Error:
            return
        if path.suffix not in (".yaml", ".yml"):
            path = path.with_suffix(".yaml")
        path.write_text(TEMPLATE.format(name=path.stem))
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        self.load_project(path)

    def _open_folder(self):
        if self.project:
            folder = self.project.output_path().parent
            folder.mkdir(parents=True, exist_ok=True)
            Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)

    def _on_close(self, *_):
        if self.recorder and self.recorder.recording:
            self._close_after_stop = True
            self.recorder.stop()
            self.status.set_label("Finalizzazione…")
            return True
        self._teardown()
        return False

    def toast(self, message: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def _alert(self, heading: str, body: str):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)


class App(Adw.Application):
    def __init__(self, project: str | None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.initial = project

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_data(CSS, len(CSS))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = Window(self)
        win.present()
        if self.initial:
            win.load_project(self.initial)


def run(project: str | None = None) -> int:
    GLib.set_prgname("rec0")
    GLib.set_application_name("rec0")
    return App(project).run([])

