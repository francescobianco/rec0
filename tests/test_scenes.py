from rec0.project import Rect, parse
from rec0.scenes import compose, interpolate, window_layer

MONITOR = Rect(0, 0, 1920, 1080)


def project(**over):
    data = {"video": {"resolution": "1920x1080"}, "camera": {"device": "test"},
            "windows": ["Firefox"], "screen": {"margin": 0}}
    data.update(over)
    return parse(data)


WORK = Rect(0, 32, 1920, 1048)   # GNOME's top bar takes the first 32 px


def test_window_keeps_real_position_and_gets_an_outside_frame():
    layer = window_layer(project(), MONITOR, Rect(100, 50, 800, 600))
    # Crop and rect widened by the frame (3 px at 1:1 scale) around the window.
    assert layer.crop == (97, 47, 1920 - 903, 1080 - 653)
    assert layer.rect == Rect(97, 47, 806, 606)
    assert layer.edge == 3 and layer.radius == 12


def test_large_aspect_mismatch_is_fitted_without_distortion():
    p = project(screen={"margin": 108})
    # (1920-216):(1080-216) is far from 16:9: uniform 0.8 scale, centred.
    layer = window_layer(p, MONITOR, None)
    assert layer.rect == Rect(192, 108, 1536, 864) and layer.edge == 0


def test_work_area_corners_are_canvas_corners():
    from rec0.scenes import to_canvas
    p = project()
    bubble = Rect(0, 32 + 1048 - 200, 200, 200)      # bottom-left corner of the work area
    r = to_canvas(p, WORK, bubble)
    assert (r.x, r.y + r.height) == (0, 1080)
    top_right = to_canvas(p, WORK, Rect(1920 - 200, 32, 200, 200))
    assert (top_right.x + top_right.width, top_right.y) == (1920, 0)


def test_maximized_window_fills_the_video_without_frame():
    layer = window_layer(project(), MONITOR, WORK, fullscreen=True, area=WORK)
    assert layer.rect == Rect(0, 0, 1920, 1080)
    assert layer.crop == (0, 32, 0, 0) and layer.edge == 0


def test_fullscreen_window_is_clipped_to_the_work_area_and_fills():
    layer = window_layer(project(), MONITOR, MONITOR, fullscreen=True, area=WORK)
    assert layer.rect == Rect(0, 0, 1920, 1080) and layer.crop == (0, 32, 0, 0)


def test_window_is_clipped_to_the_screen():
    layer = window_layer(project(), MONITOR, Rect(-100, -100, 600, 400))
    assert layer.crop[:2] == (0, 0)
    assert layer.rect.x == 0 and layer.rect.y == 0


def test_scenes():
    p = project()
    cam = compose(p, "camera", MONITOR, None)
    assert cam.camera.rect == p.camera.closeup.rect and cam.camera.alpha == 1
    assert cam.screen.alpha == 0
    share = compose(p, "share", MONITOR, Rect(10, 10, 500, 500), previous=cam)
    assert share.camera.rect == p.camera.overlay.rect
    # the window plus its 3 px frame
    assert share.screen.alpha == 1 and share.screen.rect == Rect(7, 7, 506, 506)
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
