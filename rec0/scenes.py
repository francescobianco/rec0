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
    edge: float = 0.0     # window frame width, in captured (screen) pixels; 0 = none
    radius: float = 0.0   # corner radius of that frame, in captured pixels


@dataclass(frozen=True)
class Frame:
    camera: Layer | None
    screen: Layer | None


FILL_STRETCH_MAX = 0.04   # aspect mismatch the work area may be stretched by
WINDOW_EDGE = 3           # frame around shared windows, canvas pixels
WINDOW_RADIUS = 12        # its corner radius, canvas pixels


def screen_area(project: Project, area: Rect) -> tuple[float, float, float, float]:
    """(sx, sy, ox, oy) mapping the screen's usable area onto the canvas.

    The virtual desktop *is* the work area (screen minus panels and docks): a
    corner of it is a corner of the video. A small aspect mismatch (1920x1048
    under GNOME's top bar) is stretched away; a large one is fitted undistorted.
    """
    m = project.screen.margin
    tw, th = project.width - 2 * m, project.height - 2 * m
    if abs((area.width / area.height) / (tw / th) - 1) <= FILL_STRETCH_MAX:
        return tw / area.width, th / area.height, m, m
    k = min(tw / area.width, th / area.height)
    return k, k, (project.width - area.width * k) / 2, (project.height - area.height * k) / 2


def to_canvas(project: Project, area: Rect, rect: Rect) -> Rect:
    """Map an absolute screen rectangle onto the canvas, as the screen layer draws it."""
    sx, sy, ox, oy = screen_area(project, area)
    return Rect(round(ox + (rect.x - area.x) * sx), round(oy + (rect.y - area.y) * sy),
                max(1, round(rect.width * sx)), max(1, round(rect.height * sy)))


def bubble_rect(bubble: Bubble, area: Rect) -> Rect:
    """Initial absolute position of the on-screen bubble, inside the usable area."""
    d, m, pos = bubble.size, bubble.margin, bubble.position
    x = m if "left" in pos else area.width - d - m if "right" in pos else (area.width - d) // 2
    y = m if pos.startswith("top") else area.height - d - m if pos.startswith("bottom") else (area.height - d) // 2
    return Rect(area.x + x, area.y + y, d, d)


def window_layer(project: Project, monitor: Rect, window: Rect | None, fullscreen: bool = False,
                 area: Rect | None = None) -> Layer:
    """The window, cropped out of the monitor capture, where it is on the real screen.

    Windows get a frame with rounded corners drawn just outside them (the crop is
    widened to make room); fullscreen and maximized ones fill the video bare.
    """
    area = area or monitor
    sx, sy, ox, oy = screen_area(project, area)
    if window is None:
        window = area
    decorated = not fullscreen and window != area
    # Room for the frame outside the window, in captured pixels.
    pad = WINDOW_EDGE / min(sx, sy) if decorated else 0.0
    grow = int(pad + 0.999)
    # Clip to the usable area (and so to the captured monitor).
    x1 = max(window.x - grow, area.x, monitor.x)
    y1 = max(window.y - grow, area.y, monitor.y)
    x2 = min(window.x + window.width + grow, area.x + area.width, monitor.x + monitor.width)
    y2 = min(window.y + window.height + grow, area.y + area.height, monitor.y + monitor.height)
    if x2 - x1 < 2 or y2 - y1 < 2:
        x1, y1, x2, y2 = area.x, area.y, area.x + area.width, area.y + area.height
        decorated, pad = False, 0.0
    crop = (x1 - monitor.x, y1 - monitor.y, monitor.x + monitor.width - x2, monitor.y + monitor.height - y2)
    rect = Rect(round(ox + (x1 - area.x) * sx), round(oy + (y1 - area.y) * sy),
                max(1, round((x2 - x1) * sx)), max(1, round((y2 - y1) * sy)))
    radius = WINDOW_RADIUS / min(sx, sy) if decorated else 0.0
    return Layer(rect, 1.0, crop, pad, radius)


def compose(project: Project, scene: str, monitor: Rect | None, window: Rect | None,
            previous: Frame | None = None, bubble: Rect | None = None, fullscreen: bool = False,
            area: Rect | None = None) -> Frame:
    """`bubble` is the absolute rect of the on-screen bubble, when shown: in the share
    scene the webcam overlay sits exactly on top of it, so it never appears twice."""
    cam = project.camera
    camera = screen = None
    area = area or monitor
    if cam:
        if scene == "camera":
            camera = Layer(cam.closeup.rect, cam.closeup.alpha)
        elif cam.overlay and bubble and monitor:
            camera = Layer(to_canvas(project, area, bubble), cam.overlay.alpha)
        elif cam.overlay:
            camera = Layer(cam.overlay.rect, cam.overlay.alpha)
        else:
            camera = Layer(cam.closeup.rect, 0.0)
    if monitor is not None:
        if scene == "share":
            screen = window_layer(project, monitor, window, fullscreen, area)
        elif previous and previous.screen:
            # Fade out in place rather than jumping somewhere else.
            screen = replace(previous.screen, alpha=0.0)
        else:
            screen = replace(window_layer(project, monitor, None, area=area), alpha=0.0)
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
        keep = lb if lb.alpha > 0 else la
        return Layer(rect, _lerp(la.alpha, lb.alpha, t), keep.crop, keep.edge, keep.radius)

    return Frame(layer(a.camera, b.camera), layer(a.screen, b.screen))
