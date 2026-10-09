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
# Residual echo suppression: what the linear filter cannot model (the room's long
# tail, the speakers' distortion) is attenuated where the echo dominates.
TAIL = 0.3                  # seconds for the echo estimate's tail to fall by 60 dB
OVER = 1.0                  # how much stronger than the estimate the residual is assumed
FLOOR = 0.3                 # lowest gain (-10 dB): gentle, the voice comes first


def available() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _decode(src: str, track: int, dst: Path):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", src, "-map", f"0:a:{track}", "-ac", "1",
                    "-ar", str(RATE), "-f", "f32le", str(dst)], check=True)


def cancel(src: str | Path, dst: str | Path, frame: int = 1024) -> list[float] | None:
    """Write to `dst` (WAV, mono) the first audio track of `src` without the echo of the
    second one. None when there is nothing to cancel (no or silent system sound).

    Returns, per `frame` samples, how many dB the cleaned microphone stands above the
    echo that was removed there: low values mean the microphone held only the echo
    (the speaker was silent), which voice activity detection alone cannot tell, since
    what the speakers play is often speech too."""
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="rec0-echo-") as tmp:
        mic_raw, ref_raw, out_raw = Path(tmp, "mic.f32"), Path(tmp, "ref.f32"), Path(tmp, "out.f32")
        _decode(str(src), 0, mic_raw)
        _decode(str(src), 1, ref_raw)
        mic = np.memmap(mic_raw, np.float32, "r")
        ref = np.memmap(ref_raw, np.float32, "r")
        n = min(len(mic), len(ref))
        if n < N * 4 or float(np.mean(np.square(ref[:n], dtype=np.float64))) < SILENT:
            return None
        out = np.memmap(out_raw, np.float32, "w+", shape=(len(mic),))
        out[n:] = mic[n:]
        removed = np.zeros(len(mic), np.float32)
        chunk, margin, fade = int(CHUNK * RATE), int(MARGIN * RATE), int(FADE * RATE)
        for c0 in range(0, n, chunk):
            c1 = min(n, c0 + chunk)
            lo, hi = max(0, c0 - margin), min(n, c1 + margin)
            clean, echo = _cancel_span(np.asarray(mic[lo:hi], np.float64), np.asarray(ref[lo:hi], np.float64))
            # Keep [c0 - fade, c1); the overlap with the previous chunk is crossfaded.
            a = max(lo, c0 - fade)
            for buf, sig in ((out, clean), (removed, echo)):
                part = sig[a - lo:c1 - lo]
                if c0 > 0:
                    ramp = np.linspace(0.0, 1.0, c0 - a, endpoint=False)
                    part[:c0 - a] = buf[a:c0] * (1 - ramp) + part[:c0 - a] * ramp
                buf[a:c1] = part
        out.flush()
        frames = len(mic) // frame
        energy = lambda x: np.mean(np.square(x[:frames * frame].reshape(frames, frame), dtype=np.float64), axis=1)
        ratio = 10 * np.log10((energy(out) + 1e-12) / (energy(removed) + 1e-12))
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(RATE), "-ac", "1",
                        "-i", str(out_raw), "-c:a", "pcm_f32le", str(dst)], check=True)
    return [float(r) for r in ratio]


def _suppression(pyy, pee):
    """Per-bin gain against the residual echo. The echo estimate is extended with an
    exponential tail (the reverberation the filter does not reach); where it dominates
    the residual, the bin is attenuated, down to FLOOR. Where the voice dominates the
    gain stays at 1, and without system sound there is nothing to attenuate."""
    import numpy as np

    decay = 10 ** (-6 * HOP / RATE / TAIL)          # -60 dB over TAIL, per frame
    tail = np.empty_like(pyy)
    level = np.zeros(pyy.shape[1], pyy.dtype)
    smooth = np.zeros_like(level)
    for t in range(len(pyy)):
        level = np.maximum(pyy[t], level * decay)
        tail[t] = level
    gain = np.empty_like(pyy)
    g = np.ones(pyy.shape[1], pyy.dtype)
    for t in range(len(pyy)):
        smooth = 0.5 * smooth + 0.5 * pee[t]
        target = np.clip(1 - OVER * tail[t] / (smooth + 1e-12), FLOOR, 1)
        # Fast to open (the voice starts), slower to close: no pumping between syllables.
        g = np.where(target > g, target, 0.7 * g + 0.3 * target)
        gain[t] = g
    return gain


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
    Y = np.zeros_like(M)                            # the echo estimate
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
            e, y = m, np.zeros_like(m)              # no system sound here: nothing to remove
        else:
            R = np.einsum("tfk,tfj->fkj", x, x.conj())
            p = np.einsum("tfk,tf->fk", x, m.conj())
            reg = 1e-3 * np.trace(R, axis1=1, axis2=2).real / TAPS + 1e-10
            w = np.linalg.solve(R + reg[:, None, None] * eye, p[..., None])[..., 0].conj()
            y = np.einsum("tfk,fk->tf", x, w)
            e = m - y
        E[s:s + B] += e * taper[:, None]
        Y[s:s + B] += y * taper[:, None]
        weight[s:s + B] += taper
    E /= np.maximum(weight, 1e-8)[:, None]
    Y /= np.maximum(weight, 1e-8)[:, None]
    E *= _suppression(np.abs(Y) ** 2, np.abs(E) ** 2)

    norm = np.zeros(len(mic_p))
    for i in range(T):
        norm[i * HOP:i * HOP + N] += win ** 2
    covered = norm > 1e-3

    def istft(S):
        frames = np.fft.irfft(S, N, axis=1) * win
        out = np.zeros(len(mic_p))
        for i in range(T):
            out[i * HOP:i * HOP + N] += frames[i]
        out[covered] /= norm[covered]
        return out

    clean, echo = istft(E), istft(Y)
    # Edges that no frame covers fully keep the microphone as is.
    clean[~covered] = mic_p[~covered]
    echo[~covered] = 0
    return clean[:n], echo[:n]
