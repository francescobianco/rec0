"""Takes the room out of the voice: late reverberation suppression.

A microphone at arm's length in an untreated room picks up the voice and then
the room's reflections of it, which go on for a few tenths of a second after
every syllable: the "echo" of a bare room, which the leveler and the compressor
would then raise between the words. Nothing but the recording is available, so
both the measurement and the cure are blind:

- The room's decay time is read off the recording itself. After the end of a
  word the energy falls, and it cannot fall faster than the room lets it: the
  fastest sustained decays of the 300-4000 Hz band bound the room's
  reverberation time (RT60, the time to fall by 60 dB). Dry speech alone gives
  about 0.13 s (the decay of the voice itself); rooms give 0.2-0.6 s.
- What the sound of `START` seconds ago predicts at the present moment through
  that exponential decay is the late reverberation; it is subtracted per
  frequency bin (Lebart, Boucher and Denbigh 2001), over-subtracted by half
  and with a floor so the voice is never torn. Early reflections, within `START`, are kept: they are part of
  how a voice sounds in a room, and of its intelligibility.

The STFT has no delay (frame-aligned overlap-add, as in rec0/echo.py), so the
voice stays in sync with the picture. Needs numpy; without it the room stays.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

RATE = 48000
N, HOP = 1024, 256          # STFT frame and hop (21 ms, 5 ms)
BAND = (300.0, 4000.0)      # Hz: where speech decays are cleanest
START = 0.05                # s: late reverberation begins here; earlier reflections are kept
FLOOR = 0.056               # lowest gain (-25 dB): the tail is lowered, not cut
OVER = 1.5                  # over-subtraction: the predicted tail counts this much (2 colours the voice)
SMOOTH = 0.6                # one-pole smoothing of the power spectrum (about 13 ms)
MIN_RUN = 12                # frames (64 ms) a decay must last to count
MIN_DROP = 15.0             # dB a decay must fall to count
ABOVE_FLOOR = 25.0          # dB above the quietest frames a decay must start
NEAR_FLOOR = 6.0            # dB above them where a decay ends: below, the noise floor flattens it
MIN_DECAYS = 5              # fewer: no estimate
CHUNK = 20.0                # seconds processed at a time
MARGIN = 1.0                # context around each chunk
FADE = 0.05                 # crossfade between chunks


def available() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _decode(src: str, dst: Path, limit: float | None = None):
    head = ["-t", str(limit)] if limit else []
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *head, "-i", src, "-map", "0:a:0", "-ac", "1",
                    "-ar", str(RATE), "-f", "f32le", str(dst)], check=True)


def _window():
    import numpy as np
    return np.sqrt(np.hanning(N + 1)[:N])


def _stft(x):
    """Frames start every HOP samples from 0; the last partial frame is zero padded."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    pad = (-(len(x) - N) % HOP) if len(x) > N else N - len(x)
    xp = np.pad(x, (0, pad))
    return np.fft.rfft(sliding_window_view(xp, N)[::HOP] * _window(), axis=1), xp


def _istft(S, xp):
    import numpy as np
    win = _window()
    frames = np.fft.irfft(S, N, axis=1) * win
    out, norm = np.zeros(len(xp)), np.zeros(len(xp))
    for i in range(len(frames)):
        out[i * HOP:i * HOP + N] += frames[i]
        norm[i * HOP:i * HOP + N] += win ** 2
    covered = norm > 1e-3
    out[covered] /= norm[covered]
    out[~covered] = xp[~covered]      # edges no frame covers fully stay as they were
    return out


def decay_rates(env, hop_s: float) -> list[float]:
    """Decay rates (dB/s, positive) of the sustained falls of a dB envelope."""
    import numpy as np
    env = np.convolve(env, np.ones(3) / 3, mode="same")
    floor = np.percentile(env, 10)
    rates, i = [], 0
    while i < len(env) - MIN_RUN:
        j = i
        while j + 1 < len(env) and env[j + 1] <= env[j] + 0.5 and env[j + 1] > floor + NEAR_FLOOR:
            j += 1
        if j - i >= MIN_RUN and env[i] - env[j] >= MIN_DROP and env[i] > floor + ABOVE_FLOOR:
            rates.append(float(-np.polyfit(np.arange(i, j + 1) * hop_s, env[i:j + 1], 1)[0]))
        i = j + 1
    return rates


def reverberation_time(x) -> float | None:
    """Blind RT60 (s) of a mono signal at RATE, or None when it holds too few decays."""
    import numpy as np
    f = np.fft.rfftfreq(N, 1 / RATE)
    band = (f >= BAND[0]) & (f < BAND[1])
    env = []
    step = int(CHUNK * RATE) // HOP * HOP
    for c0 in range(0, max(1, len(x) - N + 1), step):
        X, _ = _stft(np.asarray(x[c0:c0 + step + N - HOP], np.float64))
        env.append(10 * np.log10((np.abs(X[:, band]) ** 2).sum(axis=1) + 1e-12))
    rates = decay_rates(np.concatenate(env), HOP / RATE)
    if len(rates) < MIN_DECAYS:
        return None
    fastest = np.sort(rates)[-max(1, len(rates) // 5):]
    return float(60 / np.median(fastest))


def decay_time(src: str | Path, limit: float | None = None) -> float | None:
    """Blind RT60 (s) of the first audio track of `src` (its first `limit` seconds)."""
    import numpy as np
    with tempfile.TemporaryDirectory(prefix="rec0-room-") as tmp:
        raw = Path(tmp, "voice.f32")
        _decode(str(src), raw, limit)
        x = np.memmap(raw, np.float32, "r")
        if len(x) < N * 4:
            return None
        return reverberation_time(x)


def _suppress_span(x, rt60: float):
    """Late reverberation suppression of one span (numpy array at RATE)."""
    import numpy as np
    X, xp = _stft(x)
    P = np.abs(X) ** 2
    smoothed = np.empty_like(P)
    acc = P[0].copy()
    for t in range(len(P)):
        acc = SMOOTH * acc + (1 - SMOOTH) * P[t]
        smoothed[t] = acc
    # The tail predicted from START seconds ago: energy falls by 60 dB in rt60 seconds.
    late_frames = max(1, round(START * RATE / HOP))
    decay = np.exp(-2 * (3 * np.log(10) / rt60) * START)
    late = np.zeros_like(P)
    late[late_frames:] = OVER * decay * smoothed[:-late_frames]
    gain = np.maximum(1 - late / np.maximum(smoothed, 1e-12), FLOOR)
    for t in range(1, len(gain)):                   # at most -6 dB per frame, against flutter
        gain[t] = np.maximum(gain[t], 0.5 * gain[t - 1])
    return _istft(X * gain, xp)[:len(x)]


def suppress(src: str | Path, dst: str | Path, rt60: float, limit: float | None = None):
    """Write to `dst` (WAV, mono) the first audio track of `src` with its late
    reverberation lowered, for a room whose decay time is `rt60` seconds."""
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="rec0-room-") as tmp:
        raw, out_raw = Path(tmp, "voice.f32"), Path(tmp, "out.f32")
        _decode(str(src), raw, limit)
        x = np.memmap(raw, np.float32, "r")
        n = len(x)
        out = np.memmap(out_raw, np.float32, "w+", shape=(n,))
        chunk, margin, fade = int(CHUNK * RATE), int(MARGIN * RATE), int(FADE * RATE)
        for c0 in range(0, n, chunk):
            c1 = min(n, c0 + chunk)
            lo, hi = max(0, c0 - margin), min(n, c1 + margin)
            clean = _suppress_span(np.asarray(x[lo:hi], np.float64), rt60)
            a = max(lo, c0 - fade)                  # the overlap with the previous chunk is crossfaded
            part = clean[a - lo:c1 - lo]
            if c0 > 0:
                ramp = np.linspace(0.0, 1.0, c0 - a, endpoint=False)
                part[:c0 - a] = out[a:c0] * (1 - ramp) + part[:c0 - a] * ramp
            out[a:c1] = part
        out.flush()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(RATE), "-ac", "1",
                        "-i", str(out_raw), "-c:a", "pcm_f32le", str(dst)], check=True)
