"""End-to-end: record a short clip with synthetic sources and switch scenes."""

import subprocess

import pytest
from gi.repository import GLib

from rec0.capture import Captures, pick_camera_caps
from rec0.focus import FocusedWindow
from rec0.project import Rect, parse
from rec0.recorder import Director, Recorder


def test_pick_camera_caps_prefers_full_framerate():
    caps = ("image/jpeg, width=(int)1920, height=(int)1080, framerate=(fraction)30/1; "
            "image/jpeg, width=(int)1280, height=(int)720, framerate=(fraction){ 30/1, 15/1 }; "
            "video/x-raw, format=(string)YUY2, width=(int)1920, height=(int)1080, framerate=(fraction)5/1; "
            "video/x-raw, format=(string)YUY2, width=(int)640, height=(int)480, framerate=(fraction)30/1")
    # MJPEG, decoded on the GPU or in software depending on the machine
    assert pick_camera_caps(caps, 30, (1280, 720)).startswith("image/jpeg,width=1280,height=720,framerate=30/1 ! ")
    # A GPU decoder that can scale must be pinned to the native size: otherwise the
    # bubble branch, negotiated through the tee, shrinks the whole webcam (356x200).
    chain = pick_camera_caps(caps, 30, (1280, 720))
    assert "vapostproc" not in chain or chain.endswith("video/x-raw,width=1280,height=720")
    assert pick_camera_caps(caps, 30, (320, 180)) == "video/x-raw,format=YUY2,width=640,height=480,framerate=30/1"


@pytest.mark.parametrize("fmt", ["mp4", "mkv"])
def test_record_with_scene_switch(tmp_path, fmt):
    p = parse({
        "video": {"resolution": "640x360", "fps": 25, "transition": 0.1},
        "background": "#203040",
        "camera": {"device": "test"},
        "screen": {"monitor": "test", "margin": 20},
        "windows": ["Firefox"],
        "audio": {"microphone": "test"},
        "output": {"directory": str(tmp_path), "format": fmt},
    })
    caps = Captures(p, log=lambda m: None)
    rec = Recorder(p, caps)
    caps.prepare()
    director = Director(rec)
    scenes, errors, done = [], [], []
    director.on_scene = lambda s, t: scenes.append(s)
    loop = GLib.MainLoop()
    rec.on_error = errors.append
    rec.on_finished = lambda path: (done.append(path), loop.quit())
    out = p.output_path()
    rec.start(director.frame, out)

    firefox = FocusedWindow(1, "Docs - Mozilla Firefox", "Navigator firefox", Rect(100, 100, 800, 600))
    GLib.timeout_add(500, lambda: director.focus_changed(firefox) and False)
    GLib.timeout_add(1000, lambda: director.focus_changed(FocusedWindow(2, "Slack", "slack", Rect(0, 0, 10, 10))) and False)
    GLib.timeout_add(1500, lambda: rec.stop() and False)
    GLib.timeout_add(15000, loop.quit)
    loop.run()

    assert not errors
    assert done == [out]
    # on_scene also reports subject changes (window title): look at scene changes only
    changes = [sc for i, sc in enumerate(scenes) if i == 0 or scenes[i - 1] != sc]
    assert changes == ["share", "camera"]
    probe = subprocess.run(["gst-discoverer-1.0", str(out)], capture_output=True, text=True).stdout
    assert "H.264" in probe and "AAC" in probe


def _session(tmp_path, **over):
    data = {
        "video": {"resolution": "640x360", "fps": 25, "transition": 0},
        "camera": {"device": "test"},
        "screen": {"monitor": "test"},
        "windows": ["chrome", "terminal"],
        "audio": {"microphone": False},
        "privacy": {"block": ["mybank.example"]},
        "output": {"directory": str(tmp_path)},
    }
    data.update(over)
    p = parse(data)
    caps = Captures(p, log=lambda m: None)
    rec = Recorder(p, caps)
    caps.prepare()
    return p, rec, Director(rec)


def _run(rec, p, steps, until=1800):
    loop = GLib.MainLoop()
    errors = []
    rec.on_error = errors.append
    rec.on_finished = lambda path: loop.quit()
    for ms, fn in steps:
        GLib.timeout_add(ms, lambda fn=fn: fn() and False)
    GLib.timeout_add(until, lambda: rec.stop() and False)
    GLib.timeout_add(15000, loop.quit)
    loop.run()
    return errors


def test_privacy_holds_scene_and_delays_reveal(tmp_path):
    from rec0.recorder import SCREEN_DELAY

    p, rec, d = _session(tmp_path)
    rec.start(d.frame, p.output_path())
    log = []

    def tab(title):
        return FocusedWindow(1, title + " - Google Chrome", "google-chrome", Rect(0, 32, 800, 600))

    reveal = int(SCREEN_DELAY * 1000) + 200
    errors = _run(rec, p, [
        # From close-up, a private tab never starts sharing (nothing is captured).
        (300, lambda: d.focus_changed(tab("Inbox - Gmail"))),
        (400, lambda: log.append(("gmail", d.scene, sorted(d.windows), d.private.domain))),
        # A shareable tab: the window joins frozen, and shows only after the delay.
        (500, lambda: d.focus_changed(tab("Python docs"))),
        (600, lambda: log.append(("docs-early", rec.frozen.get(1), 1 in d.revealed))),
        (500 + reveal, lambda: log.append(("docs", d.scene, rec.frozen.get(1), 1 in d.revealed))),
        # A private tab while sharing freezes that window at once; the scene stays.
        (1400, lambda: d.focus_changed(tab("Home - mybank.example"))),
        (1450, lambda: log.append(("bank", d.scene, rec.frozen.get(1)))),
    ])
    assert not errors
    assert log == [
        ("gmail", "camera", [], "gmail.com"),
        ("docs-early", True, False),
        ("docs", "share", False, True),
        ("bank", "share", True),
    ]


def test_presented_windows_stay_when_focus_moves_on(tmp_path):
    p, rec, d = _session(tmp_path)
    rec.start(d.frame, p.output_path())
    t1 = FocusedWindow(11, "one - Terminal", "gnome-terminal", Rect(0, 40, 600, 400))
    t2 = FocusedWindow(12, "two - Terminal", "gnome-terminal", Rect(700, 300, 600, 400))
    other = FocusedWindow(13, "Notes", "notes", Rect(0, 0, 300, 300))
    log = []
    errors = _run(rec, p, [
        (200, lambda: d.focus_changed(t1)),
        (700, lambda: d.focus_changed(t2)),
        (1300, lambda: log.append(("both", [k for k, l in d.frame.windows if l.alpha > 0]))),
        # A window outside the project: close-up, but the presentation is remembered...
        (1400, lambda: d.focus_changed(other)),
        (1500, lambda: log.append(("camera", d.scene, list(d.windows)))),
        # ...and t1 closed (the tracker reports it gone): it leaves the presentation.
        (1600, lambda: d.focus_changed(t2, {11: None, 12: t2})),
        (1700, lambda: log.append(("closed", list(d.windows), sorted(rec.sources)))),
    ], until=2200)
    assert not errors
    assert log == [
        ("both", [11, 12]),                  # t2 on top of t1, both visible
        ("camera", "camera", [11, 12]),
        ("closed", [12], [12]),
    ]


def test_system_sound_is_its_own_track_and_mixed_in_at_the_end(tmp_path):
    from rec0 import audio, postprocess

    p, rec, d = _session(tmp_path, audio={"microphone": "test", "desktop": "test", "processing": "off"})
    out = p.output_path()
    rec.start(d.frame, out)
    assert not _run(rec, p, [], until=1500)
    assert audio.audio_tracks(out) == 2                       # voice and system sound, separate
    assert postprocess.finalize(p, out) is None               # optimization off: just mixed
    assert audio.audio_tracks(out) == 1
    assert audio.audio_tracks(postprocess.original_path(out)) == 2


def test_preview_rate_changes_while_recording(tmp_path):
    # Going to the background lowers the preview rate mid-recording: the preview
    # branch must renegotiate on its own, without stopping the recording.
    p, rec, d = _session(tmp_path)
    frames = []
    rec.on_preview = lambda *a: frames.append(1)
    out = p.output_path()
    rec.start(d.frame, out)
    counts = []
    errors = _run(rec, p, [
        (500, lambda: rec.set_preview_rate(10)),
        (900, lambda: counts.append(len(frames))),
        (1300, lambda: counts.append(len(frames))),
        (1400, lambda: rec.set_preview_rate(30)),
    ], until=2200)
    assert not errors
    assert counts[1] > counts[0]          # the preview kept going at the lower rate
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(out)], capture_output=True, text=True).stdout
    assert float(probe) > 1.5
