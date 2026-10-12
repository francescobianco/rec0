"""The room: blind reverberation time and late reverberation suppression (rec0/dereverb.py).

Dry ALSA voice samples are put in a synthetic room (an exponentially decaying noise
response of a known RT60) so the estimate and the suppression can be checked
against the truth."""
import subprocess
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
from rec0 import audio, dereverb  # noqa: E402

VOICES = sorted(Path("/usr/share/sounds/alsa").glob("*_*.wav"))
needs_ffmpeg = pytest.mark.skipif(not audio.available() or not VOICES, reason="needs ffmpeg and ALSA voice samples")
R = dereverb.RATE


def read(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ar", str(R), "-ac", "1", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(float)


def write(path, x):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(R), "-ac", "1", "-i", "-",
                    "-c:a", "pcm_f32le", str(path)], input=x.astype(np.float32).tobytes(), check=True)


def room(rt60, length=1.0, drr_db=3.0, seed=1):
    """Direct sound, then 5 ms later a noise tail falling 60 dB in rt60 seconds."""
    rng = np.random.default_rng(seed)
    n = int(length * R)
    t = np.arange(n) / R
    tail = rng.standard_normal(n) * np.exp(-3 * np.log(10) * t / rt60)
    tail[:int(0.005 * R)] = 0
    tail *= 10 ** (-drr_db / 20) / np.sqrt(np.sum(tail ** 2))
    h = np.zeros(n)
    h[0] = 1.0
    return h + tail


@pytest.fixture(scope="module")
def dry():
    x = np.concatenate([np.concatenate([read(v), np.zeros(int(0.4 * R))]) for v in VOICES] * 2)
    return x / np.max(np.abs(x)) * 0.3


def frame_energy(x):
    X, _ = dereverb._stft(x)
    return 10 * np.log10((np.abs(X) ** 2).sum(axis=1) + 1e-12)


@needs_ffmpeg
def test_blind_reverberation_time_finds_the_room(dry):
    assert dereverb.reverberation_time(dry) < audio.REVERB_MIN          # dry speech: no room
    for rt60 in (0.2, 0.3, 0.5):
        wet = np.convolve(dry, room(rt60))[:len(dry)]
        # Long rooms read short: the first part of a decay is the word's own end.
        assert 0.7 * rt60 <= dereverb.reverberation_time(wet) <= 1.2 * rt60


@needs_ffmpeg
def test_suppression_lowers_the_tails_and_keeps_the_voice(dry, tmp_path):
    wet = np.convolve(dry, room(0.3))[:len(dry)]
    src, dst = tmp_path / "wet.wav", tmp_path / "dry.wav"
    write(src, wet)
    dereverb.suppress(src, dst, 0.3)
    out = read(dst)
    assert len(out) == len(wet)
    d, w, o = frame_energy(dry), frame_energy(wet), frame_energy(out)
    tails = (d < d.max() - 70) & (w > w.max() - 50)   # the room alone, after the words
    speech = d > d.max() - 25
    assert np.median(o[tails] - w[tails]) < -6
    assert abs(np.median(o[speech] - w[speech])) < 1
    # No delay: the suppressed voice lines up sample for sample with the input.
    n = 1 << 20
    c = np.fft.irfft(np.fft.rfft(out[:n]) * np.conj(np.fft.rfft(wet[:n])))
    assert int(np.argmax(np.abs(c))) == 0
    assert dereverb.reverberation_time(out) < dereverb.reverberation_time(wet) - 0.05


@needs_ffmpeg
def test_analysis_and_plan_carry_the_room(dry, tmp_path):
    wet = np.convolve(dry, room(0.35))[:len(dry)]
    src = tmp_path / "wet.wav"
    write(src, wet)
    a = audio.analyze(src)
    assert a.reverb_s == pytest.approx(0.35, abs=0.08)
    assert next(s for s in audio.plan(a).stages if s.name == "dereverb").enabled
