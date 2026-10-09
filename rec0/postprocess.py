"""Run audio processing on a finished recording, keeping the original safe."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from . import audio
from .project import Project


def original_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.original{path.suffix}")


def finalize(project: Project, path: Path, progress: Callable[[str, float], None] | None = None) -> audio.Report:
    """Replace `path` with its processed version. The recording is renamed first and
    restored if anything fails, so a recording is never lost."""
    original = original_path(path)
    os.replace(path, original)
    try:
        report = audio.process(original, path, project.audio.target, progress)
    except BaseException:
        if path.exists():
            path.unlink()
        os.replace(original, path)
        raise
    if not project.audio.keep_original:
        original.unlink()
    return report


def finalize_async(project: Project, path: Path, on_progress: Callable[[str, float], None],
                   on_done: Callable[[audio.Report | None, str | None], None]):
    """finalize() in a thread; callbacks run on the GLib main loop."""

    def progress(step, value):
        GLib.idle_add(lambda: on_progress(step, value) and False)

    def run():
        try:
            report, error = finalize(project, path, progress), None
        except Exception as e:  # noqa: BLE001 - reported to the user, original restored
            report, error = None, str(e)
        GLib.idle_add(lambda: on_done(report, error) and False)

    threading.Thread(target=run, name="rec0-audio", daemon=True).start()
