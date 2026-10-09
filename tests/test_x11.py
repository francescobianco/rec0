from rec0.x11 import has_shape


def test_pointer_shape():
    arrow = [0] * 400 + [0xFF000000] * 176          # opaque shape inside transparent room
    assert has_shape(arrow)
    assert not has_shape([0] * 576)                  # blank: a hidden pointer
    assert not has_shape([0x49000000] * 576)         # filled translucent square
