from rec0.project import Rect, parse
from rec0.scenes import Shown, bubble_rect, compose, interpolate, to_canvas, window_layer

MONITOR = Rect(0, 0, 1920, 1080)
WORK = Rect(0, 32, 1920, 1048)   # GNOME's top bar takes the first 32 px


def project(**over):
    data = {"video": {"resolution": "1920x1080"}, "camera": {"device": "test"},
            "windows": ["Firefox"], "screen": {"margin": 0}}
    data.update(over)
    return parse(data)


def test_window_keeps_real_position_and_gets_an_outside_frame():
    layer = window_layer(project(), MONITOR, Rect(100, 50, 800, 600), shadow=(26, 23, 26, 29))
    # 3 px of frame around the window; videobox crops the shadows minus that room.
    assert layer.rect == Rect(97, 47, 806, 606)
    assert layer.crop == (23, 20, 23, 26)
    assert layer.edge == 3 and layer.radius == 12


def test_window_without_shadows_gets_transparent_room_for_the_frame():
    layer = window_layer(project(), MONITOR, Rect(100, 50, 800, 600))
    assert layer.crop == (-3, -3, -3, -3)


def test_large_aspect_mismatch_is_fitted_without_distortion():
    p = project(screen={"margin": 108})
    layer = window_layer(p, MONITOR, MONITOR, fullscreen=True)
    assert layer.rect == Rect(192, 108, 1536, 864) and layer.edge == 0


def test_work_area_corners_are_canvas_corners():
    p = project()
    r = to_canvas(p, WORK, Rect(0, 32 + 1048 - 200, 200, 200))
    assert (r.x, r.y + r.height) == (0, 1080)
    top_right = to_canvas(p, WORK, Rect(1920 - 200, 32, 200, 200))
    assert (top_right.x + top_right.width, top_right.y) == (1920, 0)


def test_maximized_window_fills_the_video_without_frame():
    layer = window_layer(project(), WORK, WORK, fullscreen=True)
    assert layer.rect == Rect(0, 0, 1920, 1080) and layer.crop == (0, 0, 0, 0) and layer.edge == 0


def test_fullscreen_window_is_clipped_to_the_work_area_and_fills():
    layer = window_layer(project(), WORK, MONITOR, fullscreen=True)
    assert layer.rect == Rect(0, 0, 1920, 1080) and layer.crop == (0, 32, 0, 0)


def test_window_partly_off_screen_is_clipped():
    layer = window_layer(project(), MONITOR, Rect(-100, -100, 600, 400))
    assert layer.rect.x == 0 and layer.rect.y == 0
    # the frame on those sides is off-screen too: only the window content is cut
    assert layer.crop[0] == 100 and layer.crop[1] == 100


def test_scenes_keep_every_presented_window():
    p = project()
    a, b = Shown(1, Rect(10, 10, 500, 500)), Shown(2, Rect(600, 100, 400, 300))
    cam = compose(p, "camera", MONITOR, [a, b])
    assert cam.camera.rect == p.camera.closeup.rect and cam.camera.alpha == 1
    assert [l.alpha for _k, l in cam.windows] == [0, 0]
    share = compose(p, "share", MONITOR, [a, b], previous=cam)
    assert [k for k, _l in share.windows] == [1, 2]          # bottom to top
    assert all(l.alpha == 1 for _k, l in share.windows)
    assert share.camera.rect == p.camera.overlay.rect
    back = compose(p, "camera", MONITOR, [a, b], previous=share)
    # windows fade out where they were
    assert dict(back.windows)[1].rect == dict(share.windows)[1].rect


def test_unrevealed_window_stays_invisible():
    p = project()
    f = compose(p, "share", MONITOR, [Shown(1, Rect(10, 10, 500, 500), visible=False)])
    assert dict(f.windows)[1].alpha == 0


def test_without_screen_capture():
    assert compose(project(windows=[]), "camera", None, []).windows == ()


def test_interpolate_endpoints_and_new_windows_fade_in():
    p = project()
    a = compose(p, "camera", MONITOR, [])
    b = compose(p, "share", MONITOR, [Shown(1, Rect(10, 10, 500, 500))], previous=a)
    assert interpolate(a, b, 0).camera == a.camera
    assert interpolate(a, b, 1) == b
    mid = dict(interpolate(a, b, 0.5).windows)[1]
    assert 0 < mid.alpha < 1 and mid.rect == dict(b.windows)[1].rect


def test_overlay_follows_bubble():
    p = project()
    b = bubble_rect(p.camera.bubble, WORK)
    assert b == Rect(1920 - 200 - 40, 32 + 1048 - 200 - 40, 200, 200)
    f = compose(p, "share", WORK, [Shown(1, Rect(0, 32, 900, 500))], bubble=b)
    assert f.camera.rect == to_canvas(p, WORK, b)
