"""Command line interface.

`rec0 [FILE…]` opens the GUI (GApplication options such as --gapplication-service
are passed through); `rec0 init|check|record|devices|pipeline|forget` run headless.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from . import __version__
from .i18n import _
from .project import EXTENSION, EXTENSIONS, ProjectError, load, template

COMMANDS = {"init", "check", "record", "process", "devices", "pipeline", "forget", "-h", "--help", "--version"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        return gui(argv)

    parser = argparse.ArgumentParser(prog="rec0", description=_("Declarative video recorder driven by YAML projects."),
                                     epilog=_("Without a command, rec0 opens the graphical interface."))
    parser.add_argument("--version", action="version", version=f"rec0 {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help=_("create a new project file"))
    p.add_argument("name", help=_("project name (creates NAME.r0)"))

    p = sub.add_parser("check", help=_("validate the project and check the devices"))
    p.add_argument("project")

    p = sub.add_parser("record", help=_("record without the graphical interface (Ctrl+C to stop)"))
    p.add_argument("project")
    p.add_argument("-d", "--duration", type=float, help=_("stop after N seconds"))
    p.add_argument("-o", "--output", help=_("output file (overrides output.directory/filename)"))
    p.add_argument("-s", "--scene", choices=("auto", "camera", "share"), default="auto",
                   help=_("auto follows window focus (default)"))
    p.add_argument("--no-launch", action="store_true", help=_("do not start the applications in 'launch'"))
    p.add_argument("--no-bubble", action="store_true", help=_("do not show the webcam bubble on screen"))
    p.add_argument("--no-process", action="store_true", help=_("do not optimize the audio after recording"))

    p = sub.add_parser("process", help=_("optimize the audio of a video file (noise, levels, loudness)"))
    p.add_argument("file")
    p.add_argument("-o", "--output", help=_("output file (default: FILE.processed.EXT)"))
    p.add_argument("-t", "--target", choices=("youtube", "podcast", "broadcast"), default="youtube")
    p.add_argument("-n", "--dry-run", action="store_true", help=_("only analyze and show the plan"))

    sub.add_parser("devices", help=_("list webcams, microphones, monitors and windows"))

    p = sub.add_parser("pipeline", help=_("print the GStreamer pipeline (debugging)"))
    p.add_argument("project")

    p = sub.add_parser("forget", help=_("forget saved screen capture permissions (Wayland)"))
    p.add_argument("project", nargs="?")

    args = parser.parse_args(argv)
    try:
        return globals()[f"cmd_{args.command}"](args)
    except ProjectError as e:
        print(_("Errors in the project:"), file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def gui(argv: list[str]) -> int:
    from .app import run

    dev_api = "--dev-api" in argv
    return run([a for a in argv if a != "--dev-api"], dev_api=dev_api)


def cmd_init(args) -> int:
    path = Path(args.name if args.name.endswith(EXTENSIONS) else f"{args.name}{EXTENSION}")
    if path.exists():
        print(_("{path} already exists").format(path=path), file=sys.stderr)
        return 1
    path.write_text(template(path.stem))
    print(_("created {path}").format(path=path))
    return 0


def cmd_check(args) -> int:
    from .capture import CaptureError, Captures, find_microphone, session_type, x11_windows
    from .recorder import audio_encoder, video_encoder

    project = load(args.project)
    ok = True

    def report(label, fn):
        nonlocal ok
        try:
            print(f"  ✓ {label}: {fn()}")
        except (CaptureError, RuntimeError) as e:
            ok = False
            print(f"  ✗ {label}: {e}")

    print(_("Project '{name}': {width}x{height} @ {fps} fps, background {background}").format(
        name=project.name, width=project.width, height=project.height, fps=project.fps,
        background=project.background))
    print(_("Graphical session: {session}").format(session=session_type()))
    caps = Captures(project, log=lambda m: None)
    if project.camera:
        if project.camera.device == "test":
            print("  ✓ " + _("camera: test source"))
        else:
            report(_("camera"), lambda: (caps.prepare(), f"{caps.camera.name} ({caps.camera.id})")[1])
    if project.windows:
        if caps.backend == "wayland":
            print("  ! " + _("Wayland: window focus cannot be read, scenes are switched by hand"))
            print("    " + _("(from the GUI or with --scene); the screen is authorized on first use"))
        else:
            report(_("screen"), lambda: (caps.prepare(), f"{caps.monitor.width}x{caps.monitor.height}"
                                         f"+{caps.monitor.x}+{caps.monitor.y}")[1])
            if caps.backend == "x11":
                wins = x11_windows()
                for w in project.windows:
                    found = [x for x in wins if project.match_window(x.title, x.wm_class) is w]
                    state = _("open ({title})").format(title=found[0].title) if found else _("not open now")
                    print("  · " + _("window '{match}': {state}").format(match=w.match, state=state))
    a = project.audio
    if a.microphone and a.microphone != "test":
        report(_("microphone"), lambda: find_microphone(a.microphone) or _("default"))
    if a.desktop:
        print("  ✓ " + _("system sound: monitor of the default output"))
    report(_("video encoder"), lambda: video_encoder(project.output.video_bitrate, project.fps).split()[0])
    report(_("audio encoder"), lambda: audio_encoder(project.output.audio_bitrate).split()[0])
    print(_("Output: {path}").format(path=project.output_path()))
    caps.close()
    print(_("All set.") if ok else _("There are problems to fix."))
    return 0 if ok else 1


def cmd_record(args) -> int:
    from gi.repository import GLib

    from .capture import CaptureError, Captures
    from .focus import FocusTracker
    from .recorder import Director, Recorder
    from .scenes import scene_label

    project = load(args.project)
    output = Path(args.output).expanduser() if args.output else project.output_path()
    captures = Captures(project)
    try:
        if not args.no_launch:
            captures.launch_apps()
        captures.prepare()
    except CaptureError as e:
        print(_("error: {message}").format(message=e), file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    rec = Recorder(project, captures)
    director = Director(rec, on_scene=lambda scene, title: print(
        "\r\033[K" + _("scene: {scene}").format(scene=scene_label(scene)) + (f" — {title}" if title else "")))
    result = {"code": 0}
    bubble = None
    tracker = FocusTracker(director.focus_changed) if captures.follows_focus and args.scene == "auto" else None
    if tracker:
        tracker.watch = director.watched
    if args.scene != "auto":
        director.set_mode(args.scene)
    elif not captures.follows_focus and project.windows:
        print(_("note: automatic scene switching is not available in this session (use --scene)"))

    def on_error(msg):
        print("\n" + _("error: {message}").format(message=msg), file=sys.stderr)
        result["code"] = 1

    def on_finished(path):
        if tracker:
            tracker.stop()
        if bubble:
            bubble.stop()
        if path:
            print("\n" + _("saved: {path}").format(path=path))
        loop.quit()

    def tick():
        if rec.pipeline:
            s = int(rec.position())
            print(f"\r\033[K● REC {s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}", end="", flush=True)
        return True

    stops = {"n": 0}

    def request_stop(*_args):
        stops["n"] += 1
        if stops["n"] > 1 or not rec._playing:
            # Nothing recorded yet (the pipeline never started), or asked twice: leave now.
            rec._teardown()
            result["code"] = result["code"] or 1
            loop.quit()
            return True
        print("\n" + _("finishing…"))
        rec.stop()
        return True

    def check_started():
        if rec.pipeline is not None and not rec._playing:
            print("\n" + _("error: {message}").format(
                message=_("recording did not start (is the webcam used by another program?)")), file=sys.stderr)
            result["code"] = 1
            rec._teardown()
            loop.quit()
        return False

    rec.on_error = on_error
    rec.on_finished = on_finished
    try:
        rec.start(director.frame, output)
    except RuntimeError as e:
        print(_("error: {message}").format(message=e), file=sys.stderr)
        captures.close()
        return 1
    if tracker:
        tracker.start()
    cam = project.camera
    if cam and cam.bubble and captures.monitor and not args.no_bubble:
        from .bubble import Bubble
        from .project import Rect
        from .scenes import bubble_rect

        r = bubble_rect(cam.bubble, captures.area or captures.monitor)
        bubble = Bubble(r.width, r.x, r.y,
                        on_move=lambda x, y, size: director.set_bubble(Rect(x, y, size, size)))
        rec.on_bubble_frame = bubble.frame
        bubble.start()
        bubble.show()
        director.set_bubble(r)
    print(_("recording to {path} — Ctrl+C to stop").format(path=output))
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, request_stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, request_stop)
    GLib.timeout_add(500, tick)
    GLib.timeout_add_seconds(10, check_started)
    if args.duration:
        GLib.timeout_add(int(args.duration * 1000), request_stop)
    loop.run()
    captures.close()
    if result["code"] == 0 and output.exists() and project.audio.processing and not args.no_process:
        from . import audio, postprocess

        if not audio.available():
            print(_("note: install ffmpeg to optimize the audio"))
            return 0
        print(_("optimizing audio…"))
        try:
            report = postprocess.finalize(project, output, _progress_printer())
        except audio.AudioError as e:
            print("\n" + _("audio optimization failed, the original recording was kept: {error}").format(error=e),
                  file=sys.stderr)
            return 1
        print()
        _print_report(report)
    return result["code"]


def _progress_printer():
    labels = {"analyze": _("analyzing"), "measure": _("measuring"), "render": _("rendering"),
              "verify": _("verifying"), "done": _("done")}

    def progress(step, value):
        print(f"\r\033[K  {labels.get(step, step)} {value:.0%}", end="", flush=True)
    return progress


def _print_report(report) -> None:
    for s in report.stages:
        print(f"  {'●' if s.enabled else '○'} {s.name:<10} {s.reason}")
    r = report.result
    print(_("Result: {lufs:.1f} LUFS, true peak {tp:.1f} dBTP, loudness range {lra:.1f} LU").format(
        lufs=r["integrated_lufs"], tp=r["true_peak_dbtp"], lra=r["loudness_range_lu"]))
    print(_("saved: {path}").format(path=report.output))


def cmd_process(args) -> int:
    from . import audio

    src = Path(args.file)
    if not src.exists():
        print(_("cannot read {path}: {error}").format(path=src, error=_("file not found")), file=sys.stderr)
        return 1
    if not audio.available():
        print(_("ffmpeg is required for audio processing"), file=sys.stderr)
        return 1
    if args.dry_run:
        a = audio.analyze(src)
        print(a.summary())
        for line in audio.plan(a, args.target).describe():
            print("  " + line)
        return 0
    dst = Path(args.output) if args.output else src.with_name(f"{src.stem}.processed{src.suffix}")
    try:
        report = audio.process(src, dst, args.target, _progress_printer())
    except audio.AudioError as e:
        print("\n" + _("error: {message}").format(message=e), file=sys.stderr)
        return 1
    print()
    _print_report(report)
    return 0


def cmd_devices(args) -> int:
    from .capture import cameras, microphones, session_type, x11_monitors, x11_windows

    print(_("Webcams:"))
    for c in cameras():
        print(f"  {c.id}  {c.name}")
    print(_("Microphones:"))
    for m in microphones():
        print(f"  {m.name}  ({m.id})")
    if session_type() == "x11":
        print(_("Monitors:"))
        for m in x11_monitors():
            print(f"  {m['index']}: {m['name']} {m['width']}x{m['height']}+{m['x']}+{m['y']}"
                  + (" " + _("(primary)") if m["primary"] else ""))
        print(_("Windows (for 'windows: - match: ...'):"))
        for w in x11_windows():
            print(f"  [{w.wm_class}]  {w.title}")
    else:
        print(_("Monitors and windows: on Wayland they are chosen through the system portal."))
    return 0


def cmd_pipeline(args) -> int:
    from .capture import Captures
    from .recorder import Director, Recorder

    project = load(args.project)
    captures = Captures(project)
    rec = Recorder(project, captures)
    captures.prepare()
    print(rec.describe(project.output_path(), Director(rec).frame, preview=False).replace(" mix.", " mix.\n  "))
    captures.close()
    return 0


def cmd_forget(args) -> int:
    from .capture import forget_tokens

    forget_tokens(load(args.project).name if args.project else None)
    print(_("saved permissions forgotten"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
