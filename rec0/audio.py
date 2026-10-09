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
    expander    push the pauses between phrases down        if noise is still audible after denoise
    leveler     even out distance-from-mic changes          if speech level wanders
    mud         cut boxiness around 250 Hz                  if low-mids are excessive
    presence    lift intelligibility around 3.5 kHz         if the voice sounds muffled
    deesser     tame harsh "s" sounds                       if sibilance is strong
    compressor  steady speech dynamics                      ratio from the dynamic range
    loudness    two-pass EBU R128 to the platform target    always
    limiter     true-peak safety                            always
"""

from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .i18n import _, pkgdata

RATE = 48000
WINDOW = 0.05                 # analysis window, seconds

# Platform loudness targets (integrated LUFS, true peak dBTP).
TARGETS = {
    "youtube": (-14.0, -1.0),
    "podcast": (-16.0, -1.0),
    "broadcast": (-23.0, -1.0),
}

# Decision thresholds (dB). Tuned on speech recordings; kept together so they
# can be adjusted in one place.
SILENCE_DB = -90.0            # digital silence, ignored by the statistics
ACTIVE_ABOVE_NOISE = 10.0     # a window is speech if this much above the noise floor
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
    levels: list = field(default_factory=list, repr=False)  # RMS dB of each analysis window

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


def has_audio(path: str | Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _window_levels(stdout: str) -> list[float]:
    return [_db(v) for v in re.findall(r"RMS_level=(\S+)", stdout)]


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


def analyze(path: str | Path, limit: float | None = None) -> Analysis:
    """Measure the audio of `path` (only the first `limit` seconds, if given)."""
    path = str(path)
    duration = probe_duration(path)
    head = ["-t", str(limit)] if limit else []
    if limit:
        duration = min(duration, limit)
    if not has_audio(path):
        return Analysis(duration=duration, has_audio=False)
    norm = f"aformat=sample_fmts=flt:channel_layouts=mono,aresample={RATE}"

    # 1. Short-window levels: noise floor, speech level and how much it wanders.
    win = int(RATE * WINDOW)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *head, *_mono(path), "-af",
         f"{norm},asetnsamples=n={win}:p=0,astats=metadata=1:reset=1:measure_perchannel=none:"
         f"measure_overall=RMS_level,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         *_null()], capture_output=True, text=True)
    all_levels = _window_levels(r.stdout)
    levels = [v for v in all_levels if v > SILENCE_DB]
    a = Analysis(duration=duration, has_audio=bool(levels), levels=all_levels)
    if not levels:
        return a
    ordered = sorted(levels)
    # Continuous speech leaves few pauses: higher percentiles land in word tails
    # and reverb, not in the room noise (checked on real OBS recordings).
    a.noise_floor_db = ordered[int(len(ordered) * 0.03)]
    active = [v for v in levels if v > a.noise_floor_db + ACTIVE_ABOVE_NOISE]
    a.active_ratio = len(active) / len(levels)
    if active:
        a.speech_db = statistics.median(active)
        # Smooth over ~1 s so syllables do not count as level changes.
        step = int(1 / WINDOW)
        means = [statistics.fmean(active[i:i + step]) for i in range(0, len(active) - step + 1, step)]
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

    # What denoising cannot remove is left to a gentle downward expander in the
    # pauses: threshold between noise and speech, depth only what is still needed.
    residual = needed - reduction
    if residual > NOISE_MIN_REDUCTION and a.snr_db > 8:
        depth = _clamp(residual + 3, 6, 18)
        thr = noise - reduction + min(a.snr_db * 0.4, 12)
        S.append(Stage("expander", True, _("pauses lowered by up to {db:.0f} dB").format(db=depth),
                       f"agate=threshold={10 ** (thr / 20):.6f}:range={10 ** (-depth / 20):.4f}:ratio=3"
                       f":attack=8:release=250:knee=6:detection=rms"))
    else:
        S.append(Stage("expander", False, _("pauses already quiet")))

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
    err = _ffmpeg([*inputs[:-1], "-i", inputs[-1], "-vn", "-af", graph, *_null()])
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
    return _window_levels(r.stdout)


def speech_loss(a: Analysis, src: str, head: list[str], p: Plan) -> float:
    """dB of level the speech windows lose through the stages up to the denoiser
    (the preamp gain is accounted for)."""
    upto = []
    for s in p.stages:
        if s.enabled and s.filter:
            upto.append(s.filter)
        if s.name == "denoise":
            break
    after = _trial_levels(src, head, ",".join(upto))
    gain = next((float(s.filter[7:-2]) for s in p.stages if s.name == "preamp" and s.enabled), 0.0)
    threshold = a.noise_floor_db + ACTIVE_ABOVE_NOISE
    pairs = [(b, c) for b, c in zip(a.levels, after) if b > threshold]
    if not pairs:
        return 0.0
    return statistics.median(b + gain - c for b, c in pairs)


def guard_denoise(a: Analysis, src: str, head: list[str], p: Plan) -> float:
    """Feedback: back the denoiser off if it is eating the voice. Returns the measured loss."""
    stage = next((s for s in p.stages if s.name == "denoise" and s.enabled), None)
    if stage is None or not stage.filter.startswith("arnndn"):
        return 0.0
    loss = speech_loss(a, src, head, p)
    if loss <= SPEECH_LOSS_MAX:
        stage.reason += " · " + _("voice kept ({db:.1f} dB)").format(db=-loss)
        return loss
    if loss <= 2 * SPEECH_LOSS_MAX:
        mix = re.search(r"mix=([\d.]+)", stage.filter)
        new = max(0.3, float(mix[1]) * SPEECH_LOSS_MAX / loss) if mix else 0.5
        stage.filter = re.sub(r"mix=[\d.]+", f"mix={new:.2f}", stage.filter)
        stage.reason += " · " + _("backed off to {mix:.0%}, it was taking {db:.1f} dB of voice").format(
            mix=new, db=loss)
    else:
        nf = round(_clamp(a.noise_floor_db, -80, -20))
        stage.filter = f"afftdn=nr=10:nf={nf}:tn=1"
        stage.reason = _("weak voice under noise: gentle spectral denoise (neural took {db:.1f} dB of voice)").format(
            db=loss)
    return loss


def render_chain(p: Plan, measured: dict) -> str:
    """Final filter: planned stages, then linear loudness normalisation, then the limiter."""
    loud = (f"loudnorm=I={p.target_lufs}:TP={p.target_tp}:LRA=11"
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true")
    stages = [s for s in p.stages if s.enabled and s.filter]
    before = ",".join(s.filter for s in stages if s.name != "limiter")
    limiter = next((s.filter for s in stages if s.name == "limiter"), "")
    # The limiter runs 4x oversampled so that it also catches inter-sample (true) peaks.
    oversampled = f"aresample={RATE * 4},{limiter},aresample={RATE}" if limiter else f"aresample={RATE}"
    return ",".join(x for x in (before, loud, oversampled) if x)


def process(src: str | Path, dst: str | Path, target: str = "youtube",
            progress: Callable[[str, float], None] | None = None, limit: float | None = None) -> Report:
    """Analyse `src`, then write `dst` with processed audio and the video copied."""
    if not available():
        raise AudioError(_("ffmpeg is required for audio processing"))
    src, dst = str(src), str(dst)

    def step(name, value):
        if progress:
            progress(name, value)

    step("analyze", 0.0)
    a = analyze(src, limit)
    head = ["-t", str(limit)] if limit else []
    if not a.has_audio or a.active_ratio == 0:
        raise AudioError(_("the recording has no usable audio"))
    p = plan(a, target)
    step("verify", 0.2)
    guard_denoise(a, src, head, p)
    step("measure", 0.3)
    measured = _loudnorm_pass(head + [src], ",".join(s.filter for s in p.stages
                                            if s.enabled and s.filter and s.name != "limiter"),
                              p.target_lufs, p.target_tp)
    step("render", 0.4)
    chain = render_chain(p, measured)
    _ffmpeg([*head, "-i", src, "-map", "0:v?", "-map", "0:a:0", "-c:v", "copy", "-af", chain,
             "-c:a", "aac", "-b:a", "192k", "-ar", str(RATE), "-movflags", "+faststart", dst],
            progress=lambda f: step("render", 0.4 + 0.5 * f), duration=a.duration)
    step("verify", 0.9)
    after = _loudnorm_pass([dst], "", p.target_lufs, p.target_tp)
    step("done", 1.0)
    return Report(src, dst, a, p.stages, {
        "integrated_lufs": float(after["input_i"]),
        "true_peak_dbtp": float(after["input_tp"]),
        "loudness_range_lu": float(after["input_lra"]),
    })
