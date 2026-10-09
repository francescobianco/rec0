# Audio processing in rec0: technical dossier

> [!WARNING]
> **Status: not there yet. The expected quality has not been reached.**
>
> The goal is simple to state. The voice should come out clean, even and as
> loud as YouTube expects, and what the computer plays while recording (a
> video, a song) should sound in the final file **exactly as it was heard
> live**. Today rec0 does not meet that bar. On the latest real test
> (`dev-20261009-165346`, a YouTube video playing through speakers while
> talking over it), the author reports that the processed result is worse than
> the unprocessed original, with a voice that "gets better and worse by turns".
> The fixes described below measurably reduced the problems, but they have not
> been confirmed by listening, and several parameters are still hand-tuned on
> two recordings. Read this document as the record of an ongoing
> investigation, not as the description of a finished feature.

This dossier collects everything about how rec0 handles audio: the capture,
the echo canceller, the analysis, the adaptive "circuits", the rendering, how
it evolved, the bugs found along the way, the attempts that failed, and what
is still open. Measurements are reported with the method that produced them,
because more than once a wrong metric led to a wrong conclusion.

Contents:

1. [Quality expectations](#1-quality-expectations)
2. [Overview](#2-overview)
3. [Capture](#3-capture)
4. [Speaker echo canceller](#4-speaker-echo-canceller-rec0echopy)
5. [Analysis and voice activity detection](#5-analysis-and-voice-activity-detection)
6. [The circuits (plan)](#6-the-circuits-plan)
7. [Rendering](#7-rendering)
8. [Delay compensation](#8-delay-compensation)
9. [Evolution](#9-evolution)
10. [Bugs found](#10-bugs-found)
11. [Attempts and measurements](#11-attempts-and-measurements)
12. [Measurement methods (and their traps)](#12-measurement-methods-and-their-traps)
13. [Open problems](#13-open-problems)
14. [Proposed next steps](#14-proposed-next-steps)
15. [Reproducing the analyses](#15-reproducing-the-analyses)

---

## 1. Quality expectations

What "good" means for rec0, in the author's words and as requirements:

| # | Expectation | Status |
|---|---|---|
| E1 | A first-time creator with a cheap microphone in a noisy room gets clean, even voice at YouTube loudness (-14 LUFS, -1 dBTP) without touching anything | reached on voice-only recordings (synthetic and real); **not** when system sound plays through speakers |
| E2 | What plays on the computer while recording is in the video **as heard live**, "as if overlaid" | the system sound track is laid over untouched (verified to 0.03 dB); but see E3 and E5 |
| E3 | No echo or "room" effect on the system sound when speakers are used instead of headphones | much improved (from the room copy 18 dB **above** the clean sound to 21 dB **below** it), residue still audible according to the author |
| E4 | Stable processing: the voice must not change character from one moment to the next | **not reached**: reported on `dev-20261009-165346`; mitigated, not confirmed |
| E5 | The balance between voice and system sound resembles what was heard in the room | **not addressed**: the voice is normalised to -14 LUFS while system sound keeps its own level (open design question, [§13](#13-open-problems)) |
| E6 | The voice stays in sync with the picture and with the system sound | reached: from +27 ms to -0.15 ms on a real recording |
| E7 | Never lose a recording | reached: the original is renamed first and restored on any failure |

## 2. Overview

```mermaid
flowchart LR
  subgraph rec [while recording, GStreamer]
    mic[pulsesrc<br/>microphone] --> t1[AAC track 1]
    mon[pulsesrc<br/>@DEFAULT_MONITOR@] --> t2[AAC track 2]
  end
  t1 --> orig[(name.original.mp4<br/>separate tracks)]
  t2 --> orig
  subgraph post [after stop, ffmpeg + numpy]
    orig --> aec[echo canceller<br/>echo.py]
    aec -->|clean voice + echo-only mask| an[analysis + VAD]
    an --> plan[plan: which circuits]
    plan --> guard[denoise feedback check]
    guard --> meas[loudness pass 1]
    meas --> render[render: voice chain<br/>+ system sound untouched<br/>+ limiter on the sum]
  end
  render --> final[(name.mp4)]
```

Files:

| File | Role |
|---|---|
| `rec0/recorder.py` (`Recorder._audio`) | one GStreamer branch per audio source, each its own track |
| `rec0/postprocess.py` | `finalize()`: renames the recording to `.original`, processes or mixes, restores on failure |
| `rec0/echo.py` | speaker echo canceller (numpy) |
| `rec0/audio.py` | analysis, VAD, plan, denoise guard, rendering, delay compensation |
| `tests/test_audio.py` | plan unit tests, end-to-end processing, echo removal, sync, filter delays |

## 3. Capture

- **Two tracks, no mixer.** The microphone (`pulsesrc`, the chosen device) and
  the system sound (`pulsesrc device=@DEFAULT_MONITOR@`, the monitor of the
  default output) are recorded as separate AAC tracks, 48 kHz stereo,
  192 kbit/s, microphone first. There used to be a live `audiomixer`. It was
  removed for two reasons: it dropped and resynced late microphone buffers,
  causing thousands of 10 ms cuts (measured in GStreamer logs), and mixing
  before processing makes the processor treat what the computer plays as the
  speaker's voice.
- **Warm-up.** The countdown runs the real pipeline without writing anything.
  The start is frame-exact: an `identity` gate drops audio and video buffers
  before the scheduled start.
- **Closing.** On stop, one more second is recorded and the audio fades out
  (`volume name=outrovol*`) over the same timestamps as the CRT power-off.
- **Mono microphones** are duplicated to both channels at full level (the
  default upmix spread them at -3 dB per side).
- **The original keeps the separate tracks** (`name.original.mp4`), so
  everything below can be re-run on it (`rec0 process name.original.mp4`).

## 4. Speaker echo canceller (`rec0/echo.py`)

### The physical problem

```
system track (clean) ──► speakers ──► room ──► microphone
                                                ▲
                          speaker's voice ──────┘

microphone = voice + h * system      (h: speakers + room, unknown)
```

The system sound track is the exact reference of what the speakers played.
What has to be removed from the microphone is not that track as is, but the
track **as transformed by the speakers and the room**. That means 11-18 ms of
acoustic delay in the author's setup, the speakers' colour, the early
reflections and the reverberant tail. So `h` has to be estimated. The
estimate comes from the recording itself: the playing video is the test
signal, and `h` is re-estimated every 2 s, which follows changes of volume,
position and the drift between the two devices' clocks. No prior calibration
is needed, but none is possible either ([§14](#14-proposed-next-steps)).

### Stage 1: linear canceller (least squares per frequency bin)

- STFT: frame `N = 1024` (21 ms), hop `HOP = 256`, square-root Hann window.
- Model per bin `f`: `M[t,f] = sum_k W[f,k] · X[t-k,f] + V[t,f]`, with
  `TAPS = 16` frames of history (~85 ms: delay plus early reflections).
- Solved by **batch least squares** over blocks of `BLOCK = 2 s`
  (half-overlapping, Hann-tapered): `W = (R + λI)⁻¹ p`, with
  `R = Σ X Xᴴ`, `p = Σ X M*`, and regularisation `λ = 1e-3 · tr(R)/TAPS`.
- Why least squares and not an adaptive filter (NLMS)? The voice is
  uncorrelated with the reference, so over a block it only adds variance and
  no bias. No double-talk detector is needed, and the estimate does not
  diverge when the speaker talks over the video. Processing is offline, so
  batch estimation is possible.
- Why blocks and not one global filter? Clock drift: the microphone and the
  sound card run on different clocks (measured: +80.9 ppm, the delay sliding
  from 17.6 to 18.9 ms in 26 s). A fixed filter is wrong above a few hundred Hz
  after a few seconds.
- Blocks where the reference is silent are passed through unchanged.
- Long recordings are processed in 20 s chunks with 1 s of context and 50 ms
  crossfades, on memory-mapped raw files: memory stays at ~350 MB, and speed
  is ~15% of real time (6-7 s for 45 s of audio).

### Stage 2: residual echo suppression

What the linear filter cannot model remains: the reverberant tail beyond
85 ms and the speakers' non-linear distortion. Per bin:

- the echo estimate `|Y|²` is extended with an exponential tail falling 60 dB
  in `TAIL = 0.3 s`;
- gain `G = clip(1 - OVER · tail / |E|², FLOOR, 1)`, with `OVER = 1`,
  `FLOOR = 0.3` (-10 dB), fast to open and slower to close.

It is deliberately gentle. A stronger setting (`OVER = 2`, `FLOOR = 0.1`)
cost the voice 1.2 dB and lowered the synthetic score ([§11](#11-attempts-and-measurements)).

### Output: the echo-only mask

`cancel()` also returns, for every 1024-sample analysis frame, how many dB the
cleaned microphone stands above the echo removed there. Measured on
`dev-20261009-165346` (median per second):

| Situation | Value |
|---|---|
| speaker talking alone (no system sound) | +60 … +90 dB |
| only the video playing, speaker silent | -18 … +2 dB |
| speaker talking over the video | +11 … +34 dB |

Frames below `ECHO_ONLY_DB = 0 dB` are treated as pauses by the analysis
([§5](#5-analysis-and-voice-activity-detection)). This is the fix for the
"unstable voice" ([§10](#10-bugs-found), B9). The threshold is fixed and was
chosen on one file.

## 5. Analysis and voice activity detection

`audio.analyze()` measures, with ffmpeg (`astats`, `aspectralstats`,
`loudnorm`):

- per 21.3 ms frame: level, spectral flatness, spectral centroid;
- clipping ratio, mains hum (50/60 Hz prominence), band energies (rumble
  <80 Hz, mud 150-350 Hz, presence 3-8 kHz, sibilance 5-9 kHz, relative to the
  0.3-3 kHz voice band), EBU R128 loudness, LRA and true peak.

**Voice activity detection (`vad`)** is adaptive to each file. Voiced speech
is harmonic (low flatness) with energy low in the spectrum; breaths, fans,
keyboard and clicks are noise-like and bright. The thresholds sit between the
quietest 15% and the loudest 30% of the frames of that very file. A frame
must also be at least 6 dB above the floor. The result is smoothed with
phonetic timings: gaps under 0.12 s are inside words, bursts under 0.10 s are
clicks, with margins of 0.06 s before and 0.12 s after.

From the speech/non-speech split come the speech level (median), the noise
floor (20th percentile of non-speech), the pause level (95th percentile of
non-speech), the active ratio and the speech level spread over ~1 s chunks.

**Known weakness:** the VAD recognises *speech*, not *the speaker*. A video
playing through speakers is speech too. Before the echo-only mask, 80-84% of
`dev-20261009-165346` was classified as speech; with the mask, 52-54%.

## 6. The circuits (plan)

`plan()` is a pure function of the analysis (easy to unit-test). Each stage is
enabled only when the measurements call for it:

| Circuit | Turns on when | Filter (ffmpeg) | How it is tuned |
|---|---|---|---|
| declip | > 2e-5 of samples at full scale | `adeclip` | — |
| highpass | always | 2× `highpass` | 70/80/100 Hz from the rumble band |
| dehum | a 50/60 Hz line ≥ 12 dB above its neighbours | 4× `bandreject` | fundamental + 3 harmonics |
| preamp | speech below -30 dBFS | `volume` | up to -24 dBFS, max +24 dB, peaks ≤ +6 dBFS (float) |
| denoise | the noise floor, projected after normalisation, would be > 4 dB above -68 dBFS | `arnndn` (RNNoise, bundled speech model), mix 40-100% | mix = needed / 14 dB |
| pauses | non-speech, after normalisation, would be > 4 dB above -60 dBFS | `asendcmd` + `volume@pauses` | depth 6-24 dB, ramps 30 ms open / 250 ms close, driven by the VAD |
| leveler | speech level spread > 4 dB | `dynaudnorm f=400 g=15 m=8` | threshold `t` = speech level -18 dB (pauses are not raised) |
| mud | 150-350 Hz > 2 dB over the voice band | `equalizer 250 Hz` | -2 … -5 dB |
| presence | 3-8 kHz < -26 dB under the voice band | `equalizer 3.5 kHz` | +2 … +5 dB |
| deesser | 5-9 kHz > -10 dB | `deesser` | fixed |
| compressor | there is speech | `acompressor` | 2:1 / 3:1 / 4:1 from the LRA (7 / 12 LU), threshold speech +6 dB |
| loudness | always | `loudnorm` two-pass, linear | -14 / -16 / -23 LUFS (youtube / podcast / broadcast) |
| limiter | always | `alimiter`, 4× oversampled | true peak -1 dBTP |

**Denoise feedback check (`guard_denoise`)**: before rendering, the chain up
to the denoiser is run, and the level lost by the speech frames is measured
(preamp gain accounted for). If RNNoise takes more than 3 dB of speech, its
mix is lowered or it is replaced by a gentle spectral denoiser (`afftdn`).
The rule is that the voice comes before cleanliness.

> [!NOTE]
> On every real recording with speakers, RNNoise was measured to take
> **19.6-30.8 dB** of "speech" and was replaced by `afftdn`. Part of that
> "speech" was the video's residue, which RNNoise rightly removes. Even with
> the echo-only mask, the check still reports 19.6 dB, which needs
> investigating (open problem P4).

## 7. Rendering

- **Two passes**: a measuring `loudnorm` pass after the planned stages, then
  the render with `loudnorm linear=true` using the measured values (no
  dynamic gain riding), then the oversampled limiter.
- **With a system sound track** the render is a filtergraph. The voice
  (cleaned by the echo canceller when available) goes through the chain; the
  system sound is resampled and converted to stereo **without any other
  processing**; the two are summed (`amix normalize=0`), and only the limiter
  acts on the sum.
- **Processing off** (`audio.processing: off`): `mixdown()` still sums the two
  tracks, untouched, because players play one track only. The echo canceller
  is **not** applied on this path (open problem P6).
- The video stream is copied, never re-encoded. Audio goes to AAC 192 kbit/s,
  48 kHz.
- `finalize()` renames the recording to `.original` first and restores it on
  any failure.

## 8. Delay compensation

Several filters delay the voice. Since the system sound bypasses the chain,
any delay puts the voice out of sync with the picture and with the system
sound. A delayed copy of the video's echo also plays against the clean track
(comb filtering, the "room" effect). All delays are measured by
cross-correlation and undone with `atrim=start_sample=N,asetpts=PTS-STARTPTS`:

| Source | Delay | Compensation |
|---|---|---|
| `arnndn` (RNNoise, 10 ms frames) | 480 samples (10.0 ms) | `FILTER_DELAYS` |
| `afftdn` (spectral denoise) | 1200 samples (25.0 ms) | `FILTER_DELAYS` |
| `alimiter attack=2` (lookahead) | 96 samples (2.0 ms), on the whole mix | `LIMITER_DELAY` |
| AAC encoder priming in Matroska (no edit list) | 1024 samples (21.3 ms) | `AAC_DELAY`, MKV/MKA/WebM only |
| `highpass` ×2 at 80 Hz | ~1.5 ms in a cross-correlation | none: it is a low-frequency phase shift, not a delay |
| `loudnorm`, `dynaudnorm`, `acompressor`, `volume`, 4× resampling | 0 | — |

`tests/test_audio.py::test_filter_delays_are_the_ones_compensated` re-measures
the table with the installed ffmpeg, so a version change cannot move the audio
silently. `test_processed_voice_stays_in_sync_with_system_sound` checks the
end-to-end alignment (≤ 2 ms, the high-pass phase).

Result on the author's recording `dev-20261009-161737`: processed voice vs
original microphone, from **+26.9 ms** to **-0.15 ms**.

## 9. Evolution

| Commit | Change |
|---|---|
| `1fbb019` | Adaptive post-processing: analysis, plan, RNNoise, denoise feedback check, two-pass loudness, oversampled limiter; tuned on real OBS recordings |
| `b8cafa8` | Clicks fixed: `audiomixer` dropped late microphone buffers; with one input the mixer is bypassed, with two it gets 200 ms latency |
| `f5e6f26` | VAD drives the processing: noise floor and speech level on the right frames, "pauses" stage before leveler and compressor; loud non-speech from +0.2 dB (original) and -4.2 dB (previous) to -12.5 dB relative to the voice |
| `da35036` | Audio optimisation toggle in the microphone menu, saved in the project |
| `c14efa2`, `4eab96f` | Audio fade over the CRT closing; frame-exact start gate on audio too |
| `0170065` | **Multitrack**: system sound on its own track, laid over the processed voice untouched (verified to 0.03 dB); mono duplicated at full level |
| `dbed9fd` | **Echo canceller** (stage 1), filter delay compensation, AAC priming in Matroska |
| `71ae9ab` | **Echo-only mask** (speaker silent → pauses), residual echo suppression (stage 2) |

## 10. Bugs found

| # | Symptom | Cause | Fix | Evidence |
|---|---|---|---|---|
| B1 | Thousands of 10 ms clicks in GUI recordings | `audiomixer` dropping/resyncing late microphone buffers | no mixer; separate tracks | GStreamer logs (`b8cafa8`) |
| B2 | Breaths, keyboard, fan raised with the voice | leveler and compressor act on everything | VAD-driven "pauses" stage before them | -12.5 dB non-speech (`f5e6f26`) |
| B3 | Filters applied to a YouTube voice playing underneath | single mixed track: the processor saw everything as the speaker | multitrack (`0170065`) | — |
| B4 | Mono tracks 3 dB too quiet in the mix | default upmix at -3 dB per side | `pan=stereo\|c0=c0\|c1=c0` | fidelity test |
| B5 | "Room"/echo effect on the system sound | the microphone's copy of the speakers, raised ~20 dB as voice, laid over the clean track **45.6 ms late** | echo canceller + delay compensation | correlation of the final mix with the system track: room copy 0.41 vs direct 0.05 (-17.8 dB) → direct 0.22 vs room copy 0.02 (+20.8 dB) |
| B6 | Voice late on the picture | RNNoise 10 ms, afftdn 25 ms, limiter 2 ms, not compensated | `FILTER_DELAYS`, `LIMITER_DELAY` | +26.9 ms → -0.15 ms |
| B7 | MKV outputs: audio 21 ms late | AAC priming not recorded by Matroska | pre-trim 1024 samples | test measured 22.75 ms before |
| B8 | First fix compensated RNNoise only, voice still 24.85 ms late | on that file the guard had replaced RNNoise with `afftdn`, whose 25 ms were unknown | per-filter delay table + test | — |
| B9 | **Voice "better and worse by turns"** | the VAD took the video's residue for the speaker (80-84% "speech"); preamp + leveler + normalisation raised silent stretches by up to +30 dB (e.g. -41 dB in → -11 dB out) | echo-only mask: those frames are pauses | silent stretches: second 17 from -14.0 to -24/-26.5 dB, second 21 from -11.9 to -16/-18.7 dB; talking stretches unchanged within 1 dB |
| B10 | One run of `test_process_cleans_and_normalizes` failed (SNR gain 10.46 dB < 15) | unknown: not reproduced in 4 later runs | none yet | flaky? |

## 11. Attempts and measurements

All on the author's recording `dev-20261009-161737` (speakers, YouTube,
44.8 s) unless stated otherwise. "Synthetic" is the test case of
`test_speaker_echo_is_removed_from_the_microphone`: system sound speech
samples, a room with reflections at 18, 35 and 65 ms, and no clock drift.

**Diagnosis of B5** (16 kHz mono, cross-correlation):

| Pair | Correlation | Lag |
|---|---|---|
| microphone vs system track | 0.28 | 17.7 ms (speaker bleed) |
| final mix vs system track | -0.36 | 45.6 ms (the bleed, delayed again by processing, dominates) |
| final mix vs microphone | 0.79 | 26.9 ms (processing delay) |

Levels: microphone -34.5 LUFS, system -27.6 LUFS, final -13.9 LUFS.

**Attempt 1: ffmpeg `anlms`** (time-domain NLMS, reference = system,
desired = microphone). Total microphone level in a video-only stretch
(-34.3 dB in the same decoding):

| `out_mode` | Level | Verdict |
|---|---|---|
| `o` | -37.9 dB | -3.6 dB: too little |
| `e` (error = cleaned) | -33.6 dB | no reduction |
| order 4096, mu 0.3-0.8 | -31.3 … -31.4 dB | worse |

Rejected: slow convergence over a long room response, with clock drift on top.

**Attempt 2: global least squares** (STFT, one filter for the whole file),
taps 8 / 24 / 48 (43 / 128 / 256 ms): total level only -2.3 … -2.9 dB. The
total level was the wrong metric, see [§12](#12-measurement-methods-and-their-traps).
The real limit was clock drift, measured as the lag sliding from 17.6 to
18.9 ms in 26 s.

**Attempt 3: drift compensation** (fit of the lag over time, +80.9 ppm, then
resampling of the reference) plus a global filter: no gain over short blocks,
which already follow the drift. Dropped for simplicity.

**Attempt 4: blockwise least squares**, coherence metric (echo energy per
band, 100-500 / 500-1500 / 1500-4000 / 4000-8000 Hz, dB):

| Version | Bands | Voice level |
|---|---|---|
| microphone | -10.3 / -12.6 / -24.3 / -28.5 | -33.0 dB |
| global LS | -31.9 / -30.7 / -35.8 / -46.3 | — |
| 1 s blocks + drift warp | -31.6 / -31.9 / -45.1 / -46.1 | — |
| `echo.py` v1 (1 s blocks, chunked) | -34.2 / -34.2 / -47.2 / -51.8 | -33.4 dB |

**Block length sweep** (real: total coherent echo, microphone -8.1 dB;
synthetic: bleed reduction and correlation with the true voice):

| Block | Real | Synthetic | Voice corr. |
|---|---|---|---|
| 1.0 s | -30.6 dB | 10.9 dB | 0.964 |
| 1.5 s | -29.7 dB | 12.1 dB | 0.973 |
| **2.0 s (chosen)** | -29.9 dB | 13.4 dB | 0.980 |
| 3.0 s | -28.9 dB | 15.2 dB | 0.987 |
| 6.0 s | -28.9 dB | — | — |

Longer blocks estimate better (synthetic, no drift); shorter ones follow the
drift (real). Other STFT shapes on the synthetic case were all worse or much
slower: N 2048 / hop 512 / 10 taps gave 12.0 dB, N 512 / hop 128 / 36 taps
10.5 dB in 29 s.

**Residual suppression settings** (synthetic):

| Setting | Bleed reduction | Voice corr. | Voice level |
|---|---|---|---|
| off | 13.4 dB | 0.980 | -0.24 dB |
| OVER 2, FLOOR 0.1 | 11.4 dB | 0.973 | -1.20 dB |
| **OVER 1, FLOOR 0.3 (chosen)** | 13.7 dB | 0.984 | -0.85 dB |
| OVER 1, FLOOR 0.1 | 13.3 dB | 0.982 | -0.88 dB |

On the real `dev-20261009-165346`, stage 2 lowered the video-only stretches by
a further 1-4 dB.

**Echo per segment on `dev-20261009-165346`** (coherent echo / everything
else, relative dB, before → after stage 1):

| Seconds | Microphone | Cleaned |
|---|---|---|
| 6-11 | 5.3 / 18.0 | -24.9 / 14.5 |
| 11-15 | 4.8 / 22.2 | -15.2 / 21.3 |
| 15-19 | 4.6 / 14.1 | -17.7 / 7.1 |
| 19-26 | 6.4 / 19.5 | -24.4 / 13.2 |
| 26-30 | 12.6 / 23.0 | -11.1 / 20.4 |
| 30-37 | 7.1 / 25.0 | -12.0 / 23.7 |

The coherent echo drops by 20-30 dB, but "everything else" is the larger
part of the microphone. It is the speaker's voice plus what a linear model
cannot explain (tail, distortion), and that remainder is what processing used
to raise (B9).

## 12. Measurement methods (and their traps)

- **Total level is not an echo metric.** When the speaker talks over the
  video, the microphone's level is mostly voice. Attempt 2 looked like a
  failure (-3 dB) when it was removing ~20 dB of echo. Use **coherence with
  the reference** (energy of the output linearly explained by the system
  track, per band) or, on synthetic cases, the residual against the known
  voice.
- **Coherence measured on the data the filter was fitted on is optimistic.**
  Least squares minimises exactly that correlation. The synthetic test with
  the true voice is the honest check.
- **Cross-correlation on processed audio**: dynamic stages and the mix shift
  the peak. The direct/room-copy test looks at lag ±1 ms (direct) and lag
  > 5 ms (room copy) separately.
- **Timestamp-synced comparisons can pair the wrong frames.** When files have
  different start offsets, compare by sample index.
- **Raw decoding ignores start times**: `ffmpeg … -f f32le` returns samples
  from the first decoded one. GStreamer's MP4s have no edit list for the AAC
  priming, while ffmpeg's do. Compare files written by the same muxer, or
  account for it.
- **Nothing replaces listening.** All the numbers above improved, and the
  author still hears problems. There is no listening test protocol yet (P8).

## 13. Open problems

| # | Problem | Notes |
|---|---|---|
| P1 | **Quality not confirmed by listening** after `71ae9ab` | the author's last verdict predates it |
| P2 | **Residue beyond the linear model**: reverberant tail > 85 ms, speaker non-linearity | stage 2 is gentle on purpose; a stronger post-filter costs voice |
| P3 | **Hand-tuned constants**: `BLOCK`, `TAIL`, `OVER`, `FLOOR`, `ECHO_ONLY_DB`, VAD thresholds | chosen on two recordings; they should be derived from each recording ([§14](#14-proposed-next-steps)) |
| P4 | **RNNoise always replaced by `afftdn` with speakers** (19.6-30.8 dB "speech" loss) | the speech frames used by the check may still include residue; `afftdn` is weaker and adds 25 ms |
| P5 | **Large gains** on quiet microphones: preamp +12…+17 dB, leveler up to +18 dB, normalisation on top | any residue left in frames classified as speech is raised with the voice |
| P6 | **No echo cancelling when processing is off** (`mixdown`) | the bleed stays at its natural level, less audible, but the copy still sits 11-18 ms behind the clean track |
| P7 | **Voice/system balance** (E5) | the voice goes to -14 LUFS, system sound keeps its level: on `dev-20261009-165346` the video ends up ~10 dB under the voice, unlike in the room |
| P8 | **No listening protocol, no reference corpus** | decisions rest on objective metrics only |
| P9 | **Double talk**: while the speaker talks over the video, the estimate's variance grows (the voice is noise for the estimator) | longer blocks help but follow the drift worse |
| P10 | Flaky test (B10) | — |
| P11 | GStreamer MP4s carry the AAC priming without an edit list | the effect on audio/video sync of the *original* files was not measured |

## 14. Proposed next steps

1. **Listen first**: record the same scene (speakers, a video, talking over
   it) and compare the original, the processed result before `71ae9ab` and
   after it. Write down where it fails, with timestamps.
2. **Self-tuning per recording** (the author's suggestion): derive the
   remaining constants from each recording instead of fixing them.
   - The **tail** from the estimated room response (its decay).
   - The **residue level** from the echo-only stretches.
   - The **echo-only threshold** from the distribution of the per-frame
     ratios, which is bimodal (echo only around -5 dB, speaker above +10 dB).
   - The **block length** from the measured clock drift.
3. **Optional speaker calibration** ("Calibrate speakers"): play a short test
   signal (sweep or noise, ~5 s) with nobody talking, measure the room
   response, its length and the speakers' non-linearity, and store a profile
   per output device. It gives a clean prior for the estimator and its
   constants, and could help live use.
4. **A stronger, voice-safe post-filter** for P2, guided by the voice/echo
   mask and evaluated on the synthetic test plus real recordings.
5. **Gain staging review** (P5): cap the total gain on frames that the mask
   marks as uncertain; consider applying the leveler only on confirmed speech.
6. **Decide the balance policy** (P7): keep the system sound as is, or match
   the voice/system ratio heard in the room (measurable from the speaker
   bleed itself).
7. **Echo cancelling on the processing-off path** (P6).
8. **A small reference corpus** (voice only, voice + speakers, voice +
   headphones, different rooms), with the objective metrics of this document
   computed in CI.

## 15. Reproducing the analyses

```bash
# What the processor would do, without rendering
rec0 process ~/Videos/rec0/dev/NAME.original.mp4 --dry-run

# Render again from the original (the separate tracks are kept there)
rec0 process ~/Videos/rec0/dev/NAME.original.mp4 -o /tmp/out.mp4

# Loudness of each track
ffmpeg -i NAME.original.mp4 -map 0:a:0 -af ebur128=peak=true -f null -   # microphone
ffmpeg -i NAME.original.mp4 -map 0:a:1 -af ebur128=peak=true -f null -   # system sound

# Tests: echo removal, sync, filter delays
python3 -m pytest -q tests/test_audio.py
```

From Python, the echo canceller alone and its per-frame mask:

```python
from rec0 import echo
ratios = echo.cancel("NAME.original.mp4", "/tmp/voice.wav")   # None: nothing to cancel
```

The helpers `_read`, `_two_tracks` and `_lag_ms` in `tests/test_audio.py`
decode a track to a numpy array, build a two-track test file and measure a
lag.
