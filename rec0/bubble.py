"""On-screen webcam bubble.

The bubble runs in its own process with GTK3, because GTK4 cannot keep a window
above the others or refuse focus. The webcam is already owned by the recording
pipeline, so frames are streamed to the bubble through its stdin:

    b"F" + width, height (2 x uint16) + width*height*4 bytes (BGRA)   frame
    b"S" / b"H"                        show / hide
    b"Q"                               quit

The bubble reports its position on stdout ("x y size") when it is moved or
resized, and "size N" once a resize is over (the project keeps it). Hovering it shows a handle that resizes it: in the bottom-right
corner, or on the opposite side when the bubble is near the right or bottom
edge of the screen, so it always has room to grow.
"""

from __future__ import annotations

import math
import os
import queue
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

RING = 0.03   # ring width as a fraction of the diameter, shared with the video mask
OUTLINE = 1.0   # px of dark grey just outside the white ring
DOUBLE_CLICK_MS = 400
MIN_SIZE = 80          # px, smallest bubble the handle allows
MAX_SIZE_RATIO = 0.6   # largest bubble, as a fraction of the screen's short side


def ring_width(d: float) -> float:
    return max(2.0, d * RING)


def draw_ring(cr, d: float):
    """White ring around the webcam circle, with a thin dark outline that keeps it
    visible on light backgrounds (the on-screen bubble and the video share it)."""
    lw = ring_width(d)
    cr.set_line_width(lw)
    cr.set_source_rgba(1, 1, 1, 0.9)
    cr.arc(d / 2, d / 2, d / 2 - OUTLINE - lw / 2, 0, 2 * math.pi)
    cr.stroke()
    cr.set_line_width(OUTLINE)
    cr.set_source_rgba(0.2, 0.2, 0.2, 0.9)
    cr.arc(d / 2, d / 2, d / 2 - OUTLINE / 2, 0, 2 * math.pi)
    cr.stroke()


def handle_corner(x: int, y: int, size: int, area: tuple[int, int, int, int]) -> tuple[int, int]:
    """(sx, sy) of the resize handle: +1 right/bottom, -1 left/top. It sits on the
    bottom-right unless the bubble is too close to that edge of the screen (`area`
    is x, y, width, height) to grow there: then on the opposite side."""
    ax, ay, aw, ah = area
    room = size / 2
    sx = 1 if ax + aw - (x + size) >= room or x - ax < ax + aw - (x + size) else -1
    sy = 1 if ay + ah - (y + size) >= room or y - ay < ay + ah - (y + size) else -1
    return sx, sy


def handle_center(size: int, corner: tuple[int, int]) -> tuple[float, float, float]:
    """(cx, cy, radius) of the handle: on the white ring, in the corner's direction."""
    r = size / 2
    hr = max(6.0, min(9.0, size * 0.035))
    k = (r - OUTLINE - ring_width(size) / 2) / math.sqrt(2)
    return r + corner[0] * k, r + corner[1] * k, hr


def resized(start: tuple[int, int, int], corner: tuple[int, int], dx: float, dy: float,
            area: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """(x, y, size) after dragging the handle by (dx, dy) from `start` (x, y, size):
    the corner opposite the handle stays where it is."""
    x, y, size = start
    sx, sy = corner
    limit = max(MIN_SIZE, int(min(area[2], area[3]) * MAX_SIZE_RATIO))
    new = int(round(size + (dx * sx + dy * sy) / 2))
    new = max(MIN_SIZE, min(limit, new))
    nx = x if sx > 0 else x + size - new
    ny = y if sy > 0 else y + size - new
    return nx, ny, new


# ---------------------------------------------------------------------------
# Controller, used by the main process

class Bubble:
    def __init__(self, size: int, x: int, y: int, on_move: Callable[[int, int, int], None] | None = None,
                 on_activate: Callable[[int], None] | None = None,
                 on_resize: Callable[[int], None] | None = None):
        self.size = size
        self.pos = (x, y)
        self.on_move = on_move
        self.on_activate = on_activate   # (X server time) double click, or focus received
        self.on_resize = on_resize       # (size) the handle was released
        self.visible = False
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._latest: bytes | None = None
        self._wake = threading.Event()

    def start(self):
        from . import config

        # Same identity as the main window: grouped with rec0 in the dock.
        env = dict(os.environ, GDK_BACKEND="x11", REC0_APP_ID=config.APP_ID)
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "rec0.bubble", str(self.size), str(self.pos[0]), str(self.pos[1])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        threading.Thread(target=self._writer, daemon=True, name="rec0-bubble-w").start()
        threading.Thread(target=self._reader, daemon=True, name="rec0-bubble-r").start()

    def frame(self, data: bytes, width: int, height: int):
        """Called from the streaming thread: keep only the most recent frame."""
        if len(data) == width * height * 4:
            self._latest = struct.pack("<HH", width, height) + data
            self._wake.set()

    def show(self):
        if not self.visible:
            self.visible = True
            self._command(b"S")

    def hide(self):
        if self.visible:
            self.visible = False
            self._command(b"H")

    def stop(self):
        self._command(b"Q")
        self._wake.set()
        if self._proc:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def _command(self, cmd: bytes):
        try:
            self._queue.put_nowait(cmd)
            self._wake.set()
        except queue.Full:
            pass

    def _writer(self):
        proc = self._proc
        try:
            while proc.poll() is None:
                self._wake.wait()
                self._wake.clear()
                while not self._queue.empty():
                    cmd = self._queue.get_nowait()
                    proc.stdin.write(cmd)
                    if cmd == b"Q":
                        proc.stdin.close()
                        return
                frame, self._latest = self._latest, None
                if frame is not None and self.visible:
                    proc.stdin.write(b"F" + frame)
                proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _reader(self):
        from gi.repository import GLib

        for line in self._proc.stdout:
            words = line.split()
            if words[:1] == [b"activate"]:
                t = int(words[1]) if len(words) > 1 and words[1].isdigit() else 0
                if self.on_activate:
                    GLib.idle_add(lambda t=t: self.on_activate(t) and False)
                continue
            if words[:1] == [b"size"]:
                if len(words) > 1 and words[1].isdigit() and self.on_resize:
                    GLib.idle_add(lambda s=int(words[1]): self.on_resize(s) and False)
                continue
            try:
                x, y, size = map(int, words)
            except ValueError:
                continue
            self.pos, self.size = (x, y), size
            if self.on_move:
                GLib.idle_add(lambda: self.on_move(x, y, size) and False)


# ---------------------------------------------------------------------------
# Bubble process

def main(argv: list[str]) -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    import cairo
    from gi.repository import Gdk, GLib, Gtk

    size, x, y = (int(v) for v in argv[:3])
    GLib.set_prgname(os.environ.get("REC0_APP_ID", "rec0"))
    state = {"surface": None, "size": size, "hover": False, "corner": (1, 1), "over_handle": False}

    win = Gtk.Window(title="rec0")
    win.set_decorated(False)
    win.set_keep_above(True)
    win.stick()
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_accept_focus(False)      # never steal focus: it would change the scene
    win.set_focus_on_map(False)
    # A dock-type panel: always above normal windows, and the shell neither counts it
    # as one of rec0's windows (the dock activates the main window) nor focuses it.
    win.set_type_hint(Gdk.WindowTypeHint.DOCK)
    win.set_resizable(False)
    win.set_app_paintable(True)
    visual = win.get_screen().get_rgba_visual()
    if visual:
        win.set_visual(visual)
    win.set_size_request(size, size)
    win.move(x, y)
    win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                   | Gdk.EventMask.POINTER_MOTION_MASK
                   | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)

    def work_area() -> tuple[int, int, int, int]:
        """The usable part of the monitor the bubble is on."""
        wx, wy = win.get_position()
        s = state["size"]
        display = win.get_display()
        monitor = display.get_monitor_at_point(wx + s // 2, wy + s // 2) or display.get_primary_monitor()
        r = monitor.get_workarea()
        return r.x, r.y, r.width, r.height

    def on_handle(px: float, py: float) -> bool:
        cx, cy, hr = handle_center(state["size"], state["corner"])
        return (px - cx) ** 2 + (py - cy) ** 2 <= (hr + 4) ** 2   # a little more room than drawn

    def set_cursor(name: str | None):
        gw = win.get_window()
        if gw:
            gw.set_cursor(Gdk.Cursor.new_from_name(win.get_display(), name) if name else None)

    def on_draw(_w, cr):
        size = state["size"]
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.arc(size / 2, size / 2, size / 2, 0, 2 * math.pi)
        cr.save()
        cr.clip()
        surf = state["surface"]
        if surf:
            # Cover: scale the short side to the diameter and centre.
            w, h = surf.get_width(), surf.get_height()
            k = size / min(w, h)
            cr.scale(k, k)
            cr.set_source_surface(surf, (size / k - w) / 2, (size / k - h) / 2)
        else:
            cr.set_source_rgb(0.12, 0.13, 0.19)
        cr.paint()
        cr.restore()
        draw_ring(cr, size)
        if state["hover"] or click.get("resize"):
            cx, cy, hr = handle_center(size, state["corner"])
            cr.arc(cx, cy, hr, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 1 if state["over_handle"] or click.get("resize") else 0.85)
            cr.fill_preserve()
            cr.set_line_width(1.5)
            cr.set_source_rgba(0, 0, 0, 0.35)
            cr.stroke()
        return True

    click = {"press": None, "release": 0, "dragging": False, "resize": None}

    def activate(time: int):
        # The main window comes forward; the click time lets it through focus-stealing prevention.
        print("activate", time, flush=True)

    def resize_to(nx: int, ny: int, new: int):
        state["size"] = new
        win.set_size_request(new, new)
        win.resize(new, new)
        win.move(nx, ny)
        update_input()
        win.queue_draw()

    def update_input():
        # The handle sits on the ring, half outside the circle: clickable only while shown.
        gw = win.get_window()
        if gw:
            shown = state["hover"] or click.get("resize")
            handle = handle_center(state["size"], state["corner"]) if shown else None
            gw.input_shape_combine_region(_circle_region(state["size"], handle), 0, 0)

    def on_enter(_w, _ev):
        if not click["press"]:
            state["corner"] = handle_corner(*win.get_position(), state["size"], work_area())
        state["hover"] = True
        update_input()
        win.queue_draw()
        return False

    def on_leave(_w, ev):
        if ev.detail != Gdk.NotifyType.INFERIOR and not click["resize"]:
            state["hover"] = state["over_handle"] = False
            set_cursor(None)
            update_input()
            win.queue_draw()
        return False

    def on_press(_w, ev):
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS and on_handle(ev.x, ev.y):
            # Resize from the handle: the opposite corner stays put.
            wx, wy = win.get_position()
            click["resize"] = ((wx, wy, state["size"]), ev.x_root, ev.y_root, work_area())
            click["press"] = None
            return True
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS:
            click["press"] = (ev.x_root, ev.y_root, ev.time)
            click["grab"] = (ev.x, ev.y)          # where the bubble was taken
            click["dragging"] = False
        return True

    def on_motion(_w, ev):
        # The window manager does not move dock-type windows: the bubble follows the
        # pointer itself. Only once it really moves: a still click can be a double click.
        if click["resize"]:
            start, px, py, area = click["resize"]
            resize_to(*resized(start, state["corner"], ev.x_root - px, ev.y_root - py, area))
            return True
        over = state["hover"] and on_handle(ev.x, ev.y)
        if over != state["over_handle"]:
            state["over_handle"] = over
            set_cursor(("nwse-resize" if state["corner"][0] == state["corner"][1] else "nesw-resize") if over else None)
            win.queue_draw()
        p = click["press"]
        if not p:
            return True
        if not click["dragging"] and abs(ev.x_root - p[0]) + abs(ev.y_root - p[1]) > 4:
            click["dragging"] = True
        if click["dragging"]:
            gx, gy = click["grab"]
            win.move(int(ev.x_root - gx), int(ev.y_root - gy))
        return True

    def on_release(_w, ev):
        if click["resize"]:
            if click["resize"][0][2] != state["size"]:
                print("size", state["size"], flush=True)
            click["resize"] = None
            s = state["size"]
            state["hover"] = (ev.x - s / 2) ** 2 + (ev.y - s / 2) ** 2 <= (s / 2) ** 2   # released outside: hide
            state["over_handle"] = state["hover"] and on_handle(ev.x, ev.y)
            if not state["over_handle"]:
                set_cursor(None)
            update_input()
            win.queue_draw()
            return True
        if ev.button == 1 and not click["dragging"]:
            if ev.time - click["release"] < DOUBLE_CLICK_MS:
                activate(ev.time)
                click["release"] = 0
            else:
                click["release"] = ev.time
        click["press"] = None
        return True

    def raise_above():
        if win.get_visible() and win.get_window():
            win.get_window().raise_()
        return True

    last = {"pos": None}

    def on_configure(_w, ev):
        pos = tuple(win.get_position())
        if (pos, state["size"]) != (last["pos"], last.get("size")):
            last["pos"], last["size"] = pos, state["size"]
            print(pos[0], pos[1], state["size"], flush=True)
        return False

    win.connect("draw", on_draw)
    win.connect("button-press-event", on_press)
    win.connect("motion-notify-event", on_motion)
    win.connect("button-release-event", on_release)
    win.connect("enter-notify-event", on_enter)
    win.connect("leave-notify-event", on_leave)
    # Should it get focus anyway (e.g. from the dock), hand it to the main window.
    win.connect("focus-in-event", lambda *_: activate(Gtk.get_current_event_time()) or False)
    GLib.timeout_add(1000, raise_above)   # stay above windows that raise themselves
    win.connect("configure-event", on_configure)
    win.connect("realize", lambda w: w.get_window().input_shape_combine_region(_circle_region(size), 0, 0))

    def on_frame(w: int, h: int, data: bytes):
        state["surface"] = cairo.ImageSurface.create_for_data(
            bytearray(data), cairo.FORMAT_ARGB32, w, h, w * 4)
        win.queue_draw()
        return False

    def on_command(cmd: bytes):
        if cmd == b"S":
            win.show_all()
            win.move(*(last["pos"] or (x, y)))
            raise_above()
        elif cmd == b"H":
            win.hide()
        elif cmd == b"Q":
            Gtk.main_quit()
        return False

    def reader():
        stdin = sys.stdin.buffer
        while True:
            kind = stdin.read(1)
            if not kind:
                GLib.idle_add(on_command, b"Q")
                return
            if kind == b"F":
                header = stdin.read(4)
                w, h = struct.unpack("<HH", header) if len(header) == 4 else (0, 0)
                data = stdin.read(w * h * 4)
                if not w or len(data) < w * h * 4:
                    GLib.idle_add(on_command, b"Q")
                    return
                GLib.idle_add(on_frame, w, h, data)
            else:
                GLib.idle_add(on_command, kind)

    threading.Thread(target=reader, daemon=True).start()
    Gtk.main()
    return 0


def _circle_region(size: int, handle: tuple[float, float, float] | None = None):
    """Clicks outside the circle (and the handle, when shown) go through to the window below."""
    import cairo

    region = cairo.Region()
    if handle:
        cx, cy, hr = handle
        hr += 2
        for row in range(max(0, int(cy - hr)), min(size, int(math.ceil(cy + hr)))):
            dy = row + 0.5 - cy
            half = math.sqrt(max(0.0, hr * hr - dy * dy))
            x0, x1 = max(0, int(cx - half)), min(size, int(math.ceil(cx + half)))
            if x1 > x0:
                region.union(cairo.RectangleInt(x0, row, x1 - x0, 1))
    r = size / 2
    for row in range(size):
        dy = row + 0.5 - r
        half = math.sqrt(max(0.0, r * r - dy * dy))
        x0, x1 = int(r - half), int(math.ceil(r + half))
        if x1 > x0:
            region.union(cairo.RectangleInt(x0, row, x1 - x0, 1))
    return region


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
