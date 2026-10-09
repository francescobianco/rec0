"""Removes the system sound that the microphone picked up from the speakers.

With speakers instead of headphones, what plays on the computer (a video, a
song) reaches the microphone too, delayed and coloured by the room. Processed
as if it were voice, that copy is raised with it and lands on top of the clean
system sound track: an echo, a "room" sound. Since the system sound is
recorded on its own track, it is the exact reference of what the speakers
played, and the copy can be estimated and subtracted from the microphone.

The room's response is estimated per frequency bin with least squares over
short blocks (STFT, 16 frames of history: ~85 ms of delay and early
reflections). The voice is uncorrelated with the reference, so talking over
the system sound does not bias the estimate (no double-talk detector needed),
and short blocks follow the slow drift between the microphone's clock and the
sound card's.

Needs numpy; without it the microphone is used as recorded.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

RATE = 48000
N, HOP = 1024, 256          # STFT frame and hop (21 ms, 5 ms)
TAPS = 16                   # frames of reference history per bin (~85 ms)
BLOCK = 2.0                 # seconds per least-squares block (half overlapping): longer
                            # estimates better, shorter follows clock drift better
CHUNK = 20.0                # seconds processed at a time
MARGIN = 1.0                # context around each chunk
FADE = 0.05                 # crossfade between chunks
SILENT = 1e-7               # mean square below which the reference is silence


def available() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _decode(src: str, track: int, dst: Path):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", src, "-map", f"0:a:{track}", "-ac", "1",
                    "-ar", str(RATE), "-f", "f32le", str(dst)], check=True)


def cancel(src: str | Path, dst: str | Path) -> bool:
    """Write to `dst` (WAV, mono) the first audio track of `src` without the echo of the
    second one. False when there is nothing to cancel (no or silent system sound)."""
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="rec0-echo-") as tmp:
        mic_raw, ref_raw, out_raw = Path(tmp, "mic.f32"), Path(tmp, "ref.f32"), Path(tmp, "out.f32")
        _decode(str(src), 0, mic_raw)
        _decode(str(src), 1, ref_raw)
        mic = np.memmap(mic_raw, np.float32, "r")
        ref = np.memmap(ref_raw, np.float32, "r")
        n = min(len(mic), len(ref))
        if n < N * 4 or float(np.mean(np.square(ref[:n], dtype=np.float64))) < SILENT:
            return False
        out = np.memmap(out_raw, np.float32, "w+", shape=(len(mic),))
        out[n:] = mic[n:]
        chunk, margin, fade = int(CHUNK * RATE), int(MARGIN * RATE), int(FADE * RATE)
        for c0 in range(0, n, chunk):
            c1 = min(n, c0 + chunk)
            lo, hi = max(0, c0 - margin), min(n, c1 + margin)
            clean = _cancel_span(np.asarray(mic[lo:hi], np.float64), np.asarray(ref[lo:hi], np.float64))
            # Keep [c0 - fade, c1); the overlap with the previous chunk is crossfaded.
            a = max(lo, c0 - fade)
            part = clean[a - lo:c1 - lo]
            if c0 > 0:
                ramp = np.linspace(0.0, 1.0, c0 - a, endpoint=False)
                part[:c0 - a] = out[a:c0] * (1 - ramp) + part[:c0 - a] * ramp
            out[a:c1] = part
        out.flush()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(RATE), "-ac", "1",
                        "-i", str(out_raw), "-c:a", "pcm_f32le", str(dst)], check=True)
    return True


def _cancel_span(mic, ref):
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    n = len(mic)
    pad = (-(n - N) % HOP) if n > N else N - n
    mic_p, ref_p = np.pad(mic, (0, pad)), np.pad(ref, (0, pad))
    win = np.sqrt(np.hanning(N + 1)[:N])

    def stft(x):
        frames = sliding_window_view(x, N)[::HOP]
        return np.fft.rfft(frames * win, axis=1).astype(np.complex64)

    M, X = stft(mic_p), stft(ref_p)
    T, F = M.shape
    # History view: hist[t, f, k] = X[t - k] (zeros before the start).
    Xp = np.concatenate([np.zeros((TAPS - 1, F), X.dtype), X])
    hist = sliding_window_view(Xp, TAPS, axis=0)[:, :, ::-1]

    E = np.zeros_like(M)
    weight = np.zeros(T, np.float32)
    B = min(T, max(TAPS * 4, int(BLOCK * RATE / HOP)))
    starts = list(range(0, T - B + 1, B // 2))
    if starts[-1] + B < T:
        starts.append(T - B)
    taper = np.hanning(B + 2)[1:-1].astype(np.float32)
    eye = np.eye(TAPS, dtype=np.complex64)
    for s in starts:
        x, m = hist[s:s + B], M[s:s + B]
        if float(np.mean(np.abs(x[:, :, 0]) ** 2)) < SILENT * N:
            e = m                                   # no system sound here: nothing to remove
        else:
            R = np.einsum("tfk,tfj->fkj", x, x.conj())
            p = np.einsum("tfk,tf->fk", x, m.conj())
            reg = 1e-3 * np.trace(R, axis1=1, axis2=2).real / TAPS + 1e-10
            w = np.linalg.solve(R + reg[:, None, None] * eye, p[..., None])[..., 0].conj()
            e = m - np.einsum("tfk,fk->tf", x, w)
        E[s:s + B] += e * taper[:, None]
        weight[s:s + B] += taper
    E /= np.maximum(weight, 1e-8)[:, None]

    frames = np.fft.irfft(E, N, axis=1) * win
    out = np.zeros(len(mic_p))
    norm = np.zeros(len(mic_p))
    for i in range(T):
        out[i * HOP:i * HOP + N] += frames[i]
        norm[i * HOP:i * HOP + N] += win ** 2
    # Edges that no frame covers fully keep the microphone as is.
    covered = norm > 1e-3
    out[covered] /= norm[covered]
    out[~covered] = mic_p[~covered]
    return out[:n]
