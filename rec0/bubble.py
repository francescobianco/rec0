"""On-screen webcam bubble.

The bubble runs in its own process with GTK3, because GTK4 cannot keep a window
above the others or refuse focus. The webcam is already owned by the recording
pipeline, so frames are streamed to the bubble through its stdin:

    b"F" + width, height (2 x uint16) + width*height*4 bytes (BGRA)   frame
    b"S" / b"H"                        show / hide
    b"Q"                               quit

The bubble reports its position on stdout ("x y size") when it is moved.
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


def draw_ring(cr, d: float):
    lw = max(2.0, d * RING)
    cr.set_line_width(lw)
    cr.set_source_rgba(1, 1, 1, 0.9)
    cr.arc(d / 2, d / 2, d / 2 - lw / 2, 0, 2 * math.pi)
    cr.stroke()


# ---------------------------------------------------------------------------
# Controller, used by the main process

class Bubble:
    def __init__(self, size: int, x: int, y: int, on_move: Callable[[int, int, int], None] | None = None):
        self.size = size
        self.pos = (x, y)
        self.on_move = on_move
        self.visible = False
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._latest: bytes | None = None
        self._wake = threading.Event()

    def start(self):
        env = dict(os.environ, GDK_BACKEND="x11")
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
            try:
                x, y, size = map(int, line.split())
            except ValueError:
                continue
            self.pos = (x, y)
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
    GLib.set_prgname("rec0-bubble")
    state = {"surface": None}

    win = Gtk.Window(title="rec0 webcam")
    win.set_decorated(False)
    win.set_keep_above(True)
    win.stick()
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_accept_focus(False)      # never steal focus: it would change the scene
    win.set_focus_on_map(False)
    win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
    win.set_resizable(False)
    win.set_app_paintable(True)
    visual = win.get_screen().get_rgba_visual()
    if visual:
        win.set_visual(visual)
    win.set_size_request(size, size)
    win.move(x, y)
    win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)

    def on_draw(_w, cr):
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
        return True

    def on_press(_w, ev):
        if ev.button == 1:
            win.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        return True

    last = {"pos": None}

    def on_configure(_w, ev):
        pos = tuple(win.get_position())
        if pos != last["pos"]:
            last["pos"] = pos
            print(pos[0], pos[1], size, flush=True)
        return False

    win.connect("draw", on_draw)
    win.connect("button-press-event", on_press)
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


def _circle_region(size: int):
    """Clicks outside the circle go through to the window below."""
    import cairo

    region = cairo.Region()
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
