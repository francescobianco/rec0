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
    assert pick_camera_caps(caps, 30, (1280, 720)) == "image/jpeg,width=1280,height=720,framerate=30/1 ! jpegdec"
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
    assert scenes == ["share", "camera"]
    probe = subprocess.run(["gst-discoverer-1.0", str(out)], capture_output=True, text=True).stdout
    assert "H.264" in probe and "AAC" in probe
