"""Scene geometry: where each layer goes on the virtual desktop.

Pure functions, no GStreamer: easy to test and to animate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .i18n import _
from .project import Bubble, Project, Rect


def scene_label(scene: str) -> str:
    return {"camera": _("Close-up"), "share": _("Share")}[scene]


@dataclass(frozen=True)
class Layer:
    rect: Rect
    alpha: float
    crop: tuple[int, int, int, int] | None = None   # left, top, right, bottom (screen pixels)


@dataclass(frozen=True)
class Frame:
    camera: Layer | None
    screen: Layer | None


def screen_area(project: Project, monitor: Rect) -> tuple[float, float, float]:
    """Scale and offset mapping real-screen coordinates onto the canvas."""
    m = project.screen.margin
    s = min((project.width - 2 * m) / monitor.width, (project.height - 2 * m) / monitor.height)
    ox = (project.width - monitor.width * s) / 2
    oy = (project.height - monitor.height * s) / 2
    return s, ox, oy


def to_canvas(project: Project, monitor: Rect, rect: Rect) -> Rect:
    """Map an absolute screen rectangle onto the canvas, as the screen layer draws it."""
    s, ox, oy = screen_area(project, monitor)
    return Rect(round(ox + (rect.x - monitor.x) * s), round(oy + (rect.y - monitor.y) * s),
                max(1, round(rect.width * s)), max(1, round(rect.height * s)))


def bubble_rect(bubble: Bubble, monitor: Rect) -> Rect:
    """Initial absolute position of the on-screen bubble."""
    d, m, pos = bubble.size, bubble.margin, bubble.position
    x = m if "left" in pos else monitor.width - d - m if "right" in pos else (monitor.width - d) // 2
    y = m if pos.startswith("top") else monitor.height - d - m if pos.startswith("bottom") else (monitor.height - d) // 2
    return Rect(monitor.x + x, monitor.y + y, d, d)


FILL_STRETCH_MAX = 0.04   # aspect mismatch a fullscreen window may be stretched by


def window_layer(project: Project, monitor: Rect, window: Rect | None, fullscreen: bool = False) -> Layer:
    """The window, cropped out of the monitor capture, at its real position.

    A fullscreen (or maximized) window fills the whole canvas instead: what fills
    the real screen fills the video. A small aspect mismatch (a maximized window
    under the top bar) is stretched away, a larger one is fitted without distortion.
    """
    s, ox, oy = screen_area(project, monitor)
    if window is None:
        window = monitor
    # Clip to the captured monitor.
    x1 = max(window.x, monitor.x)
    y1 = max(window.y, monitor.y)
    x2 = min(window.x + window.width, monitor.x + monitor.width)
    y2 = min(window.y + window.height, monitor.y + monitor.height)
    if x2 - x1 < 2 or y2 - y1 < 2:
        x1, y1, x2, y2 = monitor.x, monitor.y, monitor.x + monitor.width, monitor.y + monitor.height
    crop = (x1 - monitor.x, y1 - monitor.y, monitor.x + monitor.width - x2, monitor.y + monitor.height - y2)
    if fullscreen:
        cw, ch = project.width, project.height
        ww, wh = x2 - x1, y2 - y1
        if abs((ww / wh) / (cw / ch) - 1) <= FILL_STRETCH_MAX:
            return Layer(Rect(0, 0, cw, ch), 1.0, crop)
        k = min(cw / ww, ch / wh)
        w, h = round(ww * k), round(wh * k)
        return Layer(Rect((cw - w) // 2, (ch - h) // 2, w, h), 1.0, crop)
    rect = Rect(round(ox + (x1 - monitor.x) * s), round(oy + (y1 - monitor.y) * s),
                max(1, round((x2 - x1) * s)), max(1, round((y2 - y1) * s)))
    return Layer(rect, 1.0, crop)


def compose(project: Project, scene: str, monitor: Rect | None, window: Rect | None,
            previous: Frame | None = None, bubble: Rect | None = None, fullscreen: bool = False) -> Frame:
    """`bubble` is the absolute rect of the on-screen bubble, when shown: in the share
    scene the webcam overlay sits exactly on top of it, so it never appears twice."""
    cam = project.camera
    camera = screen = None
    if cam:
        if scene == "camera":
            camera = Layer(cam.closeup.rect, cam.closeup.alpha)
        elif cam.overlay and bubble and monitor:
            camera = Layer(to_canvas(project, monitor, bubble), cam.overlay.alpha)
        elif cam.overlay:
            camera = Layer(cam.overlay.rect, cam.overlay.alpha)
        else:
            camera = Layer(cam.closeup.rect, 0.0)
    if monitor is not None:
        if scene == "share":
            screen = window_layer(project, monitor, window, fullscreen)
        elif previous and previous.screen:
            # Fade out in place rather than jumping somewhere else.
            screen = replace(previous.screen, alpha=0.0)
        else:
            screen = replace(window_layer(project, monitor, None), alpha=0.0)
    return Frame(camera, screen)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def interpolate(a: Frame, b: Frame, t: float) -> Frame:
    """Blend two frames. Crop is not interpolated: the target crop applies immediately."""
    t = 1 - (1 - t) ** 3   # ease-out cubic

    def layer(la: Layer | None, lb: Layer | None) -> Layer | None:
        if la is None or lb is None:
            return lb
        # A window that was invisible should appear in its final place, not slide in.
        ra = lb.rect if la.alpha == 0 else la.rect
        rect = Rect(*(round(_lerp(p, q, t)) for p, q in zip(
            (ra.x, ra.y, ra.width, ra.height), (lb.rect.x, lb.rect.y, lb.rect.width, lb.rect.height))))
        return Layer(rect, _lerp(la.alpha, lb.alpha, t), lb.crop if lb.alpha > 0 else la.crop)

    return Frame(layer(a.camera, b.camera), layer(a.screen, b.screen))
