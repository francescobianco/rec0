from rec0.project import Rect, parse
from rec0.scenes import compose, interpolate, window_layer

MONITOR = Rect(0, 0, 1920, 1080)


def project(**over):
    data = {"video": {"resolution": "1920x1080"}, "camera": {"device": "test"},
            "windows": ["Firefox"], "screen": {"margin": 0}}
    data.update(over)
    return parse(data)


def test_window_keeps_real_position_at_same_scale():
    layer = window_layer(project(), MONITOR, Rect(100, 50, 800, 600))
    assert layer.rect == Rect(100, 50, 800, 600)
    assert layer.crop == (100, 50, 1920 - 900, 1080 - 650)


def test_margin_scales_screen_into_virtual_desktop():
    p = project(screen={"margin": 108})
    # (1080 - 216) / 1080 = 0.8 -> screen drawn at 1536x864 centred
    layer = window_layer(p, MONITOR, Rect(0, 0, 1920, 1080))
    assert layer.rect == Rect(192, 108, 1536, 864)


def test_window_is_clipped_to_monitor():
    layer = window_layer(project(), MONITOR, Rect(-100, -100, 600, 400))
    assert layer.crop == (0, 0, 1420, 780)
    assert layer.rect == Rect(0, 0, 500, 300)


def test_scenes():
    p = project()
    cam = compose(p, "camera", MONITOR, None)
    assert cam.camera.rect == p.camera.closeup.rect and cam.camera.alpha == 1
    assert cam.screen.alpha == 0
    share = compose(p, "share", MONITOR, Rect(10, 10, 500, 500), previous=cam)
    assert share.camera.rect == p.camera.overlay.rect
    assert share.screen.alpha == 1 and share.screen.rect == Rect(10, 10, 500, 500)
    back = compose(p, "camera", MONITOR, None, previous=share)
    # the window fades out where it was
    assert back.screen.rect == share.screen.rect and back.screen.alpha == 0


def test_without_screen_capture():
    f = compose(project(windows=[]), "camera", None, None)
    assert f.screen is None


def test_interpolate_endpoints():
    p = project()
    a = compose(p, "camera", MONITOR, None)
    b = compose(p, "share", MONITOR, Rect(10, 10, 500, 500), previous=a)
    assert interpolate(a, b, 0).camera == a.camera
    assert interpolate(a, b, 1) == b
    mid = interpolate(a, b, 0.5)
    assert 0 < mid.screen.alpha < 1
    # a window appearing does not slide in from elsewhere
    assert mid.screen.rect == b.screen.rect


def test_overlay_follows_bubble():
    from rec0.scenes import bubble_rect
    p = project(screen={"margin": 108})
    b = bubble_rect(p.camera.bubble, MONITOR)
    assert b == Rect(1920 - 200 - 40, 1080 - 200 - 40, 200, 200)
    f = compose(p, "share", MONITOR, Rect(0, 0, 1920, 1080), bubble=b)
    # same 0.8 scale and offset as the screen layer: it covers the captured bubble exactly
    assert f.camera.rect == Rect(192 + round(1680 * 0.8), 108 + round(840 * 0.8), 160, 160)
