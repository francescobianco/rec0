import shutil
import subprocess
from pathlib import Path

import pytest

from rec0 import audio, echo
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


def _read(path, track=0):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-map", f"0:a:{track}", "-ac", "1",
                          "-ar", "48000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(float)


def _two_tracks(path, voice, system):
    """A recording like rec0's: microphone on track 0, system sound on track 1."""
    import numpy as np
    d = Path(path).parent
    for name, x in (("t0.f32", voice), ("t1.f32", system)):
        (d / name).write_bytes(np.asarray(x, np.float32).tobytes())
    raw = ["-f", "f32le", "-ar", "48000", "-ac", "1", "-i"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *raw, str(d / "t0.f32"), *raw, str(d / "t1.f32"),
                    "-map", "0", "-map", "1", "-c:a", "pcm_f32le", str(path)], check=True)


def _lag_ms(a, b):
    import numpy as np
    n = min(len(a), len(b))
    nf = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(a[:n], nf) * np.conj(np.fft.rfft(b[:n], nf)), nf)
    c = np.concatenate([c[-2400:], c[:2401]])
    return (np.argmax(np.abs(c)) - 2400) / 48


@needs_ffmpeg
@pytest.mark.skipif(not echo.available(), reason="needs numpy")
def test_speaker_echo_is_removed_from_the_microphone(noisy_voice, tmp_path):
    import numpy as np
    voice = _read(noisy_voice.parent / "clean.wav")
    rng = np.random.default_rng(1)
    # "Music" from the speakers: noise shaped by a slow envelope.
    system = np.convolve(rng.standard_normal(len(voice)), np.ones(8) / 8, "same") * 0.1
    system *= 0.6 + 0.4 * np.sin(np.arange(len(voice)) / 48000 * 2 * np.pi * 0.7)
    # The room: 18 ms to the microphone, then two reflections.
    room = np.zeros(4000)
    room[[864, 1700, 3100]] = [0.5, -0.25, 0.12]
    bleed = np.convolve(system, room)[:len(voice)]
    src = tmp_path / "rec.mkv"
    _two_tracks(src, voice + bleed, system)

    assert echo.cancel(src, tmp_path / "voice.wav")
    out = _read(tmp_path / "voice.wav")[:len(voice)]
    residual = out - voice
    reduction = 10 * np.log10(np.mean(bleed ** 2) / np.mean(residual ** 2))
    assert reduction > 12, reduction
    assert np.corrcoef(out, voice)[0, 1] > 0.97


@needs_ffmpeg
def test_processed_voice_stays_in_sync_with_system_sound(noisy_voice, tmp_path):
    import numpy as np
    voice = _read(noisy_voice)
    t = np.arange(len(voice)) / 48000
    system = 0.02 * np.sin(2 * np.pi * 3000 * t)       # a quiet tone, unrelated to the voice
    src, out = tmp_path / "rec.mkv", tmp_path / "out.mkv"
    _two_tracks(src, voice, system)
    report = audio.process(src, out)
    assert "denoise" in {s.name for s in report.stages if s.enabled}    # the stage that adds 10 ms
    mixed = _read(out)
    # RNNoise's frame, the limiter's lookahead and AAC's priming (not recorded by
    # Matroska) are compensated. What is left is the high-pass filter's phase shift
    # at low frequencies, which shows as ~1.5 ms in a cross-correlation.
    assert abs(_lag_ms(mixed, voice)) <= 2


@needs_ffmpeg
@pytest.mark.parametrize("filt", ["arnndn=m={model}", "afftdn=nr=10:nf=-50:tn=1"])
def test_filter_delays_are_the_ones_compensated(noisy_voice, filt):
    import numpy as np
    model = audio._quote(audio.rnnoise_model())

    def run(f):
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(noisy_voice), "-af",
                              f"aformat=sample_fmts=flt:channel_layouts=mono,aresample=48000,{f}",
                              "-f", "f32le", "-ac", "1", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.float32).astype(float)

    name = filt.split("=")[0]
    lag = _lag_ms(run(filt.format(model=model)), run("anull"))
    assert round(lag * 48) == audio.FILTER_DELAYS[name]


@needs_ffmpeg
def test_normalization_preserves_dynamics_even_when_target_exceeds_headroom(tmp_path):
    import numpy as np

    t = np.arange(48000 * 12) / 48000
    signal = np.sin(2 * np.pi * 1000 * t) * np.where(t < 6, 0.01, 0.15)
    signal[48000:48010] = 0.8
    src = tmp_path / 'dynamic.wav'
    raw = tmp_path / 'dynamic.f32'
    raw.write_bytes(signal.astype(np.float32).tobytes())
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'f32le', '-ar', '48000', '-ac', '1',
                    '-i', str(raw), '-c:a', 'pcm_f32le', str(src)], check=True)
    p = audio.Plan([], -14, -1)
    measured = audio._loudnorm_pass([str(src)], '', p.target_lufs, p.target_tp)
    gain, limited = audio.normalization_gain(p, measured)
    assert limited
    out = tmp_path / 'normalized.wav'
    audio._ffmpeg(['-i', str(src), '-af', audio.render_chain(p, measured), '-c:a', 'pcm_f32le', str(out)])
    decoded = _read(out)
    # Every sample receives the same gain, including the quiet half and the transient.
    np.testing.assert_allclose(decoded, signal * 10 ** (gain / 20), atol=2e-7)
    assert gain == pytest.approx(-14 - float(measured["input_i"]))


@needs_ffmpeg
def test_denoise_guard_does_not_blame_upstream_attenuation(noisy_voice):
    a = audio.analyze(noisy_voice)
    p = audio.Plan([
        audio.Stage('highpass', True, '', 'volume=-12dB'),
        audio.Stage('denoise', True, '', 'afftdn=nr=0.01:nf=-80'),
    ], -14, -1)
    assert abs(audio.speech_loss(a, str(noisy_voice), [], p)) < 1


@pytest.mark.parametrize('initial,losses', [
    ('arnndn=m=test:mix=1.00', [20, 4]),
    ('arnndn=m=test:mix=1.00', [5, 4, 4]),
    ('afftdn=nr=10:nf=-50', [4]),
])
def test_denoise_guard_bypasses_when_even_fallback_damages_voice(monkeypatch, initial, losses):
    trials = iter(losses)
    monkeypatch.setattr(audio, 'speech_loss', lambda *args: next(trials))
    stage = audio.Stage('denoise', True, '', initial)
    p = audio.Plan([stage], -14, -1)
    audio.guard_denoise(analysis(), 'unused', [], p)
    assert not stage.enabled
    assert list(trials) == []


@needs_ffmpeg
def test_denoising_preserves_the_tail_and_duration(noisy_voice, tmp_path):
    p = audio.Plan([audio.Stage('denoise', True, '', 'afftdn=nr=0.01:nf=-80')], -14, -1)
    out = tmp_path / 'aligned.wav'
    audio._ffmpeg(['-i', str(noisy_voice), '-af', audio._voice_chain(p.stages),
                   '-c:a', 'pcm_f32le', str(out)])
    before, after = _read(noisy_voice), _read(out)
    # FFT filters buffer the end; padding before the filter must retain those samples.
    assert abs(len(before) - len(after)) <= 1
    assert abs(_lag_ms(after, before)) <= 1 / 48


@needs_ffmpeg
def test_system_track_keeps_level_timing_and_duration(tmp_path):
    import numpy as np

    t = np.arange(48000 * 3) / 48000
    system = 0.03 * np.sin(2 * np.pi * 997 * t) + 0.02 * np.sin(2 * np.pi * 2300 * t)
    src, out = tmp_path / 'tracks.mkv', tmp_path / 'mixed.wav'
    _two_tracks(src, np.zeros_like(system), system)
    p = audio.Plan([audio.Stage('limiter', True, '',
                    'alimiter=limit=0.84:attack=2:release=50:level=disabled')], -14, -1)
    graph = audio.render_chain(p, {'input_i': '-14', 'input_tp': '-6'}, True, (1, 1))
    audio._ffmpeg(['-i', str(src), '-filter_complex', graph, '-map', '[out]', '-c:a', 'pcm_f32le', str(out)])
    # Read one channel: ffmpeg's default stereo-to-mono downmix can add 3 dB.
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(out), '-af', 'pan=mono|c0=c0',
                          '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    mixed = np.frombuffer(raw, np.float32)
    assert len(mixed) == len(system)
    np.testing.assert_allclose(mixed[100:-100], system[100:-100], atol=1e-5)


@needs_ffmpeg
def test_failed_denoise_trial_is_not_reported_as_voice_preserved(tmp_path):
    with pytest.raises(audio.AudioError, match='denoise trial failed'):
        audio._trial_levels(str(tmp_path / 'missing.wav'), [], 'anull')


@needs_ffmpeg
def test_pause_transitions_are_sample_continuous(tmp_path):
    import numpy as np

    # Rapid reversals interrupt both opening and closing ramps. A DC signal
    # reveals gain discontinuities without confusing them with speech transients.
    mask = [True] * 10 + [False] * 20 + [True] + [False] * 3 + [True] * 20
    a = analysis(speech=mask)
    commands = tmp_path / 'pauses.cmd'
    commands.write_text(audio.pause_commands(a, 24))
    stage = audio.Stage('pauses', True, '',
                        f'asendcmd=f={audio._quote(str(commands))},'
                        'afade@pauses=t=in:ns=1:silence=1:unity=1')
    n = len(mask) * audio.FRAME
    out = tmp_path / 'envelope.wav'
    audio._ffmpeg(['-f', 'lavfi', '-i', 'aevalsrc=0.5:s=48000',
                   '-af', audio._voice_chain([stage]), '-t', str(n / audio.RATE),
                   '-c:a', 'pcm_f32le', str(out)])
    envelope = _read(out) / 0.5
    assert len(envelope) == n
    assert max(abs(np.diff(envelope))) < 1 / (audio.VAD_ATTACK * audio.RATE) + 1e-6
    assert envelope[25 * audio.FRAME] == pytest.approx(10 ** (-24 / 20), abs=1e-6)
    assert envelope[-1] == pytest.approx(1, abs=1e-6)
    # No step at any VAD boundary, and opening starts at the specified sample.
    onset = 30 * audio.FRAME
    assert envelope[onset] == pytest.approx(envelope[onset - 1], abs=1e-6)
    assert envelope[onset + 1] > envelope[onset]
