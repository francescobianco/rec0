"""Stereo reference for the canceller: the two speakers reach the microphone through two different
paths; a mono (L+R) reference cannot model stereo content. Measured as total residual reduction."""
import os, sys, subprocess, numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rec0 import echo
R = 48000
N, HOP = echo.N, echo.HOP

def read(path, track, pan):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.expanduser(path), "-map", f"0:a:{track}", "-ar", str(R),
                          "-af", pan, "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)
def db(x): return 10 * np.log10(np.mean(x ** 2) + 1e-12)

def cancel_multi(mic, refs, taps=16, block=2.0):
    """echo._cancel_span generalised to several reference channels (stacked taps per bin)."""
    n = len(mic)
    pad = (-(n - N) % HOP) if n > N else N - n
    win = np.sqrt(np.hanning(N + 1)[:N])
    def stft(x):
        frames = sliding_window_view(np.pad(x, (0, pad)), N)[::HOP]
        return np.fft.rfft(frames * win, axis=1).astype(np.complex64)
    M = stft(mic); T, F = M.shape
    hists = []
    for r in refs:
        X = stft(r)
        Xp = np.concatenate([np.zeros((taps - 1, F), X.dtype), X])
        hists.append(sliding_window_view(Xp, taps, axis=0)[:, :, ::-1])
    hist = np.concatenate(hists, axis=2)           # [t, f, taps * channels]
    K = hist.shape[2]
    E = np.zeros_like(M); weight = np.zeros(T, np.float32)
    B = min(T, max(K * 4, int(block * R / HOP)))
    starts = list(range(0, T - B + 1, B // 2))
    if starts[-1] + B < T: starts.append(T - B)
    taper = np.hanning(B + 2)[1:-1].astype(np.float32); eye = np.eye(K, dtype=np.complex64)
    for s in starts:
        x, m = hist[s:s + B], M[s:s + B]
        Rm = np.einsum("tfk,tfj->fkj", x, x.conj()); p = np.einsum("tfk,tf->fk", x, m.conj())
        reg = 1e-3 * np.trace(Rm, axis1=1, axis2=2).real / K + 1e-10
        w = np.linalg.solve(Rm + reg[:, None, None] * eye, p[..., None])[..., 0].conj()
        e = m - np.einsum("tfk,fk->tf", x, w)
        E[s:s + B] += e * taper[:, None]; weight[s:s + B] += taper
    E /= np.maximum(weight, 1e-8)[:, None]
    norm = np.zeros(n + pad)
    for i in range(T): norm[i * HOP:i * HOP + N] += win ** 2
    frames = np.fft.irfft(E, N, axis=1) * win
    out = np.zeros(n + pad)
    for i in range(T): out[i * HOP:i * HOP + N] += frames[i]
    cov = norm > 1e-3; out[cov] /= norm[cov]
    return out[:n]

cases = {
    "185025 8-28 (YouTube)": ("~/Videos/rec0/dev/dev-20261009-185025.original.mp4", 8, 28),
    "190333 13-30 (PC speech)": ("~/Videos/rec0/dev/dev-20261009-190333.original.mp4", 13, 30),
    "190333 36-60 (music)": ("~/Videos/rec0/dev/dev-20261009-190333.original.mp4", 36, 60),
}
for name, (path, a, b) in cases.items():
    mic = read(path, 0, "pan=mono|c0=c0")
    L, Rr = read(path, 1, "pan=mono|c0=c0"), read(path, 1, "pan=mono|c0=c1")
    mono = (L + Rr) / 2
    sl = slice(a * R, b * R)
    print(f"== {name}: mic {db(mic[sl]):.1f} dB; system L {db(L[sl]):.1f} R {db(Rr[sl]):.1f}, (L-R)/2 vs (L+R)/2: {db(((L - Rr) / 2)[sl]) - db(mono[sl]):+.1f} dB", flush=True)
    seg = slice(1 * R, (b - a + 1) * R)
    m = mic[(a - 1) * R:(b + 1) * R]
    for label, refs, taps in (("mono ref, 16 taps (current)", [mono], 16), ("stereo ref, 16 taps each", [L, Rr], 16),
                              ("stereo ref, 24 taps each", [L, Rr], 24)):
        out = cancel_multi(m, [r[(a - 1) * R:(b + 1) * R] for r in refs], taps)
        print(f"   {label:32s} residual {db(out[seg]) - db(m[seg]):+6.1f} dB", flush=True)
