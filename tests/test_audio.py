import shutil
import subprocess
from pathlib import Path

import pytest

from rec0 import audio
from rec0.audio import Analysis, plan

VOICES = sorted(Path("/usr/share/sounds/alsa").glob("*_*.wav"))
needs_ffmpeg = pytest.mark.skipif(not audio.available() or not VOICES, reason="needs ffmpeg and ALSA voice samples")


def analysis(**kw):
    base = dict(duration=60, has_audio=True, peak_db=-6, speech_db=-22, noise_floor_db=-75, snr_db=53,
                speech_spread_db=1.5, active_ratio=0.8,
                bands={"rumble": -30, "mud": -3, "presence": -15, "sibilance": -22},
                loudness={"input_i": "-18", "input_lra": "5", "input_tp": "-3"})
    base.update(kw)
    return Analysis(**base)


def enabled(p):
    return {s.name for s in p.stages if s.enabled}


def test_clean_recording_gets_only_the_essentials():
    assert enabled(plan(analysis())) == {"highpass", "compressor", "loudness", "limiter"}


def test_noisy_cheap_microphone_turns_on_the_cleanup_circuits():
    p = plan(analysis(speech_db=-38, noise_floor_db=-56, snr_db=18, speech_spread_db=6, peak_db=-18,
                      bands={"rumble": 5, "mud": 4, "presence": -30, "sibilance": -25},
                      loudness={"input_i": "-36", "input_lra": "15", "input_tp": "-17"}))
    on = enabled(p)
    assert {"preamp", "denoise", "leveler", "mud", "presence", "compressor"} <= on
    assert "100 Hz" in next(s.reason for s in p.stages if s.name == "highpass")
    assert "4:1" in next(s.reason for s in p.stages if s.name == "compressor")


def test_preamp_bounds_peaks_to_float_headroom():
    # Voice at -40 wants +16 dB; peaks at -8 dBFS may only reach +6 dBFS (float, limited later).
    p = plan(analysis(speech_db=-40, peak_db=-8, noise_floor_db=-90))
    pre = next(s for s in p.stages if s.name == "preamp")
    assert pre.enabled and pre.filter == "volume=14.0dB"


def test_hum_and_clipping_are_repaired():
    on = enabled(plan(analysis(hum_hz=50, hum_prominence_db=20, clipped_ratio=1e-3)))
    assert {"dehum", "declip"} <= on


def test_targets():
    assert plan(analysis(), "podcast").target_lufs == -16
    assert plan(analysis(), "broadcast").target_lufs == -23


@pytest.fixture(scope="module")
def noisy_voice(tmp_path_factory):
    d = tmp_path_factory.mktemp("voice")
    gap = d / "gap.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                    "-t", "0.6", str(gap)], check=True)
    lst = d / "list.txt"
    lst.write_text("".join(f"file '{v}'\nfile '{gap}'\n" for v in VOICES * 2))
    clean, noisy = d / "clean.wav", d / "noisy.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-af",
                    "aresample=48000,aformat=sample_fmts=flt:channel_layouts=mono,volume=-14dB",
                    "-c:a", "pcm_f32le", str(clean)], check=True)
    # A cheap setup: quiet voice, fan noise and a bit of rumble.
    subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(clean), "-f", "lavfi", "-i",
                    "anoisesrc=color=pink:amplitude=0.008:sample_rate=48000:seed=3", "-filter_complex",
                    "[0][1]amix=inputs=2:duration=first:normalize=0", "-c:a", "pcm_f32le", str(noisy)], check=True)
    return noisy


@needs_ffmpeg
def test_process_cleans_and_normalizes(noisy_voice, tmp_path):
    out = tmp_path / "out.m4a"
    report = audio.process(noisy_voice, out)
    on = {s.name for s in report.stages if s.enabled}
    assert {"denoise", "preamp"} <= on
    assert abs(report.result["integrated_lufs"] + 14) < 1
    assert report.result["true_peak_dbtp"] <= -0.9
    before, after = report.analysis, audio.analyze(out)
    assert after.snr_db > before.snr_db + 15


@needs_ffmpeg
def test_finalize_keeps_the_original_and_restores_on_failure(tmp_path, noisy_voice):
    from rec0.postprocess import finalize, original_path
    from rec0.project import parse

    p = parse({"camera": {"device": "test"}, "output": {"directory": str(tmp_path)}})
    video = tmp_path / "rec.mp4"
    shutil.copy(noisy_voice, tmp_path / "in.wav")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=10",
                    "-i", str(tmp_path / "in.wav"), "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    str(video)], check=True)
    finalize(p, video)
    assert video.exists() and original_path(video).exists()

    # A file without speech cannot be optimized: the recording must stay where it was.
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=10",
                    "-f", "lavfi", "-i", "anullsrc=r=48000", "-t", "2", "-c:v", "libx264", "-c:a", "aac",
                    str(silent)], check=True)
    with pytest.raises(audio.AudioError):
        finalize(p, silent)
    assert silent.exists() and not original_path(silent).exists()
