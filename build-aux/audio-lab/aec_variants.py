"""Echo canceller variants, measured as total residual energy where the speaker is silent
(185025 s 8-28: a YouTube video through the speakers, nobody talking) and where a podcast
plays near the microphone (190333 s 13-30: PC speech; s 36-60: music)."""
import os, sys, subprocess, numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rec0 import echo
R = 48000
def read(path, track):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.expanduser(path), "-map", f"0:a:{track}", "-ar", str(R),
                          "-af", "pan=mono|c0=c0", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)
def db(x): return 10 * np.log10(np.mean(x ** 2) + 1e-12)
def lag_frac(a, b, max_s=4800):
    n = min(len(a), len(b)); nf = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(a[:n], nf) * np.conj(np.fft.rfft(b[:n], nf)), nf)
    c = np.concatenate([c[-max_s:], c[:max_s + 1]]); k = int(np.argmax(np.abs(c)))
    # parabolic interpolation
    if 0 < k < len(c) - 1:
        y0, y1, y2 = abs(c[k - 1]), abs(c[k]), abs(c[k + 1]); k += 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2 + 1e-12)
    return k - max_s
def drift_ppm(mic, ref, a, b):
    """Lag at the start and at the end of [a, b] seconds: samples of drift per second."""
    w = 5 * R
    l0 = lag_frac(mic[a * R:a * R + w], ref[a * R:a * R + w])
    l1 = lag_frac(mic[b * R - w:b * R], ref[b * R - w:b * R])
    return l0, l1, (l1 - l0) / ((b - a - 5)) / R * 1e6
def warp(ref, ppm):
    idx = np.arange(len(ref)) * (1 + ppm * 1e-6)
    return np.interp(idx, np.arange(len(ref)), ref)
cases = {
    "185025 silent speaker 8-28": ("~/Videos/rec0/dev/dev-20261009-185025.original.mp4", 8, 28),
    "190333 podcast+speech 13-30": ("~/Videos/rec0/dev/dev-20261009-190333.original.mp4", 13, 30),
    "190333 podcast+music 36-60": ("~/Videos/rec0/dev/dev-20261009-190333.original.mp4", 36, 60),
}
data = {}
for name, (path, a, b) in cases.items():
    mic, ref = read(path, 0), read(path, 1)
    l0, l1, ppm = drift_ppm(mic, ref, a, b)
    print(f"{name}: mic {db(mic[a*R:b*R]):.1f} dB, lag {l0/48:.2f} -> {l1/48:.2f} ms, drift {ppm:+.1f} ppm", flush=True)
    data[name] = (mic, ref, a, b, ppm)
variants = [
    ("current (taps 16, block 2 s)", dict(), False),
    ("taps 32", dict(TAPS=32), False),
    ("block 4 s", dict(BLOCK=4.0), False),
    ("taps 32, block 4 s", dict(TAPS=32, BLOCK=4.0), False),
    ("drift-warped ref, taps 16", dict(), True),
    ("drift-warped ref, taps 32, block 4 s", dict(TAPS=32, BLOCK=4.0), True),
    ("drift-warped ref, taps 32, block 8 s", dict(TAPS=32, BLOCK=8.0), True),
]
defaults = {k: getattr(echo, k) for k in ("TAPS", "BLOCK")}
for label, over, warped in variants:
    for k, v in {**defaults, **over}.items():
        setattr(echo, k, v)
    row = []
    for name, (mic, ref, a, b, ppm) in data.items():
        r = warp(ref, ppm) if warped else ref
        lo, hi = max(0, (a - 1) * R), min(len(mic), (b + 1) * R)
        clean, est = echo._cancel_span(mic[lo:hi], r[lo:hi])
        seg = slice((a - (a - 1)) * R if lo else a * R, (b - (a - 1)) * R if lo else b * R)
        m = mic[lo:hi]
        row.append(f"{db(clean[seg]) - db(m[seg]):+6.1f} dB")
    print(f"{label:40s} " + "  ".join(row), flush=True)
