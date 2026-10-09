"""Command line interface: rec0 init|check|record|devices|pipeline|forget|gui."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from . import __version__
from .project import TEMPLATE, ProjectError, load


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `rec0` and `rec0 progetto.yaml` open the GUI.
    commands = {"init", "check", "record", "devices", "pipeline", "forget", "gui", "-h", "--help", "--version"}
    if not argv or argv[0] not in commands:
        argv = ["gui", *argv]

    parser = argparse.ArgumentParser(prog="rec0", description="Registratore video dichiarativo basato su progetti YAML.")
    parser.add_argument("--version", action="version", version=f"rec0 {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="crea un nuovo file di progetto")
    p.add_argument("name", help="nome del progetto (crea NAME.yaml)")

    p = sub.add_parser("check", help="valida il progetto e verifica i dispositivi")
    p.add_argument("project")

    p = sub.add_parser("record", help="registra senza interfaccia grafica (Ctrl+C per fermare)")
    p.add_argument("project")
    p.add_argument("-d", "--duration", type=float, help="ferma dopo N secondi")
    p.add_argument("-o", "--output", help="file di output (sovrascrive output.directory/filename)")
    p.add_argument("-s", "--scene", choices=("auto", "camera", "share"), default="auto",
                   help="auto segue il focus delle finestre (default)")
    p.add_argument("--no-launch", action="store_true", help="non avviare le applicazioni in 'launch'")
    p.add_argument("--no-bubble", action="store_true", help="non mostrare la bolla con la webcam sullo schermo")

    sub.add_parser("devices", help="elenca webcam, microfoni, monitor e finestre")

    p = sub.add_parser("pipeline", help="stampa la pipeline GStreamer (debug)")
    p.add_argument("project")

    p = sub.add_parser("forget", help="dimentica le autorizzazioni di cattura salvate (Wayland)")
    p.add_argument("project", nargs="?")

    p = sub.add_parser("gui", help="apre l'interfaccia grafica (default)")
    p.add_argument("project", nargs="?")

    args = parser.parse_args(argv)
    try:
        return globals()[f"cmd_{args.command}"](args)
    except ProjectError as e:
        print("Errori nel progetto:", file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def cmd_init(args) -> int:
    path = Path(args.name if args.name.endswith((".yaml", ".yml")) else f"{args.name}.yaml")
    if path.exists():
        print(f"{path} esiste già", file=sys.stderr)
        return 1
    path.write_text(TEMPLATE.format(name=path.stem))
    print(f"creato {path}")
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

    print(f"Progetto '{project.name}': {project.width}x{project.height} @ {project.fps} fps, "
          f"sfondo {project.background}")
    print(f"Sessione grafica: {session_type()}")
    caps = Captures(project, log=lambda m: None)
    if project.camera:
        if project.camera.device == "test":
            print("  ✓ camera: sorgente di test")
        else:
            report("camera", lambda: (caps.prepare(), f"{caps.camera.name} ({caps.camera.id})")[1])
    if project.windows:
        if caps.backend == "wayland":
            print("  ! Wayland: il focus delle finestre non è leggibile, le scene si cambiano a mano")
            print("    (dalla GUI o con --scene); lo schermo viene autorizzato al primo avvio")
        else:
            report("schermo", lambda: (caps.prepare(), f"{caps.monitor.width}x{caps.monitor.height}"
                                       f"+{caps.monitor.x}+{caps.monitor.y}")[1])
            if caps.backend == "x11":
                wins = x11_windows()
                for w in project.windows:
                    found = [x for x in wins if project.match_window(x.title, x.wm_class) is w]
                    state = f"aperta ({found[0].title})" if found else "non aperta ora"
                    print(f"  · finestra '{w.match}': {state}")
    a = project.audio
    if a.microphone and a.microphone != "test":
        report("microfono", lambda: find_microphone(a.microphone) or "predefinito")
    if a.desktop:
        print("  ✓ audio di sistema: monitor dell'uscita predefinita")
    report("encoder video", lambda: video_encoder(project.output.video_bitrate, project.fps).split()[0])
    report("encoder audio", lambda: audio_encoder(project.output.audio_bitrate).split()[0])
    print(f"Output: {project.output_path()}")
    caps.close()
    print("Tutto pronto." if ok else "Ci sono problemi da risolvere.")
    return 0 if ok else 1


def cmd_record(args) -> int:
    from gi.repository import GLib

    from .capture import CaptureError, Captures
    from .focus import FocusTracker
    from .scenes import SCENE_LABELS
    from .recorder import Director, Recorder

    project = load(args.project)
    output = Path(args.output).expanduser() if args.output else project.output_path()
    captures = Captures(project)
    try:
        if not args.no_launch:
            captures.launch_apps()
        captures.prepare()
    except CaptureError as e:
        print(f"errore: {e}", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    rec = Recorder(project, captures)
    director = Director(rec, on_scene=lambda scene, title: print(
        f"\r\033[Kscena: {SCENE_LABELS[scene]}" + (f" — {title}" if title else "")))
    result = {"code": 0}
    tracker = FocusTracker(director.focus_changed) if captures.follows_focus and args.scene == "auto" else None
    if args.scene != "auto":
        director.set_mode(args.scene)
    elif not captures.follows_focus and project.windows:
        print("nota: cambio scena automatico non disponibile in questa sessione (usa --scene)")

    def on_error(msg):
        print(f"\nerrore: {msg}", file=sys.stderr)
        result["code"] = 1

    def on_finished(path):
        if tracker:
            tracker.stop()
        if bubble:
            bubble.stop()
        if path:
            print(f"\nsalvato: {path}")
        loop.quit()

    def tick():
        if rec.pipeline:
            s = int(rec.position())
            print(f"\r\033[K● REC {s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}", end="", flush=True)
        return True

    def request_stop(*_):
        print("\nfinalizzazione…")
        rec.stop()
        return True

    rec.on_error = on_error
    rec.on_finished = on_finished
    try:
        rec.start(director.frame, output)
    except RuntimeError as e:
        print(f"errore: {e}", file=sys.stderr)
        captures.close()
        return 1
    if tracker:
        tracker.start()
    bubble = None
    cam = project.camera
    if cam and cam.bubble and captures.monitor and not args.no_bubble:
        from .bubble import Bubble
        from .project import Rect
        from .scenes import bubble_rect

        r = bubble_rect(cam.bubble, captures.monitor)
        bubble = Bubble(r.width, r.x, r.y,
                        on_move=lambda x, y, size: director.set_bubble(Rect(x, y, size, size)))
        rec.on_bubble_frame = bubble.frame
        bubble.start()
        bubble.show()
        director.set_bubble(r)
    print(f"registrazione in {output} — Ctrl+C per fermare")
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, request_stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, request_stop)
    GLib.timeout_add(500, tick)
    if args.duration:
        GLib.timeout_add(int(args.duration * 1000), request_stop)
    loop.run()
    captures.close()
    return result["code"]


def cmd_devices(args) -> int:
    from .capture import CaptureError, cameras, microphones, session_type, x11_monitors, x11_windows

    print("Webcam:")
    for c in cameras():
        print(f"  {c.id}  {c.name}")
    print("Microfoni:")
    for m in microphones():
        print(f"  {m.name}  ({m.id})")
    if session_type() == "x11":
        print("Monitor:")
        for m in x11_monitors():
            print(f"  {m['index']}: {m['name']} {m['width']}x{m['height']}+{m['x']}+{m['y']}"
                  + (" (primario)" if m["primary"] else ""))
        print("Finestre (per 'windows: - match: ...'):")
        try:
            for w in x11_windows():
                print(f"  [{w.wm_class}]  {w.title}")
        except CaptureError as e:
            print(f"  {e}")
    else:
        print("Monitor e finestre: su Wayland si scelgono tramite il portale di sistema.")
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
    print("autorizzazioni dimenticate")
    return 0


def cmd_gui(args) -> int:
    from .app import run

    return run(args.project)


if __name__ == "__main__":
    sys.exit(main())
