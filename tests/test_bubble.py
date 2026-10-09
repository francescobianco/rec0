"""Geometry of the bubble's resize handle."""

from rec0.bubble import MIN_SIZE, handle_center, handle_corner, resized

AREA = (0, 32, 1920, 1048)   # work area under GNOME's top bar


def test_handle_bottom_right_with_room():
    assert handle_corner(100, 100, 200, AREA) == (1, 1)


def test_handle_flips_near_right_or_bottom_edge():
    assert handle_corner(1680, 100, 200, AREA) == (-1, 1)      # too far right
    assert handle_corner(100, 840, 200, AREA) == (1, -1)       # too low
    assert handle_corner(1680, 840, 200, AREA) == (-1, -1)     # default bottom-right position


def test_handle_on_the_ring():
    from rec0.bubble import OUTLINE, ring_width
    for corner in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        cx, cy, hr = handle_center(200, corner)
        distance = ((cx - 100) ** 2 + (cy - 100) ** 2) ** 0.5
        assert abs(distance - (100 - OUTLINE - ring_width(200) / 2)) < 0.01   # centred on the white ring
        assert (cx > 100) == (corner[0] > 0) and (cy > 100) == (corner[1] > 0)
        assert hr <= cx <= 200 - hr and hr <= cy <= 200 - hr                    # inside the bubble's window


def test_resize_keeps_the_opposite_corner():
    # Bottom-right handle: top-left stays.
    assert resized((100, 100, 200), (1, 1), 50, 50, AREA) == (100, 100, 250)
    # Top-left handle: bottom-right (1880, 1040) stays, growing up and left.
    x, y, size = resized((1680, 840, 200), (-1, -1), -50, -50, AREA)
    assert (size, x + size, y + size) == (250, 1880, 1040)


def test_resize_limits():
    assert resized((100, 100, 200), (1, 1), -500, -500, AREA)[2] == MIN_SIZE
    assert resized((100, 100, 200), (1, 1), 5000, 5000, AREA)[2] == int(1048 * 0.6)


def test_controller_reports_moves_and_final_size():
    import io
    from types import SimpleNamespace

    from gi.repository import GLib

    from rec0.bubble import Bubble

    moves, sizes = [], []
    b = Bubble(200, 0, 0, on_move=lambda *a: moves.append(a), on_resize=sizes.append)
    b._proc = SimpleNamespace(stdout=io.BytesIO(b"10 20 200\n10 20 230\n10 20 260\nsize 260\n"))
    b._reader()
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)
    assert moves[-1] == (10, 20, 260) and (b.pos, b.size) == ((10, 20), 260)
    assert sizes == [260]
