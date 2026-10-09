"""Adaptive audio post-processing ("mastering") for recorded videos.

Not a fixed filter chain: the recording is analysed first, then each stage
("circuit") is switched on only when the measurements call for it, with
parameters derived from those measurements. The goal is that a first video
made with a cheap microphone in a noisy room already sounds clean, even and
as loud as YouTube expects.

    analyze()  measures the audio          (ffmpeg: astats, aspectralstats, loudnorm)
    plan()     decides the stages          (pure function: easy to test and tune)
    process()  renders the video with the processed audio; the video stream is copied

Stages, in signal order:
    declip      repair clipped peaks                        if samples hit full scale
    highpass    remove rumble / handling noise              always, cutoff by low-end energy
    dehum       notch mains hum and harmonics               if a 50/60 Hz line stands out
    preamp      raise a quiet voice before denoising        if the voice is far below a normal level
    denoise     neural (RNNoise) noise reduction            if the noise would be audible after leveling
    pauses      lower breaths, clicks and noise between     if what is not speech (by voice activity
                words, before anything can raise them      detection) would be audible
    leveler     even out distance-from-mic changes          if speech level wanders
    mud         cut boxiness around 250 Hz                  if low-mids are excessive
    presence    lift intelligibility around 3.5 kHz         if the voice sounds muffled
    deesser     tame harsh "s" sounds                       if sibilance is strong
    compressor  steady speech dynamics                      ratio from the dynamic range
    loudness    measured constant gain to platform target    always
    limiter     true-peak safety                            always
"""

from __future__ import annotations

import json
import math
import tempfile
import re
import shutil
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import echo
from .i18n import _, pkgdata

RATE = 48000
# Samples of delay that filters add to the voice (at RATE), undone after the chain so the
# voice stays in sync with the picture and the system sound (tests/test_audio.py checks them).
FILTER_DELAYS = {
    "arnndn": 480,            # RNNoise works on 10 ms frames
    "afftdn": 1200,           # spectral denoise: 25 ms of analysis window
}
LIMITER_DELAY = 96            # nominal 2 ms lookahead at RATE; actual ring delay is attack_samples - 1
ECHO_ONLY_DB = 0.0            # cleaned microphone below the echo removed there: the speaker is silent
AAC_DELAY = 1024              # samples: the AAC encoder's priming; MP4 skips it (edit list), Matroska does not
FRAME = 1024                  # analysis frame, samples (21.3 ms at 48 kHz)
WINDOW = FRAME / RATE         # ...in seconds

# Platform loudness targets (integrated LUFS, true peak dBTP).
TARGETS = {
    "youtube": (-14.0, -1.0),
    "podcast": (-16.0, -1.0),
    "broadcast": (-23.0, -1.0),
}

# Decision thresholds (dB). Tuned on speech recordings; kept together so they
# can be adjusted in one place.
SILENCE_DB = -90.0            # digital silence, ignored by the statistics
ACTIVE_ABOVE_NOISE = 10.0     # energy-only fallback: speech if this much above the floor
VAD_ABOVE_FLOOR = 6.0         # speech is at least this much above the quietest frames
VAD_FILL_GAP = 0.12           # s: gaps this short are inside words (stop consonants)
VAD_MIN_SPEECH = 0.10         # s: shorter bursts are clicks
VAD_PRE, VAD_POST = 0.06, 0.12  # s of margin around speech: onsets and decays
# (Longer values swallow the 0.3-0.5 s pauses where breaths are: measured on real
#  recordings, 0.25/0.12/0.25 turned 71% of speech frames into 93%.)
VAD_ATTACK, VAD_RELEASE = 0.03, 0.25   # s to open and close the pause gain
PAUSE_TARGET = -60.0          # where non-speech should sit after loudness normalisation
PAUSE_MAX_DEPTH = 24          # dB: deeper sounds gated, not natural
NOISE_TARGET = -68.0          # where the noise floor should end up after loudness normalisation
NOISE_MIN_REDUCTION = 4.0     # below this, denoising is not worth its artifacts
LEVELER_SPREAD = 4.0          # dB of speech level spread (stdev) that calls for leveling
HUM_PROMINENCE = 12.0         # dB a mains line must stand above its neighbours
MUD_EXCESS = 2.0              # dB of 150-350 Hz energy above the 0.3-3 kHz voice band
MUFFLED_DEFICIT = -26.0       # dB of 3-8 kHz energy below the voice band
SIBILANCE_EXCESS = -10.0      # dB of 5-9 kHz energy relative to the voice band
RUMBLE_EXCESS = -10.0         # dB of <80 Hz energy relative to the voice band
PREAMP_BELOW = -30.0          # speech level (dBFS) under which the preamp kicks in
PREAMP_TARGET = -24.0         # ...and where it brings the voice
PREAMP_PEAK_MAX = 6.0         # peaks may go this far over 0 dBFS (float) before the limiter
SPEECH_LOSS_MAX = 3.0         # dB of speech the denoiser may take before it is backed off
RNNOISE_REDUCTION = 14.0      # typical noise reduction of the RNNoise model (dB)
CLIP_RATIO = 2e-5             # fraction of samples at full scale that means clipping


class AudioError(Exception):
    pass


def available() -> bool:
    return shutil.which("ffmpeg") is not None


def rnnoise_model() -> str | None:
    """Bundled RNNoise model for speech (see assets/rnnoise/README.md)."""
    path = pkgdata("rnnoise/sh.rnnn")
    return str(path) if path.exists() else None


def _quote(value: str) -> str:
    """Quote a value for an ffmpeg filtergraph."""
    return "'" + value.replace("\\", "\\\\").replace("'", "'\\''") + "'"


@dataclass
class Analysis:
    duration: float
    has_audio: bool
    peak_db: float = SILENCE_DB
    clipped_ratio: float = 0.0
    noise_floor_db: float = SILENCE_DB
    speech_db: float = SILENCE_DB
    snr_db: float = 0.0
    speech_spread_db: float = 0.0
    active_ratio: float = 0.0
    bands: dict = field(default_factory=dict)     # band name -> dB relative to the voice band
    hum_hz: int | None = None
    hum_prominence_db: float = 0.0
    loudness: dict = field(default_factory=dict)  # loudnorm first pass
    levels: list = field(default_factory=list, repr=False)  # RMS dB of each analysis frame
    speech: list = field(default_factory=list, repr=False)  # VAD: True where someone is speaking
    pause_db: float = SILENCE_DB      # loud end of what is not speech (breaths, clicks, keyboard)

    def summary(self) -> str:
        return (f"speech {self.speech_db:.1f} dBFS, noise {self.noise_floor_db:.1f} dBFS, "
                f"SNR {self.snr_db:.1f} dB, spread {self.speech_spread_db:.1f} dB, "
                f"{self.loudness.get('input_i', '?')} LUFS")


@dataclass
class Stage:
    name: str
    enabled: bool
    reason: str
    filter: str = ""
    param: float = 0.0     # stage-specific value (pauses: depth in dB)


@dataclass
class Plan:
    stages: list[Stage]
    target_lufs: float
    target_tp: float

    @property
    def chain(self) -> str:
        return ",".join(s.filter for s in self.stages if s.enabled and s.filter)

    def describe(self) -> list[str]:
        return [f"{'●' if s.enabled else '○'} {s.name}: {s.reason}" for s in self.stages]


# --------------------------------------------------------------------------
# ffmpeg helpers

def _ffmpeg(args: list[str], progress: Callable[[float], None] | None = None,
            duration: float = 0.0) -> str:
    """Run ffmpeg, return stderr (where filters print their reports)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args]
    if progress is None:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise AudioError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffmpeg failed")
        return r.stderr
    cmd[1:1] = ["-progress", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration > 0:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
            except ValueError:
                pass
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise AudioError(err.strip().splitlines()[-1] if err.strip() else "ffmpeg failed")
    return err


def _mono(src: str) -> list[str]:
    return ["-i", src, "-vn", "-map", "0:a:0"]


def _null() -> list[str]:
    return ["-f", "null", "-"]


def probe_duration(path: str | Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def track_channels(path: str | Path) -> list[int]:
    """Channel count of each audio track."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=channels", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return [int(c) for c in r.stdout.split() if c.isdigit()]


def _to_stereo(channels: int) -> str:
    # A mono track is duplicated to both sides at full level (the default
    # upmix spreads it at -3 dB per side, which would lower it).
    up = "pan=stereo|c0=c0|c1=c0," if channels == 1 else ""
    return f"{up}aformat=sample_fmts=fltp:sample_rates={RATE}:channel_layouts=stereo"


def audio_tracks(path: str | Path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return len(r.stdout.split())


def has_audio(path: str | Path) -> bool:
    return audio_tracks(path) > 0


def _window_levels(stdout: str) -> list[float]:
    return [_db(v) for v in re.findall(r"RMS_level=(\S+)", stdout)]


def _features(path: str, head: list[str]) -> list[tuple[float, float, float]]:
    """(RMS dB, spectral flatness, spectral centroid Hz) of each analysis frame."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *head, *_mono(path), "-af",
         f"aformat=sample_fmts=flt:channel_layouts=mono,aresample={RATE},asetnsamples=n={FRAME}:p=0,"
         f"astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
         f"aspectralstats=win_size={FRAME}:overlap=0:measure=flatness+centroid,ametadata=print:file=-",
         *_null()], capture_output=True, text=True)
    out = []
    for block in r.stdout.split("frame:")[1:]:
        rms = re.search(r"Overall\.RMS_level=(\S+)", block)
        flat = re.search(r"\.1\.flatness=(\S+)", block)
        cent = re.search(r"\.1\.centroid=(\S+)", block)
        if not rms:
            continue
        try:
            fl = float(flat[1]) if flat else 1.0
            ce = float(cent[1]) if cent else 0.0
        except ValueError:
            fl, ce = 1.0, 0.0
        out.append((_db(rms[1]), fl if math.isfinite(fl) else 1.0, ce if math.isfinite(ce) else 0.0))
    return out


def vad(feats: list[tuple[float, float, float]]) -> list[bool]:
    """Voice activity per frame, from level and spectral shape.

    Voiced speech is harmonic (low spectral flatness) with its energy low in the
    spectrum; breaths, fans, keyboard and clicks are noise-like and bright.
    Thresholds sit between the quiet and the loud frames of this very file, so
    the detector adapts to the microphone and the room.
    """
    audible = sorted((f for f in feats if f[0] > SILENCE_DB), key=lambda f: f[0])
    if len(audible) < 20:
        return [False] * len(feats)
    n = len(audible)
    quiet, loud = audible[: max(1, n * 15 // 100)], audible[n * 70 // 100:]
    fl_q, fl_l = statistics.median(f[1] for f in quiet), statistics.median(f[1] for f in loud)
    ce_q, ce_l = statistics.median(f[2] for f in quiet), statistics.median(f[2] for f in loud)
    floor = statistics.median(f[0] for f in quiet)
    spectral = fl_q > 1.5 * fl_l     # otherwise the spectrum does not tell them apart
    thr_f = math.sqrt(max(fl_q, 1e-6) * max(fl_l, 1e-6))
    thr_c = (ce_q + ce_l) / 2

    def voiced(f):
        if f[0] < floor + VAD_ABOVE_FLOOR:
            return False
        if not spectral:
            return f[0] > floor + ACTIVE_ABOVE_NOISE
        return f[1] < thr_f or (f[2] < thr_c and f[1] < 1.5 * thr_f)

    raw = [voiced(f) for f in feats]
    # Median of 3: single-frame flips are not decisions.
    raw = [sorted(raw[max(0, i - 1):i + 2])[len(raw[max(0, i - 1):i + 2]) // 2] for i in range(len(raw))]
    return _smooth(raw)


def _runs(mask: list[bool]):
    i = 0
    while i < len(mask):
        j = i
        while j < len(mask) and mask[j] == mask[i]:
            j += 1
        yield mask[i], i, j
        i = j


def _smooth(mask: list[bool]) -> list[bool]:
    frames = lambda seconds: max(1, round(seconds / WINDOW))   # noqa: E731
    out = mask[:]
    # Short gaps are inside words (stops, unvoiced consonants): fill them.
    for val, i, j in list(_runs(out)):
        if not val and 0 < i and j < len(out) and j - i <= frames(VAD_FILL_GAP):
            out[i:j] = [True] * (j - i)
    # Very short bursts are clicks, not speech.
    for val, i, j in list(_runs(out)):
        if val and j - i < frames(VAD_MIN_SPEECH):
            out[i:j] = [False] * (j - i)
    # Keep a margin around speech: onsets ("s", "f") and decays are speech too.
    pre, post = frames(VAD_PRE), frames(VAD_POST)
    grown = out[:]
    for val, i, j in _runs(out):
        if val:
            grown[max(0, i - pre):min(len(out), j + post)] = [True] * (min(len(out), j + post) - max(0, i - pre))
    return grown


def pause_commands(a: Analysis, depth_db: float) -> str:
    """Sample-continuous fades at VAD transitions, including interrupted ramps."""
    low = 10 ** (-depth_db / 20)
    lines = []
    start, length, initial, target = 0, 1, 1.0, 1.0
    for sp, i, _end in _runs(a.speech):
        sample = i * FRAME
        current = initial + (target - initial) * min(1.0, (sample - start) / length)
        new = 1.0 if sp else low
        if new == target:
            continue
        start, initial, target = sample, current, new
        length = max(1, round(RATE * (VAD_ATTACK if new > current else VAD_RELEASE)))
        # Execute at this frame's start, never a frame late due to decimal rounding.
        timestamp = max(0.0, sample / RATE - 1e-7)
        commands = (("start_sample", start), ("nb_samples", length),
                    ("silence", f"{initial:.12g}"), ("unity", f"{target:.12g}"))
        lines.append(f"{timestamp:.9f} " + ", ".join(
            f"afade@pauses {key} {value}" for key, value in commands) + ";")
    return "\n".join(lines) + "\n"


def _db(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        return SILENCE_DB
    return SILENCE_DB if math.isinf(v) or math.isnan(v) else max(SILENCE_DB, v)


def _astats(stderr: str) -> dict[str, dict[str, str]]:
    """Overall section of each `astats@NAME` instance in an ffmpeg log: {name: {key: value}}."""
    out: dict[str, dict[str, str]] = {}
    overall: set[str] = set()
    for name, text in re.findall(r"^\[astats@(\w+) @ [^\]]+\] (.*)$", stderr, re.M):
        if text.strip() == "Overall":
            overall.add(name)
        elif name in overall and ":" in text:
            key, _sep, value = text.partition(":")
            out.setdefault(name, {})[key.strip()] = value.strip()
    return out


# --------------------------------------------------------------------------
# Analysis

BANDS = {
    "rumble": "lowpass=f=80:p=2,lowpass=f=80:p=2",
    "mud": "highpass=f=150:p=2,lowpass=f=350:p=2",
    "voice": "highpass=f=300:p=2,lowpass=f=3000:p=2",
    "presence": "highpass=f=3000:p=2,lowpass=f=8000:p=2",
    "sibilance": "highpass=f=5000:p=2,highpass=f=5000:p=2,lowpass=f=9000:p=2",
    "h50": "bandpass=f=50:width_type=q:w=20,bandpass=f=50:width_type=q:w=20",
    "h50n": "bandpass=f=38:width_type=q:w=8,bandpass=f=38:width_type=q:w=8",
    "h60": "bandpass=f=60:width_type=q:w=20,bandpass=f=60:width_type=q:w=20",
    "h60n": "bandpass=f=74:width_type=q:w=8,bandpass=f=74:width_type=q:w=8",
}


def analyze(path: str | Path, limit: float | None = None, echo_only: list[bool] | None = None) -> Analysis:
    """Measure the audio of `path` (only the first `limit` seconds, if given).
    `echo_only` marks frames that held nothing but the speakers' echo: never speech."""
    path = str(path)
    duration = probe_duration(path)
    head = ["-t", str(limit)] if limit else []
    if limit:
        duration = min(duration, limit)
    if not has_audio(path):
        return Analysis(duration=duration, has_audio=False)
    norm = f"aformat=sample_fmts=flt:channel_layouts=mono,aresample={RATE}"

    # 1. Per-frame level and spectral shape -> voice activity, noise floor, speech level.
    feats = _features(path, head)
    levels = [f[0] for f in feats]
    audible = [v for v in levels if v > SILENCE_DB]
    a = Analysis(duration=duration, has_audio=bool(audible), levels=levels)
    if not audible:
        return a
    a.speech = vad(feats)
    if echo_only:
        a.speech = [sp and not (i < len(echo_only) and echo_only[i]) for i, sp in enumerate(a.speech)]
    speech = [v for v, sp in zip(levels, a.speech) if sp and v > SILENCE_DB]
    other = sorted(v for v, sp in zip(levels, a.speech) if not sp and v > SILENCE_DB)
    a.active_ratio = len(speech) / len(audible)
    if other:
        a.noise_floor_db = other[int(len(other) * 0.2)]   # the steady room/fan noise
        a.pause_db = other[int(len(other) * 0.95)]        # breaths, clicks, keyboard
    else:
        a.noise_floor_db = sorted(audible)[int(len(audible) * 0.03)]
    if speech:
        a.speech_db = statistics.median(speech)
        # Over ~1 s chunks of speech, so syllables do not count as level changes.
        step = int(1 / WINDOW)
        means = [statistics.fmean(speech[i:i + step]) for i in range(0, len(speech) - step + 1, step)]
        a.speech_spread_db = statistics.pstdev(means) if len(means) > 1 else 0.0
    a.snr_db = a.speech_db - a.noise_floor_db

    # 2. Overall stats, loudness and band energies in a single pass.
    split = ";".join(f"[b{i}]{f},astats@{name}=measure_perchannel=none:measure_overall=RMS_level,anullsink"
                     for i, (name, f) in enumerate(BANDS.items()))
    outs = "".join(f"[b{i}]" for i in range(len(BANDS)))
    graph = (f"[0:a:0]{norm},asplit={len(BANDS) + 1}[main]{outs};{split};"
             f"[main]astats@all=measure_perchannel=none,loudnorm=print_format=json")
    err = _ffmpeg([*head, "-i", path, "-vn", "-filter_complex", graph, *_null()])
    stats = _astats(err)
    bands = {name: _db(v.get("RMS level dB", "-inf")) for name, v in stats.items() if name != "all"}
    overall = stats.get("all", {})
    if overall:
        a.peak_db = _db(overall.get("Peak level dB", "-inf"))
        count = float(overall.get("Peak count", 0) or 0)
        samples = float(overall.get("Number of samples", 0) or 0)
        if samples > 0 and a.peak_db > -0.5:
            a.clipped_ratio = count / samples
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if m:
        a.loudness = json.loads(m[0])

    voice = bands.get("voice", SILENCE_DB)
    a.bands = {k: round(v - voice, 1) for k, v in bands.items() if k in ("rumble", "mud", "presence", "sibilance")}
    for hz in (50, 60):
        line, near = bands.get(f"h{hz}", SILENCE_DB), bands.get(f"h{hz}n", SILENCE_DB)
        prominence = line - near
        if line > a.noise_floor_db - 20 and prominence > max(HUM_PROMINENCE, a.hum_prominence_db):
            a.hum_hz, a.hum_prominence_db = hz, round(prominence, 1)
    return a


# --------------------------------------------------------------------------
# Decision

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def plan(a: Analysis, target: str = "youtube") -> Plan:
    lufs, tp = TARGETS.get(target, TARGETS["youtube"])
    S = []

    if a.clipped_ratio > CLIP_RATIO:
        S.append(Stage("declip", True, _("{ratio:.3%} of samples clipped").format(ratio=a.clipped_ratio),
                       "adeclip=window=55:overlap=75"))
    else:
        S.append(Stage("declip", False, _("no clipping")))

    rumble = a.bands.get("rumble", -60)
    cutoff = 100 if rumble > RUMBLE_EXCESS else 80 if rumble > RUMBLE_EXCESS - 10 else 70
    S.append(Stage("highpass", True, _("{hz} Hz cut, low end {db:+.0f} dB").format(hz=cutoff, db=rumble),
                   f"highpass=f={cutoff}:p=2,highpass=f={cutoff}:p=2"))

    if a.hum_hz:
        notches = ",".join(f"bandreject=f={a.hum_hz * k}:width_type=q:w={30 if k == 1 else 20}"
                           for k in (1, 2, 3, 4))
        S.append(Stage("dehum", True, _("{hz} Hz hum, {db:.0f} dB above its surroundings").format(
            hz=a.hum_hz, db=a.hum_prominence_db), notches))
    else:
        S.append(Stage("dehum", False, _("no mains hum")))

    # Preamp: denoising and the stages after it work best on a voice at a normal
    # level (measured: on a -42 LUFS voice RNNoise removes 5 dB of noise, 14 dB
    # after raising it). Later thresholds are shifted by the same gain.
    g = 0.0
    if a.speech_db < PREAMP_BELOW:
        g = _clamp(PREAMP_TARGET - a.speech_db, 0, 24)
        if a.peak_db > SILENCE_DB:
            # Processing is floating point and the compressor and limiter handle
            # the peaks, so occasional bumps must not keep a quiet voice quiet.
            g = min(g, PREAMP_PEAK_MAX - a.peak_db)
    if g >= 3:
        S.append(Stage("preamp", True, _("quiet voice ({db:.0f} dBFS): +{g:.0f} dB").format(db=a.speech_db, g=g),
                       f"volume={g:.1f}dB"))
    else:
        g = 0.0
        S.append(Stage("preamp", False, _("voice level fine")))
    speech, noise = a.speech_db + g, a.noise_floor_db + g

    # Judge the noise where it will end up: normalisation raises it with the voice.
    projected = a.noise_floor_db + lufs - float(a.loudness.get("input_i", lufs) or lufs)
    needed = projected - NOISE_TARGET
    reduction = 0.0
    model = rnnoise_model()
    if needed > NOISE_MIN_REDUCTION and model:
        # RNNoise keeps speech within ~1 dB while removing 5-18 dB of noise; the
        # wet/dry mix scales it down when less is needed.
        mix = _clamp(needed / RNNOISE_REDUCTION, 0.4, 1.0)
        reduction = RNNOISE_REDUCTION * mix
        S.append(Stage("denoise", True, _("noise would sit at {db:.0f} dBFS: neural denoise {mix:.0%}").format(
            db=projected, mix=mix), f"arnndn=m={_quote(model)}:mix={mix:.2f}"))
    elif needed > NOISE_MIN_REDUCTION:
        nr = round(_clamp(needed, NOISE_MIN_REDUCTION, 12))
        reduction = min(nr, 8)   # afftdn delivers far less than nr on real noise
        S.append(Stage("denoise", True, _("noise would sit at {db:.0f} dBFS: spectral denoise").format(
            db=projected), f"afftdn=nr={nr}:nf={round(_clamp(noise, -80, -20))}:tn=1"))
    else:
        S.append(Stage("denoise", False, _("quiet background ({db:.0f} dBFS after leveling)").format(db=projected)))

    # Pauses: what is not speech (breaths, keyboard, clicks, fan between words) is
    # lowered, driven by voice activity, before the leveler and compressor could
    # raise it. Depth: whatever brings the loudest of it under PAUSE_TARGET.
    pause_after = a.pause_db + lufs - float(a.loudness.get("input_i", lufs) or lufs) - reduction
    if a.speech and 0.02 < a.active_ratio < 0.99 and pause_after - PAUSE_TARGET > NOISE_MIN_REDUCTION:
        depth = round(_clamp(pause_after - PAUSE_TARGET, 6, PAUSE_MAX_DEPTH))
        S.append(Stage("pauses", True, _("breaths and noise between words lowered by {db} dB").format(db=depth),
                       "asendcmd=f={pause_commands},afade@pauses=t=in:ns=1:silence=1:unity=1",
                       param=depth))
    else:
        S.append(Stage("pauses", False, _("pauses already quiet")))

    if a.speech_spread_db > LEVELER_SPREAD:
        S.append(Stage("leveler", True, _("speech level varies by {db:.1f} dB").format(db=a.speech_spread_db),
                       # t: frames below the speech level are not raised (pauses stay down).
                       f"dynaudnorm=f=400:g=15:p=0.7:m=8:r=0.5:s=12"
                       f":t={10 ** ((speech - 18) / 20):.5f}"))
    else:
        S.append(Stage("leveler", False, _("steady speech level")))

    mud = a.bands.get("mud", -60)
    if mud > MUD_EXCESS:
        gain = -_clamp(round(mud), 2, 5)
        S.append(Stage("mud", True, _("boxy low-mids ({db:+.0f} dB)").format(db=mud),
                       f"equalizer=f=250:width_type=q:w=1.2:g={gain}"))
    else:
        S.append(Stage("mud", False, _("balanced low-mids")))

    presence = a.bands.get("presence", 0)
    if presence < MUFFLED_DEFICIT:
        gain = _clamp(round((MUFFLED_DEFICIT - presence) / 2 + 2), 2, 5)
        S.append(Stage("presence", True, _("muffled voice ({db:+.0f} dB highs)").format(db=presence),
                       f"equalizer=f=3500:width_type=q:w=0.9:g={gain}"))
    else:
        S.append(Stage("presence", False, _("clear voice")))

    sib = a.bands.get("sibilance", -60)
    if sib > SIBILANCE_EXCESS:
        S.append(Stage("deesser", True, _("strong sibilance ({db:+.0f} dB)").format(db=sib),
                       "deesser=i=0.6:m=0.5:f=0.5:s=o"))
    else:
        S.append(Stage("deesser", False, _("no harsh sibilance")))

    lra = float(a.loudness.get("input_lra", 0) or 0)
    if a.active_ratio > 0.05:
        ratio = 4 if lra > 12 else 3 if lra > 7 else 2
        threshold = _clamp(speech + 6, -50, -6)
        S.append(Stage("compressor", True, _("loudness range {lra:.0f} LU: {ratio}:1").format(lra=lra, ratio=ratio),
                       f"acompressor=threshold={threshold}dB:ratio={ratio}:attack=5:release=120:knee=4:detection=rms"))
    else:
        S.append(Stage("compressor", False, _("no speech detected")))

    S.append(Stage("loudness", True, _("{lufs:.0f} LUFS, {tp:.0f} dBTP").format(lufs=lufs, tp=tp)))
    S.append(Stage("limiter", True, _("true-peak safety"),
                   f"alimiter=limit={10 ** ((tp - 0.5) / 20):.3f}:attack=2:release=50:level=disabled"))
    return Plan(S, lufs, tp)


# --------------------------------------------------------------------------
# Rendering

@dataclass
class Report:
    input: str
    output: str
    analysis: Analysis
    stages: list[Stage]
    result: dict

    def to_json(self) -> str:
        data = asdict(self)
        data["analysis"].pop("levels", None)
        return json.dumps(data, indent=2, default=str)


def _loudnorm_pass(inputs: list[str], chain: str, lufs: float, tp: float) -> dict:
    """Loudness of `inputs` (input options + path) after `chain`."""
    graph = ",".join(x for x in (chain, f"loudnorm=I={lufs}:TP={tp}:LRA=11:print_format=json") if x)
    # The first audio track: the voice (system sound, if any, is a separate track).
    err = _ffmpeg([*inputs[:-1], "-i", inputs[-1], "-map", "0:a:0", "-af", graph, *_null()])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        raise AudioError("loudness measurement failed")
    return json.loads(m[0])


def _trial_levels(src: str, head: list[str], chain: str) -> list[float]:
    """Window levels of `src` after `chain`, aligned with Analysis.levels."""
    win = int(RATE * WINDOW)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *head, *_mono(src), "-af",
         f"aformat=sample_fmts=flt:channel_layouts=mono,aresample={RATE},{chain},"
         f"asetnsamples=n={win}:p=0,astats=metadata=1:reset=1:measure_perchannel=none:"
         f"measure_overall=RMS_level,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         *_null()], capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioError("denoise trial failed: " + r.stderr.strip().splitlines()[-1])
    return _window_levels(r.stdout)


def _voice_chain(stages: list[Stage]) -> str:
    """Keep every stage on the analysis timeline, including the pause envelope."""
    filters = [f"aresample={RATE}"]
    for stage in stages:
        if not stage.enabled or not stage.filter or stage.name == "limiter":
            continue
        delay = FILTER_DELAYS.get(stage.filter.split("=", 1)[0], 0)
        if delay:
            # Feed the delayed tail through the filter before dropping its startup.
            filters.append(f"apad=pad_len={delay}")
        if stage.name == "pauses":
            filters.append(f"asetnsamples=n={FRAME}:p=0")
        filters.append(stage.filter)
        if delay:
            filters.extend((f"aresample={RATE}", _realign(delay)))
    return ",".join(filters)


def speech_loss(a: Analysis, src: str, head: list[str], p: Plan) -> float:
    """Loss attributable to denoising alone, on aligned speech windows."""
    index = next((i for i, s in enumerate(p.stages) if s.name == "denoise"), None)
    if index is None:
        return 0.0
    before = _trial_levels(src, head, _voice_chain(p.stages[:index]))
    after = _trial_levels(src, head, _voice_chain(p.stages[:index + 1]))
    pairs = [(b, c) for b, c, sp in zip(before, after, a.speech)
             if sp and b > SILENCE_DB]
    if not pairs:
        raise AudioError("denoise trial produced no usable speech windows")
    return statistics.median(b - c for b, c in pairs)


def guard_denoise(a: Analysis, src: str, head: list[str], p: Plan) -> float:
    """Check the selected denoiser, including reduced mixes and the fallback."""
    stage = next((s for s in p.stages if s.name == "denoise" and s.enabled), None)
    if stage is None:
        return 0.0
    loss = speech_loss(a, src, head, p)
    initial_loss = loss
    if loss <= SPEECH_LOSS_MAX:
        stage.reason += " · " + _("voice kept ({db:.1f} dB)").format(db=-loss)
        return loss
    if stage.filter.startswith("arnndn"):
        if loss <= 2 * SPEECH_LOSS_MAX:
            mix = re.search(r"mix=([\d.]+)", stage.filter)
            new = float(mix[1]) * SPEECH_LOSS_MAX / loss if mix else 0.5
            stage.filter = re.sub(r"mix=[\d.]+", f"mix={new:.2f}", stage.filter)
            loss = speech_loss(a, src, head, p)
            if loss <= SPEECH_LOSS_MAX:
                stage.reason = _("neural denoise backed off to {mix:.0%}; voice loss {db:.1f} dB").format(
                    mix=new, db=loss)
                return initial_loss
        gain = next((float(s.filter[7:-2]) for s in p.stages
                     if s.name == "preamp" and s.enabled), 0.0)
        nf = round(_clamp(a.noise_floor_db + gain, -80, -20))
        stage.filter = f"afftdn=nr=10:nf={nf}:tn=1"
        loss = speech_loss(a, src, head, p)
        stage.reason = _("spectral denoise; verified voice loss {db:.1f} dB").format(db=loss)
    if loss > SPEECH_LOSS_MAX:
        stage.enabled = False
        stage.reason = _("denoise bypassed: taking {db:.1f} dB of voice").format(db=loss)
    return initial_loss


def _realign(samples: int) -> str:
    """Drops the first `samples` of a stream: undoes the delay a filter added."""
    return f"atrim=start_sample={samples},asetpts=PTS-STARTPTS" if samples else ""


def _container_delay(dst: str | Path) -> str:
    """Pre-trims the encoder's priming where the container cannot record it."""
    return _realign(AAC_DELAY) if str(dst).lower().endswith((".mkv", ".mka", ".webm")) else ""


def _append(chain: str, filt: str) -> str:
    """Adds `filt` at the end of a filter chain or of a graph ending in [out]."""
    if not filt:
        return chain
    return chain[:-5] + f",{filt}[out]" if chain.endswith("[out]") else f"{chain},{filt}"


def normalization_gain(p: Plan, measured: dict) -> tuple[float, bool]:
    """Constant gain and whether the final limiter will need to catch peaks."""
    integrated, peak = float(measured["input_i"]), float(measured["input_tp"])
    if not math.isfinite(integrated) or not math.isfinite(peak):
        raise AudioError("cannot normalize non-finite loudness measurements")
    wanted = p.target_lufs - integrated
    headroom = p.target_tp - 0.5 - peak
    return wanted, headroom < wanted


def _limit_chain(limiter: str) -> str:
    """Flush and undo the limiter's exact lookahead at its working sample rate."""
    oversampling = 4
    delay = LIMITER_DELAY * oversampling - 1
    return (f"aresample={RATE * oversampling},apad=pad_len={delay},{limiter},"
            f"{_realign(delay)},aresample={RATE}")


def render_chain(p: Plan, measured: dict, system_track: bool = False, channels: tuple[int, int] = (2, 2),
                 voice_input: str = "0:a:0") -> str:
    """Final filter: planned stages, then linear loudness normalisation, then the limiter.

    With a system sound track (second audio track) this is a filtergraph: the voice
    is processed, the system sound is added exactly as it was heard, and only the
    true-peak limiter acts on the sum (it is transparent unless the peaks clip).
    """
    # loudnorm linear=true silently falls back to dynamic mode if LRA or
    # peaks exceed its limits. Use an explicit constant gain instead, controlled
    # by the final true-peak limiter only on peaks that exceed its ceiling.
    gain, _ = normalization_gain(p, measured)
    loud = f"volume={gain:.6f}dB"
    stages = [s for s in p.stages if s.enabled and s.filter]
    before = _voice_chain(stages)
    limiter = next((s.filter for s in stages if s.name == "limiter"), "")
    # The limiter runs 4x oversampled so that it also catches inter-sample (true) peaks.
    # Its lookahead (the attack) and the denoisers delay the sound: both are trimmed,
    # so the voice stays in sync with the picture and with the system sound.
    oversampled = _limit_chain(limiter) if limiter else f"aresample={RATE}"
    if not system_track:
        return ",".join(x for x in (before, loud, oversampled) if x)
    voice = ",".join(x for x in (before, loud) if x)
    return (f"[{voice_input}]{voice},{_to_stereo(channels[0])}[voice];"
            f"[0:a:1]aresample={RATE},{_to_stereo(channels[1])}[system];"
            f"[voice][system]amix=inputs=2:normalize=0:duration=longest,{oversampled}[out]")


def mixdown(src: str | Path, dst: str | Path, progress: Callable[[str, float], None] | None = None):
    """Voice and system sound tracks into one, untouched (players play one track only).
    The limiter only acts if the sum would clip."""
    if not available():
        raise AudioError(_("ffmpeg is required for audio processing"))
    ch = track_channels(src) + [2, 2]
    limiter = _limit_chain("alimiter=limit=0.977:attack=2:release=50:level=disabled")
    graph = (f"[0:a:0]aresample={RATE},{_to_stereo(ch[0])}[a];[0:a:1]aresample={RATE},{_to_stereo(ch[1])}[b];"
             f"[a][b]amix=inputs=2:normalize=0:duration=longest,{limiter}[out]")
    graph = _append(graph, _container_delay(dst))
    _ffmpeg(["-i", str(src), "-map", "0:v?", "-filter_complex", graph, "-map", "[out]", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)],
            progress=(lambda f: progress("render", f)) if progress else None, duration=probe_duration(src))


def process(src: str | Path, dst: str | Path, target: str = "youtube",
            progress: Callable[[str, float], None] | None = None, limit: float | None = None) -> Report:
    """Analyse `src`, then write `dst` with processed audio and the video copied."""
    if not available():
        raise AudioError(_("ffmpeg is required for audio processing"))
    src, dst = str(src), str(dst)

    def step(name, value):
        if progress:
            progress(name, value)

    channels = track_channels(src)
    system = len(channels) > 1
    tmp = tempfile.TemporaryDirectory(prefix="rec0-audio-")
    # With system sound on its own track, its echo is removed from the microphone
    # first (speakers instead of headphones): the clean voice feeds every pass.
    voice, echo_only = src, None
    if system and echo.available():
        step("echo", 0.0)
        clean = str(Path(tmp.name) / "voice.wav")
        ratios = echo.cancel(src, clean, FRAME)
        if ratios is not None:
            voice = clean
            # What the speakers play is often speech too: where the microphone held only
            # their echo, voice activity detection would take it for the speaker, and the
            # leveler would raise it. Those frames are pauses.
            echo_only = [r < ECHO_ONLY_DB for r in ratios]
    step("analyze", 0.0)
    a = analyze(voice, limit, echo_only)
    head = ["-t", str(limit)] if limit else []
    if not a.has_audio or a.active_ratio == 0:
        raise AudioError(_("the recording has no usable audio"))
    p = plan(a, target)
    pauses = next((s for s in p.stages if s.name == "pauses" and s.enabled), None)
    if pauses:
        cmds = Path(tmp.name) / "pauses.cmd"
        cmds.write_text(pause_commands(a, pauses.param))
        pauses.filter = pauses.filter.replace("{pause_commands}", _quote(str(cmds)))
    step("verify", 0.2)
    guard_denoise(a, voice, head, p)
    step("measure", 0.3)
    voice_channels = track_channels(voice)[0]
    measurement_chain = _voice_chain(p.stages)
    if system:
        measurement_chain += "," + _to_stereo(voice_channels)
    measured = _loudnorm_pass(head + [voice], measurement_chain, p.target_lufs, p.target_tp)
    step("render", 0.4)
    second = [*head, "-i", voice] if voice != src else []
    chain = render_chain(p, measured, system, (voice_channels, channels[1]) if system else (2, 2),
                         "1:a:0" if second else "0:a:0")
    chain = _append(chain, _container_delay(dst))
    audio_args = (["-filter_complex", chain, "-map", "[out]"] if system
                  else ["-map", "0:a:0", "-af", chain])
    _ffmpeg([*head, "-i", src, *second, "-map", "0:v?", *audio_args, "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-ar", str(RATE), "-movflags", "+faststart", dst],
            progress=lambda f: step("render", 0.4 + 0.5 * f), duration=a.duration)
    step("verify", 0.9)
    after = _loudnorm_pass([dst], "", p.target_lufs, p.target_tp)
    step("done", 1.0)
    return Report(src, dst, a, p.stages, {
        "integrated_lufs": float(after["input_i"]),
        "true_peak_dbtp": float(after["input_tp"]),
        "loudness_range_lu": float(after["input_lra"]),
        "system_sound": system,
        "normalization_gain_db": normalization_gain(p, measured)[0],
        "normalization_requires_limiter": normalization_gain(p, measured)[1],
    })
