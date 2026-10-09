"""Load and validate rec0 project files (YAML).

A rec0 video is always composed the same way: a virtual desktop (the
background) on which one of two predefined scenes is drawn:

* camera  - close-up of the webcam. Active while rec0 itself, or any window
            that is not part of the project, has focus.
* share   - the focused project window, drawn on the virtual desktop at the
            same place and size it has on the real screen, with an optional
            small webcam overlay.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from . import privacy
from .i18n import N_, _, pkgdata

POSITIONS = (
    "top-left", "top", "top-right",
    "left", "center", "right",
    "bottom-left", "bottom", "bottom-right",
)
FITS = ("contain", "cover", "stretch")
FORMATS = ("mp4", "mkv")
SCENES = ("camera", "share")
RESOLUTION_PRESETS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k": (3840, 2160),
}
DEFAULT_MARGIN = 24
DEFAULT_BACKGROUND = pkgdata("background.jpg")


class ProjectError(Exception):
    """Raised when a project file is invalid. `errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass
class Placement:
    rect: Rect
    fit: str = "cover"
    alpha: float = 1.0
    circle: bool = False


@dataclass
class Camera:
    device: str = "default"          # "default", /dev/videoN, name substring, or "test"
    capture_size: tuple[int, int] | None = None
    mirror: bool | None = None        # None: the user's preference (on by default)
    closeup: Placement | None = None  # camera scene
    overlay: Placement | None = None  # share scene (None = hidden)
    bubble: Bubble | None = None      # on-screen webcam bubble while recording


@dataclass
class Bubble:
    """Round webcam window shown on the real screen while recording, whenever
    focus leaves rec0. In the share scene the video overlay follows it."""
    size: int = 200                   # diameter in screen pixels
    position: str = "bottom-right"    # initial position on the monitor
    margin: int = 40


@dataclass
class Screen:
    monitor: str = "primary"         # "primary", index, connector name, or "test"
    margin: int = 0                  # border of virtual desktop around the real screen
    cursor: bool = True
    frame: str = "accent"            # frame around shared windows: accent, #rrggbb or none


@dataclass
class Window:
    match: str                       # substring of title or WM_CLASS (case-insensitive)
    name: str = ""


@dataclass
class Audio:
    microphone: str | None = "default"   # None = disabled, "test" = test tone
    desktop: bool = False
    microphone_volume: float = 1.0
    desktop_volume: float = 1.0
    processing: bool = True             # adaptive post-processing after recording (audio.py)
    target: str = "youtube"             # loudness target: youtube, podcast, broadcast
    keep_original: bool = True          # keep the unprocessed recording next to the result


@dataclass
class Output:
    directory: str = ""         # empty: the XDG Videos folder, in a "rec0" subfolder
    filename: str = "{project}-{timestamp}.{format}"
    format: str = "mp4"
    video_bitrate: int = 6000   # kbit/s
    audio_bitrate: int = 192    # kbit/s


@dataclass
class Launch:
    command: str
    cwd: str | None = None
    delay: float = 0.0


@dataclass
class Project:
    name: str
    path: Path | None
    width: int
    height: int
    fps: int
    background: str                  # "black", "white", "#RRGGBB" or image path
    camera: Camera | None
    screen: Screen
    windows: list[Window]
    audio: Audio
    output: Output
    transition: float = 0.3          # seconds
    launch: list[Launch] = field(default_factory=list)
    privacy: tuple[privacy.Rule, ...] = privacy.BUILTIN

    def private(self, title: str) -> privacy.Rule | None:
        """The privacy rule hiding a window with this title, if any."""
        return privacy.check(self.privacy, title)

    def output_path(self, now: datetime | None = None) -> Path:
        now = now or datetime.now()
        name = self.output.filename.format(
            project=self.name,
            timestamp=now.strftime("%Y%m%d-%H%M%S"),
            date=now.strftime("%Y-%m-%d"),
            format=self.output.format,
        )
        if not name.endswith("." + self.output.format):
            name += "." + self.output.format
        return output_directory(self.output.directory) / name

    def match_window(self, title: str, wm_class: str) -> Window | None:
        t, c = title.casefold(), wm_class.casefold()
        for w in self.windows:
            needle = w.match.casefold()
            if needle in t or needle in c:
                return w
        return None


def output_directory(directory: str) -> Path:
    if directory:
        return Path(os.path.expandvars(os.path.expanduser(directory)))
    from gi.repository import GLib

    videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    return Path(videos or Path.home() / "Videos") / "rec0"


def load(path: str | os.PathLike) -> Project:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ProjectError([_("cannot read {path}: {error}").format(path=path, error=e.strerror)]) from e
    except yaml.YAMLError as e:
        raise ProjectError([_("invalid YAML: {error}").format(error=e)]) from e
    return parse(data, path)


def parse(data, path: Path | None = None) -> Project:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ProjectError([_("the project file must be a YAML mapping")])

    base = path.parent if path else Path.cwd()
    name = str(data.get("project") or (path.stem if path else "rec0"))

    video = _mapping(data.get("video"), "video", errors)
    width, height = _resolution(video.get("resolution", "1920x1080"), "video.resolution", errors)
    canvas = (width, height)
    fps = _int(video.get("fps", 30), "video.fps", errors, minimum=1, maximum=240)
    transition = _float(video.get("transition", 0.3), "video.transition", errors)

    default_bg = str(DEFAULT_BACKGROUND) if DEFAULT_BACKGROUND.exists() else "black"
    background = str(data.get("background", video.get("background", default_bg)))
    if _color(background) is None and background not in ("black", "white"):
        bg_path = _resolve(background, base)
        if not bg_path.exists():
            errors.append(_("background: '{value}' is neither a color (#RRGGBB) nor an existing image").format(value=background))
        background = str(bg_path)

    camera = _camera(data.get("camera", {}), canvas, errors)

    raw_screen = _mapping(data.get("screen"), "screen", errors)
    screen = Screen(
        monitor=str(raw_screen.get("monitor", "primary")),
        margin=_int(raw_screen.get("margin", 0), "screen.margin", errors, minimum=0),
        cursor=bool(raw_screen.get("cursor", True)),
        frame="none" if raw_screen.get("frame", "accent") in (False, None) else str(raw_screen.get("frame", "accent")),
    )
    if screen.frame not in ("accent", "none") and _color(screen.frame) is None:
        errors.append(_("{where}: must be 'accent', a color (#RRGGBB) or false").format(where="screen.frame"))
    if screen.margin * 2 >= min(width, height):
        errors.append(_("screen.margin: too large for the resolution"))

    windows = []
    raw_windows = data.get("windows") or []
    if not isinstance(raw_windows, list):
        errors.append(_("{where}: must be a list").format(where="windows"))
        raw_windows = []
    for i, w in enumerate(raw_windows):
        if isinstance(w, str) and w:
            windows.append(Window(match=w, name=w))
        elif isinstance(w, dict) and w.get("match"):
            windows.append(Window(match=str(w["match"]), name=str(w.get("name", w["match"]))))
        else:
            errors.append(_("{where}: must be a string or a mapping with '{key}'").format(where=f"windows[{i}]", key="match"))
    if not windows and camera is None:
        errors.append(_("nothing to record: configure 'camera' and/or at least one entry in 'windows'"))

    audio = _audio(_mapping(data.get("audio"), "audio", errors), errors)
    output = _output(_mapping(data.get("output"), "output", errors), errors)
    launch = _launch(data.get("launch") or [], base, errors)
    rules = _privacy(_mapping(data.get("privacy"), "privacy", errors), errors)

    if errors:
        raise ProjectError(errors)

    return Project(
        name=name, path=path, width=width, height=height, fps=fps, background=background,
        camera=camera, screen=screen, windows=windows, audio=audio, output=output,
        transition=transition, launch=launch, privacy=rules,
    )


def _camera(raw, canvas, errors) -> Camera | None:
    if raw is False or raw is None:
        return None
    if isinstance(raw, str):
        raw = {"device": raw}
    if not isinstance(raw, dict):
        errors.append(_("camera: must be a mapping, a device name or false"))
        return None
    cam = Camera(device=str(raw.get("device", "default")))
    if "mirror" in raw:
        cam.mirror = bool(raw["mirror"])
    if "capture" in raw:
        cam.capture_size = _resolution(raw["capture"], "camera.capture", errors)
    closeup = raw.get("closeup", "fullscreen")
    cam.closeup = _placement(closeup, canvas, "camera.closeup", errors) if closeup else None
    if cam.closeup is None:
        errors.append(_("camera.closeup: the close-up scene needs a placement"))
    overlay = raw.get("overlay", {"position": "bottom-right", "width": 240})
    if isinstance(overlay, str) and overlay != "fullscreen":
        overlay = {"position": overlay}
    if overlay:
        shape = overlay.get("shape", "circle") if isinstance(overlay, dict) else "rect"
        if shape not in ("circle", "rect"):
            errors.append(_("{where}: must be one of {values}").format(where="camera.overlay.shape", values="circle, rect"))
        if shape == "circle" and isinstance(overlay, dict) and "height" not in overlay:
            overlay = {**overlay, "height": overlay.get("width", 240)}
        cam.overlay = _placement(overlay, canvas, "camera.overlay", errors)
        if cam.overlay and shape == "circle":
            cam.overlay.circle = True
    bubble = raw.get("bubble", {})
    if bubble is True:
        bubble = {}
    if isinstance(bubble, dict):
        cam.bubble = Bubble(
            size=_int(bubble.get("size", 200), "camera.bubble.size", errors, minimum=48, maximum=1000),
            position=str(bubble.get("position", "bottom-right")),
            margin=_int(bubble.get("margin", 40), "camera.bubble.margin", errors, minimum=0),
        )
        if cam.bubble.position not in POSITIONS:
            errors.append(_("{where}: must be one of {values}").format(where="camera.bubble.position", values=", ".join(POSITIONS)))
    elif bubble is not False:
        errors.append(_("camera.bubble: must be a mapping or false"))
    return cam


def _placement(spec, canvas, where: str, errors: list[str]) -> Placement | None:
    cw, ch = canvas
    if spec == "fullscreen":
        spec = {"fullscreen": True}
    elif isinstance(spec, str):
        spec = {"position": spec}
    if not isinstance(spec, dict):
        errors.append(_("{where}: must be 'fullscreen', a position or a mapping").format(where=where))
        return None

    fit = spec.get("fit", "cover")
    if fit not in FITS:
        errors.append(_("{where}: must be one of {values}").format(where=f"{where}.fit", values=", ".join(FITS)))
        fit = "cover"
    alpha = _float(spec.get("alpha", 1.0), f"{where}.alpha", errors)
    if not 0 <= alpha <= 1:
        errors.append(_("{where}: must be between 0 and 1").format(where=f"{where}.alpha"))
    if spec.get("fullscreen"):
        return Placement(Rect(0, 0, cw, ch), fit, alpha)

    w = _dimension(spec.get("width"), cw, f"{where}.width", errors)
    h = _dimension(spec.get("height"), ch, f"{where}.height", errors)
    if w is None and h is None:
        w = cw // 4
    if h is None:
        h = round(w * 9 / 16)
    if w is None:
        w = round(h * 16 / 9)

    margin = _int(spec.get("margin", DEFAULT_MARGIN), f"{where}.margin", errors, minimum=0)
    if "x" in spec or "y" in spec:
        x = _int(spec.get("x", 0), f"{where}.x", errors)
        y = _int(spec.get("y", 0), f"{where}.y", errors)
    else:
        pos = spec.get("position", "center")
        if pos not in POSITIONS:
            errors.append(_("{where}: must be one of {values}").format(where=f"{where}.position", values=", ".join(POSITIONS)))
            pos = "center"
        x = margin if "left" in pos else cw - w - margin if "right" in pos else (cw - w) // 2
        y = margin if pos.startswith("top") else ch - h - margin if pos.startswith("bottom") else (ch - h) // 2
    return Placement(Rect(x, y, w, h), fit, alpha)


def _audio(raw: dict, errors: list[str]) -> Audio:
    mic = raw.get("microphone", "default")
    if mic is False or mic is None or mic == "none":
        mic = None
    elif mic is True:
        mic = "default"
    return Audio(
        microphone=None if mic is None else str(mic),
        desktop=bool(raw.get("desktop", False)),
        microphone_volume=_float(raw.get("microphone_volume", 1.0), "audio.microphone_volume", errors),
        desktop_volume=_float(raw.get("desktop_volume", 1.0), "audio.desktop_volume", errors),
        processing=raw.get("processing", "auto") not in (False, "off", "none"),
        target=_choice(raw.get("target", "youtube"), ("youtube", "podcast", "broadcast"), "audio.target", errors),
        keep_original=bool(raw.get("keep_original", True)),
    )


def _output(raw: dict, errors: list[str]) -> Output:
    out = Output()
    out.directory = str(raw.get("directory", out.directory))
    out.filename = str(raw.get("filename", out.filename))
    out.format = str(raw.get("format", out.format))
    if out.format not in FORMATS:
        errors.append(_("{where}: must be one of {values}").format(where="output.format", values=", ".join(FORMATS)))
    out.video_bitrate = _int(raw.get("video_bitrate", out.video_bitrate), "output.video_bitrate", errors, minimum=100)
    out.audio_bitrate = _int(raw.get("audio_bitrate", out.audio_bitrate), "output.audio_bitrate", errors, minimum=32)
    try:
        out.filename.format(project="", timestamp="", date="", format="")
    except (KeyError, IndexError, ValueError) as e:
        errors.append(_("output.filename: invalid placeholder ({error}); use {placeholders}").format(
            error=e, placeholders="{project}, {timestamp}, {date}, {format}"))
    return out


def _launch(raw, base: Path, errors: list[str]) -> list[Launch]:
    if not isinstance(raw, list):
        errors.append(_("{where}: must be a list").format(where="launch"))
        return []
    out = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append(Launch(command=item))
        elif isinstance(item, dict) and item.get("command"):
            cwd = item.get("cwd")
            out.append(Launch(
                command=str(item["command"]),
                cwd=str(_resolve(str(cwd), base)) if cwd else None,
                delay=_float(item.get("delay", 0), f"launch[{i}].delay", errors),
            ))
        else:
            errors.append(_("{where}: must be a string or a mapping with '{key}'").format(where=f"launch[{i}]", key="command"))
    return out


def _privacy(raw: dict, errors: list[str]) -> tuple[privacy.Rule, ...]:
    allow = raw.get("allow") or []
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        errors.append(_("{where}: must be a list of domains").format(where="privacy.allow"))
        allow = []
    block = []
    raw_block = raw.get("block") or []
    if not isinstance(raw_block, list):
        errors.append(_("{where}: must be a list").format(where="privacy.block"))
        raw_block = []
    for i, item in enumerate(raw_block):
        if isinstance(item, str) and item:
            block.append(privacy.Rule(item))
        elif isinstance(item, dict) and item.get("domain"):
            titles = item.get("titles") or []
            if isinstance(titles, str):
                titles = [titles]
            block.append(privacy.Rule(str(item["domain"]), tuple(str(t) for t in titles)))
        else:
            errors.append(_("{where}: must be a string or a mapping with '{key}'").format(
                where=f"privacy.block[{i}]", key="domain"))
    return privacy.build(allow, block)


def _choice(value, choices: tuple[str, ...], where: str, errors: list[str]) -> str:
    if value not in choices:
        errors.append(_("{where}: must be one of {values}").format(where=where, values=", ".join(choices)))
        return choices[0]
    return value


def _mapping(value, where: str, errors: list[str]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(_("{where}: must be a mapping").format(where=where))
        return {}
    return value


def _resolution(value, where: str, errors: list[str]) -> tuple[int, int]:
    s = str(value).lower()
    if s in RESOLUTION_PRESETS:
        return RESOLUTION_PRESETS[s]
    m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", s)
    if not m or int(m[1]) < 16 or int(m[2]) < 16:
        errors.append(_("{where}: invalid resolution '{value}' (e.g. 1920x1080 or 1080p)").format(where=where, value=value))
        return 1920, 1080
    # Encoders want even dimensions.
    return int(m[1]) // 2 * 2, int(m[2]) // 2 * 2


def _dimension(value, total: int, where: str, errors: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.endswith("%"):
        try:
            return round(total * float(value[:-1]) / 100) // 2 * 2
        except ValueError:
            pass
    elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    errors.append(_("{where}: must be a positive integer or a percentage (e.g. 25%)").format(where=where))
    return None


def _int(value, where: str, errors: list[str], minimum=None, maximum=None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(_("{where}: must be an integer").format(where=where))
        return minimum or 0
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        errors.append(_("{where}: out of range ({value})").format(where=where, value=value))
    return value


def _float(value, where: str, errors: list[str]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(_("{where}: must be a number").format(where=where))
        return 0.0
    return float(value)


def _color(value: str) -> int | None:
    """Parse #RRGGBB / #AARRGGBB into an ARGB int, as videotestsrc expects."""
    m = re.fullmatch(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})", value)
    if not m:
        return None
    v = int(m[1], 16)
    return v | 0xFF000000 if len(m[1]) == 6 else v


def _resolve(value: str, base: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    return p if p.is_absolute() else base / p


def template(name: str) -> str:
    """A commented starter project."""
    return _(TEMPLATE).format(name=name)


# Translators: this is a YAML file; translate only the comments after '#'.
TEMPLATE = N_("""\
project: {name}

video:
  resolution: 1920x1080
  fps: 30
  transition: 0.3            # seconds of transition between scenes

# The virtual desktop the video is composed on: a color (#RRGGBB) or the path
# of an image. When omitted, rec0's default background is used.
# background: "#1e2030"

# Close-up scene: active while rec0, or any window not listed below, has focus.
camera:
  device: default            # default, /dev/videoN or part of the device name
  # mirror: true             # mirrored like a mirror (default: the preference, on)
  closeup: fullscreen        # or {{position: center, width: 70%}}
  overlay:                   # small webcam during the share scene (false to hide it)
    position: bottom-right
    width: 240
    shape: circle            # circle or rect
  bubble:                    # round webcam window on screen while recording
    size: 200                # (false to disable it); in the video the overlay follows it
    position: bottom-right

# Share scene: when one of these windows has focus it is shown on the virtual
# desktop at the same position it has on the real screen. Matching is on the
# window title or class: for a browser tab, use its title.
screen:
  monitor: primary
  margin: 0                  # virtual desktop border around the real screen (0: corners match)
  frame: accent              # frame around shared windows: accent (theme colour), #RRGGBB or false

windows:
  - match: Firefox
  - match: Terminal

audio:
  microphone: default        # default, false, "test" or part of the device name
  desktop: false             # system sound
  processing: auto           # adaptive noise reduction, leveling and loudness after recording (or off)
  target: youtube            # loudness target: youtube (-14 LUFS), podcast (-16) or broadcast (-23)
  keep_original: true        # keep the unprocessed file next to the result

# Pages that are never recorded: when one has focus the share scene does not
# start (or freezes on the last safe frame). rec0 has a built-in list of mail,
# chat, password and banking sites (gmail.com, web.whatsapp.com, paypal.com…).
# privacy:
#   allow: [app.slack.com]           # lift built-in entries
#   block:                           # add your own
#     - mybank.example
#     - {{domain: intranet.example, titles: ["Intranet"]}}

# Applications to start before recording (optional)
# launch:
#   - command: firefox https://docs.python.org
#   - command: gnome-terminal
#     cwd: ~/Develop/project

output:
  # directory: ~/Videos/{name}   # default: the Videos folder, rec0 subfolder
  filename: "{{project}}-{{timestamp}}.mp4"
  format: mp4                # mp4 or mkv (more robust if the program is interrupted)
""")
