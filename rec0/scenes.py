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
    # Window layers: videobox margins in captured pixels (positive crops the
    # client-side shadows, negative adds transparent room for the frame).
    crop: tuple[int, int, int, int] | None = None
    edge: float = 0.0     # frame width around the window, captured pixels; 0 = none
    radius: float = 0.0   # corner radius of the frame, captured pixels


@dataclass(frozen=True)
class Frame:
    camera: Layer | None
    windows: tuple[tuple[int, Layer], ...] = ()   # (key, layer), bottom to top


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
    """Map an absolute screen rectangle onto the canvas."""
    sx, sy, ox, oy = screen_area(project, area)
    return Rect(round(ox + (rect.x - area.x) * sx), round(oy + (rect.y - area.y) * sy),
                max(1, round(rect.width * sx)), max(1, round(rect.height * sy)))


def bubble_rect(bubble: Bubble, area: Rect) -> Rect:
    """Initial absolute position of the on-screen bubble, inside the usable area."""
    d, m, pos = bubble.size, bubble.margin, bubble.position
    x = m if "left" in pos else area.width - d - m if "right" in pos else (area.width - d) // 2
    y = m if pos.startswith("top") else area.height - d - m if pos.startswith("bottom") else (area.height - d) // 2
    return Rect(area.x + x, area.y + y, d, d)


def window_layer(project: Project, area: Rect, content: Rect,
                 shadow: tuple[int, int, int, int] = (0, 0, 0, 0), fullscreen: bool = False) -> Layer | None:
    """Layer for a window captured on its own, at its real place on the virtual desktop.

    `content` is the visible window in screen coordinates and `shadow` the
    client-side shadows around it in the capture. Windows get a frame with
    rounded corners just outside them; fullscreen and maximized ones fill bare.
    """
    sx, sy, ox, oy = screen_area(project, area)
    pad = 0.0 if fullscreen else WINDOW_EDGE / min(sx, sy)
    grow = int(pad + 0.999)
    box = Rect(content.x - grow, content.y - grow, content.width + 2 * grow, content.height + 2 * grow)
    x1, y1 = max(box.x, area.x), max(box.y, area.y)
    x2, y2 = min(box.x + box.width, area.x + area.width), min(box.y + box.height, area.y + area.height)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None   # entirely off the usable area
    gl, gt, gr, gb = shadow
    crop = (gl - grow + (x1 - box.x), gt - grow + (y1 - box.y),
            gr - grow + (box.x + box.width - x2), gb - grow + (box.y + box.height - y2))
    rect = Rect(round(ox + (x1 - area.x) * sx), round(oy + (y1 - area.y) * sy),
                max(1, round((x2 - x1) * sx)), max(1, round((y2 - y1) * sy)))
    radius = 0.0 if fullscreen else WINDOW_RADIUS / min(sx, sy)
    return Layer(rect, 1.0, crop, pad, radius)


@dataclass(frozen=True)
class Shown:
    """A window in the presentation, as the scene composer needs it."""
    key: int
    content: Rect
    shadow: tuple[int, int, int, int] = (0, 0, 0, 0)
    fullscreen: bool = False
    visible: bool = True     # revealed (past the privacy delay) and not minimized


def compose(project: Project, scene: str, area: Rect | None, windows: list[Shown],
            previous: Frame | None = None, bubble: Rect | None = None) -> Frame:
    """The frame for `scene`. Windows are bottom to top; in the camera scene they fade
    out where they are. `bubble` is the on-screen bubble's absolute rect, when shown:
    the webcam overlay then sits exactly on top of it, so it never appears twice."""
    cam = project.camera
    camera = None
    if cam:
        if scene == "camera":
            camera = Layer(cam.closeup.rect, cam.closeup.alpha)
        elif cam.overlay and bubble and area:
            camera = Layer(to_canvas(project, area, bubble), cam.overlay.alpha)
        elif cam.overlay:
            camera = Layer(cam.overlay.rect, cam.overlay.alpha)
        else:
            camera = Layer(cam.closeup.rect, 0.0)
    layers = []
    before = dict(previous.windows) if previous else {}
    if area is not None:
        for w in windows:
            layer = window_layer(project, area, w.content, w.shadow, w.fullscreen)
            if layer is None:
                continue
            if scene != "share" or not w.visible:
                layer = replace(before.get(w.key, layer), alpha=0.0)
            layers.append((w.key, layer))
    return Frame(camera, tuple(layers))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def interpolate(a: Frame, b: Frame, t: float) -> Frame:
    """Blend two frames. Crops are not interpolated: the target ones apply at once."""
    t = 1 - (1 - t) ** 3   # ease-out cubic

    def layer(la: Layer | None, lb: Layer | None) -> Layer | None:
        if la is None or lb is None:
            return lb
        # A layer that was invisible appears in its final place, it does not slide in.
        ra = lb.rect if la.alpha == 0 else la.rect
        rect = Rect(*(round(_lerp(p, q, t)) for p, q in zip(
            (ra.x, ra.y, ra.width, ra.height), (lb.rect.x, lb.rect.y, lb.rect.width, lb.rect.height))))
        keep = lb if lb.alpha > 0 else la
        return Layer(rect, _lerp(la.alpha, lb.alpha, t), keep.crop, keep.edge, keep.radius)

    before = dict(a.windows)
    windows = tuple((k, layer(before.get(k, replace(lb, alpha=0.0)), lb)) for k, lb in b.windows)
    return Frame(layer(a.camera, b.camera), windows)
