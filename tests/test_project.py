import pytest

from rec0.project import ProjectError, Rect, parse


def base(**over):
    data = {
        "project": "demo",
        "video": {"resolution": "1280x720", "fps": 30},
        "camera": {"device": "test"},
        "windows": ["Firefox"],
    }
    data.update(over)
    return data


def test_defaults():
    p = parse(base())
    assert (p.width, p.height, p.fps) == (1280, 720, 30)
    assert p.camera.closeup.rect == Rect(0, 0, 1280, 720)
    # default overlay: round, bottom-right, 240px, 24px margin
    assert p.camera.overlay.rect == Rect(1280 - 240 - 24, 720 - 240 - 24, 240, 240)
    assert p.camera.overlay.circle
    assert p.camera.bubble.size == 200
    assert p.windows[0].match == "Firefox"
    assert p.audio.microphone == "default"


def test_positions_and_percentages():
    p = parse(base(camera={"device": "test", "closeup": {"position": "center", "width": "50%"},
                           "overlay": {"position": "top-left", "width": 200, "height": 200, "margin": 10}}))
    assert p.camera.closeup.rect == Rect(320, 180, 640, 360)
    assert p.camera.overlay.rect == Rect(10, 10, 200, 200)


def test_overlay_and_bubble_can_be_disabled():
    cam = parse(base(camera={"device": "test", "overlay": False, "bubble": False})).camera
    assert cam.overlay is None and cam.bubble is None


def test_rect_overlay_keeps_16_9():
    cam = parse(base(camera={"device": "test", "overlay": {"width": 320, "shape": "rect"}})).camera
    assert (cam.overlay.rect.width, cam.overlay.rect.height) == (320, 180) and not cam.overlay.circle


def test_collects_all_errors():
    with pytest.raises(ProjectError) as e:
        parse(base(video={"resolution": "huge", "fps": "x"}, output={"format": "avi"},
                   windows=[{"nomatch": 1}]))
    msgs = "\n".join(e.value.errors)
    assert "video.resolution" in msgs and "video.fps" in msgs
    assert "output.format" in msgs and "windows[0]" in msgs


def test_needs_something_to_record():
    with pytest.raises(ProjectError):
        parse(base(camera=False, windows=[]))


def test_match_window_title_or_class():
    p = parse(base(windows=["firefox", {"match": "Lezione 3"}]))
    assert p.match_window("Docs — Mozilla Firefox", "Navigator") is p.windows[0]
    assert p.match_window("YouTube", "google-chrome") is None
    assert p.match_window("Lezione 3 - Google Chrome", "google-chrome") is p.windows[1]


def test_output_path_placeholders(tmp_path):
    from datetime import datetime
    p = parse(base(output={"directory": str(tmp_path), "filename": "{project}-{date}"}))
    assert p.output_path(datetime(2026, 1, 2)) == tmp_path / "demo-2026-01-02.mp4"


def test_set_audio_processing_edits_only_that_line(tmp_path):
    from rec0.project import load, set_audio_processing
    f = tmp_path / "p.r0"
    original = "project: x  # mine\ncamera: {device: test}\nwindows: [a]\naudio:\n  microphone: test  # keep\n"
    f.write_text(original)
    set_audio_processing(f, False)
    assert load(f).audio.processing is False
    assert f.read_text() == original.replace("  microphone: test  # keep\n",
                                             "  processing: off\n  microphone: test  # keep\n")
    set_audio_processing(f, True)
    assert load(f).audio.processing is True and "processing: auto" in f.read_text()


def test_privacy_covers_chat_sites_and_desktop_apps():
    p = parse(base(privacy={"allow": ["zoom"]}))
    assert p.private("(3) WhatsApp - Google Chrome").domain == "web.whatsapp.com"
    assert p.private("Google Chat - Google Chrome").domain == "chat.google.com"
    assert p.private("General | Team", "teams-for-linux teams-for-linux").domain == "teams"
    assert p.private("anything", "Mail thunderbird_thunderbird") is not None
    assert p.private("capture.pcap", "wireshark Wireshark") is None        # whole class names only
    assert p.private("Meeting", "zoom zoom") is None                       # lifted by allow
    assert p.private("Python docs - Google Chrome") is None


@pytest.mark.parametrize("before, after", [
    # an existing size: only the value changes, the comment stays
    ("camera:\n  device: test\n  bubble:\n    size: 200   # px\n    position: top-left\nwindows: [a]\n",
     "camera:\n  device: test\n  bubble:\n    size: 260   # px\n    position: top-left\nwindows: [a]\n"),
    # a bubble without size
    ("camera:\n  device: test\n  bubble:\n    position: top-left\n",
     "camera:\n  device: test\n  bubble:\n    size: 260\n    position: top-left\n"),
    # no bubble section
    ("camera:\n  device: test  # webcam\nwindows: [a]\n",
     "camera:\n  bubble:\n    size: 260\n  device: test  # webcam\nwindows: [a]\n"),
    # `bubble: true` is an empty mapping
    ("camera:\n    device: test\n    bubble: true  # on\n",
     "camera:\n    device: test\n    bubble:  # on\n        size: 260\n"),
])
def test_set_option_nested(tmp_path, before, after):
    from rec0.project import load, set_option
    f = tmp_path / "p.r0"
    f.write_text(before)
    assert set_option(f, ("camera", "bubble", "size"), "260")
    assert f.read_text() == after
    assert load(f).camera.bubble.size == 260


def test_set_option_leaves_inline_sections_alone(tmp_path):
    from rec0.project import set_option
    f = tmp_path / "p.r0"
    f.write_text("camera:\n  bubble: {size: 200}\n")
    assert not set_option(f, ("camera", "bubble", "size"), "260")
    assert f.read_text() == "camera:\n  bubble: {size: 200}\n"


def test_window_class_matches_wayland_app_ids():
    p = parse(base(windows=["gnome-terminal", "jetbrains-webstorm"]))
    assert p.match_window("francesco@Yoga7: ~", "gnome-terminal-server Gnome-terminal")   # X11
    assert p.match_window("francesco@Yoga7: ~", "org.gnome.Terminal")                     # Wayland
    assert p.match_window("rec0 – app.py", "jetbrains-webstorm")
    assert p.match_window("Notes", "org.gnome.TextEditor") is None


def test_privacy_recognises_app_ids():
    p = parse(base())
    for app_id in ("org.mozilla.Thunderbird", "com.slack.Slack", "org.signal.Signal", "com.discordapp.Discord"):
        assert p.private("anything", app_id) is not None, app_id
    assert p.private("~", "org.gnome.Terminal") is None
    assert p.private("capture", "org.wireshark.Wireshark") is None    # "wire" is not Wireshark
