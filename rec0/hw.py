"""Hardware video acceleration (VA-API), used when it really works.

Encoding H.264 and decoding the webcam's MJPEG on the GPU takes most of the
load off the CPU (measured on Intel UHD 730: encoding 1080p30 ~42% -> ~3% of
a core, MJPEG decoding 33% -> 6%). Each path is tried once with a tiny
pipeline; anything failing falls back to software. REC0_NO_HW=1 forces software.
"""

from __future__ import annotations

import functools
import os

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)


@functools.cache
def _works(desc: str) -> bool:
    if os.environ.get("REC0_NO_HW"):
        return False
    try:
        pipeline = Gst.parse_launch(desc)
    except Exception:  # noqa: BLE001 - missing element or bad caps: not usable
        return False
    pipeline.set_state(Gst.State.PLAYING)
    msg = pipeline.get_bus().timed_pop_filtered(3 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
    return msg is not None and msg.type == Gst.MessageType.EOS


def h264_encoder() -> bool:
    return _works("videotestsrc num-buffers=3 ! video/x-raw,width=320,height=240,format=NV12 "
                  "! vah264lpenc ! h264parse ! fakesink")


_disabled: set[str] = set()


def jpeg_decoder() -> bool:
    """vajpegdec needs caps details (SOF marker, sampling) that webcams give and a
    synthetic test JPEG does not, so it cannot be probed: it is used when present,
    and the recorder falls back to software if it refuses the webcam (disable())."""
    if os.environ.get("REC0_NO_HW") or "jpeg" in _disabled:
        return False
    return Gst.ElementFactory.find("vajpegdec") is not None and Gst.ElementFactory.find("vapostproc") is not None


def disable(what: str):
    _disabled.add(what)
