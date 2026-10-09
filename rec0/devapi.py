"""Local development API: drive the running app over HTTP.

Enabled only with `--dev-api` / REC0_DEV_API=1 (what `make start` does). It
listens on 127.0.0.1 and requires a random token; port and token are written
to $XDG_RUNTIME_DIR/rec0-devapi.json, which build-aux/devctl.py reads.

    GET  /state                      app state as JSON
    GET  /log                        recent messages (toasts, errors, saves)
    GET  /screenshot                 PNG of the rec0 window only
    POST /open      {"path": ...}    open a project
    POST /record    {"action": "start"|"stop"|"toggle", "countdown": 0}
    POST /scene     {"mode": "auto"|"camera"|"share"}
    POST /focus     {"title": ..., "wm_class": ..., "rect": [x, y, w, h]}   simulate a focus change
    POST /action    {"name": "app.about", "target": "..."}                 activate any action
    POST /eval      {"code": ...}    run Python on the main loop (app, win, Gtk, Adw, GLib…);
                                     assign `result` to return a value
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import gi

gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gio, GLib, Graphene, Gst, Gtk  # noqa: E402

from .focus import FocusedWindow  # noqa: E402
from .project import Rect  # noqa: E402


def info_file() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or GLib.get_user_runtime_dir()) / "rec0-devapi.json"


class MainThreadError(Exception):
    pass


def on_main(fn, timeout: float = 30):
    """Run fn() on the GLib main loop and return its result."""
    done, box = threading.Event(), {}

    def run():
        try:
            box["value"] = fn()
        except Exception:  # noqa: BLE001 - reported to the client
            box["error"] = traceback.format_exc()
        finally:
            done.set()
        return False

    GLib.idle_add(run)
    if not done.wait(timeout):
        raise MainThreadError("timeout waiting for the main loop")
    if "error" in box:
        raise MainThreadError(box["error"])
    return box.get("value")


class DevApi:
    def __init__(self, app):
        self.app = app
        self.token = secrets.token_urlsafe(16)
        self.server: ThreadingHTTPServer | None = None

    def start(self):
        port = int(os.environ.get("REC0_DEV_API_PORT", "0"))
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                api.handle(self, "GET")

            def do_POST(self):
                api.handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="rec0-devapi").start()
        f = info_file()
        f.write_text(json.dumps({"port": self.server.server_port, "token": self.token, "pid": os.getpid()}))
        f.chmod(0o600)
        print(f"rec0 dev API: http://127.0.0.1:{self.server.server_port} (token in {f})", flush=True)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server = None
        try:
            info_file().unlink()
        except OSError:
            pass

    # ---- dispatch ---------------------------------------------------------

    def handle(self, req: BaseHTTPRequestHandler, method: str):
        if req.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send(req, 401, {"error": "invalid token"})
        route = req.path.split("?")[0].strip("/").replace("-", "_")
        body = {}
        if method == "POST":
            length = int(req.headers.get("Content-Length") or 0)
            try:
                body = json.loads(req.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(req, 400, {"error": "invalid JSON"})
        handler = getattr(self, f"{method.lower()}_{route}", None)
        if handler is None:
            return self._send(req, 404, {"error": f"unknown endpoint {method} /{route}"})
        try:
            result = on_main(lambda: handler(body))
        except MainThreadError as e:
            return self._send(req, 500, {"error": str(e)})
        if isinstance(result, bytes):
            req.send_response(200)
            req.send_header("Content-Type", "image/png")
            req.send_header("Content-Length", str(len(result)))
            req.end_headers()
            req.wfile.write(result)
        else:
            self._send(req, 200, result if result is not None else {"ok": True})

    @staticmethod
    def _send(req, code: int, data):
        payload = json.dumps(data, indent=2, default=str).encode()
        req.send_response(code)
        req.send_header("Content-Type", "application/json")
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    @property
    def win(self):
        win = self.app.window
        if win is None:
            self.app.activate()
            win = self.app.window
        return win

    # ---- endpoints (run on the main loop) ----------------------------------

    def get_state(self, _body):
        win = self.app.window
        if win is None:
            return {"window": None}
        d = win.director
        focused = d.focused if d else None
        return {
            "window": {"active": win.is_active(), "width": win.get_width(), "height": win.get_height(),
                       "page": win.stack.get_visible_child_name()},
            "project": win.project.name if win.project else None,
            "project_path": str(win.project_path) if win.project_path else None,
            "status": win.status.get_label(),
            "recording": win.recording,
            "countdown": win._countdown is not None,
            "position": round(win.recorder.position(), 2) if win.recording else 0,
            "pipeline": win.recorder is not None and win.recorder.pipeline is not None,
            "scene": d.scene if d else None,
            "mode": d.mode if d else None,
            "follows_focus": bool(win.captures and win.captures.follows_focus),
            "focused": {"title": focused.title, "wm_class": focused.wm_class,
                        "rect": list(focused.rect.__dict__.values())} if focused else None,
            "bubble": {"visible": win.bubble.visible, "pos": win.bubble.pos} if win.bubble else None,
            "last_recording": str(win.last_recording) if win.last_recording else None,
            "processing": str(win.processing) if win.processing else None,
            "last_audio_report": {"stages": [s.name for s in win.last_report.stages if s.enabled],
                                  "result": win.last_report.result} if win.last_report else None,
            "settings": {k: win.settings.get_value(k).unpack() if hasattr(win.settings.get_value(k), "unpack")
                         else win.settings.get_value(k)
                         for k in ("countdown", "show-bubble", "reopen-last-project", "last-project")},
            "gstreamer": Gst.version_string(),
            "adwaita": f"{Adw.get_major_version()}.{Adw.get_minor_version()}",
        }

    def get_log(self, _body):
        return {"messages": self.app.messages}

    def get_screenshot(self, _body):
        win = self.win
        w, h = win.get_width(), win.get_height()
        snapshot = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(win).snapshot(snapshot, w, h)
        node = snapshot.to_node()
        if node is None:
            raise RuntimeError("window not rendered yet")
        rect = Graphene.Rect()
        rect.init(0, 0, w, h)
        texture = win.get_renderer().render_texture(node, rect)
        return texture.save_to_png_bytes().get_data()

    def post_open(self, body):
        self.win.load_project(os.path.expanduser(body["path"]))
        self.win.present()
        return self.get_state(None)

    def post_record(self, body):
        win = self.win
        action = body.get("action", "toggle")
        if action == "start":
            win.start_recording(countdown=body.get("countdown", 0))
        elif action == "stop":
            win.stop_recording()
        else:
            win.toggle_record()
        return {"recording": win.recording, "countdown": win._countdown is not None}

    def post_scene(self, body):
        self.win.activate_action("win.scene", GLib.Variant("s", body["mode"]))
        return {"scene": self.win.director.scene if self.win.director else None}

    def post_focus(self, body):
        d = self.win.director
        if d is None:
            raise RuntimeError("no project open")
        rect = Rect(*body.get("rect", (0, 0, 800, 600)))
        win = FocusedWindow(0, body.get("title", ""), body.get("wm_class", ""), rect) if body else None
        d.focus_changed(win)
        return {"scene": d.scene}

    def post_action(self, body):
        name = body["name"]
        group, _, action = name.partition(".")
        target = body.get("target")
        param = GLib.Variant("s", target) if isinstance(target, str) else None
        owner = self.app if group == "app" else self.win
        if owner.lookup_action(action) is None:
            raise RuntimeError(f"unknown action {name}")
        owner.activate_action(action, param)
        return {"activated": name}

    def post_eval(self, body):
        scope = {"app": self.app, "win": self.app.window, "Gtk": Gtk, "Adw": Adw, "GLib": GLib,
                 "Gio": Gio, "Gst": Gst, "result": None}
        exec(compile(body["code"], "<devapi>", "exec"), scope)  # noqa: S102 - local dev tool
        result = scope.get("result")
        try:
            json.dumps(result)
            return {"result": result}
        except TypeError:
            return {"result": repr(result)}
