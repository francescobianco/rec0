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
> two recordings. The subsequent circuit audit (§16) fixes hidden dynamic
> normalisation, misaligned denoise checks and pause timing; its renders still
> need listening approval. The real experiment of the evening (§19, a podcast
> as a constant-level voice, speech and music from the PC) shows that the
> canceller removes only 5-7 dB of the microphone's total energy and that the
> voice is gated more than a quarter of the time while the PC plays. Read this
> document as the record of an ongoing investigation, not as the description
> of a finished feature.

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
16. [Circuit audit and corrections](#16-circuit-audit-and-corrections)
17. [Measurement and listening protocol](#17-measurement-and-listening-protocol)
18. [Improvement strategy](#18-improvement-strategy)
19. [Real experiment: a podcast as the voice](#19-real-experiment-a-podcast-as-the-voice)

---

## 1. Quality expectations

What "good" means for rec0, in the author's words and as requirements:

| # | Expectation | Status |
|---|---|---|
| E1 | A first-time creator with a cheap microphone in a noisy room gets clean, even voice at YouTube loudness (-14 LUFS, -1 dBTP) without touching anything | reached on voice-only recordings (synthetic and real); **not** when system sound plays through speakers |
| E2 | What plays on the computer while recording is in the video **as heard live**, "as if overlaid" | the system sound track is laid over untouched (verified to 0.03 dB); but see E3 and E5 |
| E3 | No echo or "room" effect on the system sound when speakers are used instead of headphones | much improved (from the room copy 18 dB **above** the clean sound to 21 dB **below** it), residue still audible according to the author; the canceller removes only **5-7 dB of the microphone's total energy** on real recordings ([§19](#19-real-experiment-a-podcast-as-the-voice)) |
| E4 | Stable processing: the voice must not change character from one moment to the next | **not reached**: measured on `dev-20261009-190333`: while the PC plays, the voice is lowered by more than 6 dB in 27% of 100 ms windows, with 88 gate transitions in 43 s ([§19](#19-real-experiment-a-podcast-as-the-voice)) |
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
    guard --> meas[loudness measurement]
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
| loudness | always | `loudnorm` measurement + constant gain | -14 / -16 / -23 LUFS (youtube / podcast / broadcast) |
| limiter | always | `alimiter`, 4× oversampled | true peak -1 dBTP |

**Denoise feedback check (`guard_denoise`)**: the chain up to the denoiser
is compared with the **same upstream chain without the denoiser**, on
sample-aligned speech frames. Median loss must stay within 3 dB. A reduced
RNNoise mix is measured again; the `afftdn` fallback is also measured,
including when it was selected initially. If the fallback still exceeds the
limit, denoising is bypassed. Failed trials raise an error instead of
reporting zero loss. This protects measured speech level, not every aspect
of perceived quality: spectral damage can pass a level-only check.

> [!NOTE]
> On every real recording with speakers, RNNoise was measured to take
> **19.6-30.8 dB** of "speech" and was replaced by `afftdn`. Part of that
> "speech" was the video's residue, which RNNoise rightly removes. Even with
> the echo-only mask, the old check reported 19.6 dB. The aligned,
> denoiser-only check reports about **17.7 dB** on `dev-20261009-165346`:
> the loss was mostly real according to this metric, not just a timing error.
> The verified spectral fallback loses approximately 0.0 dB on those frames.
> Neither number proves that every frame labelled speech contains the author.

## 7. Rendering

- **Measurement then constant gain**: `loudnorm` measures the planned voice
  chain; rendering applies `volume=(target LUFS - measured LUFS)dB`, followed
  by the oversampled limiter. The previous `loudnorm linear=true` could
  silently revert to dynamic processing when LRA or peaks exceeded its
  constraints. This occurred on the real problem recording (§16).
- Measurement uses the same aligned voice chain and channel conversion as
  rendering. An echo-cancelled voice is mono even if the captured microphone
  was stereo; its actual channel count determines the full-level stereo
  duplication. Loudness is measured **after** that duplication for a mix.
- Constant normalisation does not remove the planned leveler or compressor.
  Those remain intentional dynamic stages and need their own quality checks.
  The limiter can lower achieved loudness; the report records final LUFS,
  true peak, requested constant gain and whether the voice peaks require
  limiting. That flag does not measure how much the final mix was limited.
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
(comb filtering, the "room" effect). Filter delays are measured by
cross-correlation and undone with `atrim=start_sample=N,asetpts=PTS-STARTPTS`.
Denoiser compensation now happens **immediately after that filter**, at
48 kHz, before the VAD-driven pause envelope. Silence is padded before the
filter to recover its delayed tail; padding and trimming preserve the voice
length. The processing and mixdown limiters also receive padding before their
lookahead is removed, at the internal 192 kHz rate. Pause commands run on 1024-sample frames, matching analysis.
The original delay measurements were:

| Source | Delay | Compensation |
|---|---|---|
| `arnndn` (RNNoise, 10 ms frames) | 480 samples (10.0 ms) | `FILTER_DELAYS` |
| `afftdn` (spectral denoise) | 1200 samples (25.0 ms) | `FILTER_DELAYS` |
| `alimiter attack=2` (lookahead) | nominally 96 samples at 48 kHz; actual 383 samples at its 192 kHz working rate (1.995 ms), on the whole mix | `_limit_chain()`: pad/trim at 192 kHz before downsampling |
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
- **Downmixing can change the measured gain.** FFmpeg's stereo-to-mono
  conversion depends on the negotiated format/layout and can sum duplicated
  channels at +3 dB relative to either channel. For channel fidelity, decode
  `pan=mono|c0=c0` or compare the same layout on both sides. A gain error in
  the measurement must not become a compensating error in processing.
- **Nothing replaces listening.** All the numbers above improved, and the
  author still hears problems. The protocol in §17 still needs to be executed (P8).

## 13. Open problems

| # | Problem | Notes |
|---|---|---|
| P1 | **Quality not confirmed by listening** after `71ae9ab` | the author's last verdict predates it; a listening set for `190333` is ready in `~/Videos/rec0/dev/listen-190333/` |
| P2 | **Residue beyond the linear model**: reverberant tail > 85 ms, speaker non-linearity | stage 2 is gentle on purpose; a stronger post-filter costs voice |
| P3 | **Hand-tuned constants**: `BLOCK`, `TAIL`, `OVER`, `FLOOR`, `ECHO_ONLY_DB`, VAD thresholds | chosen on two recordings; they should be derived from each recording ([§14](#14-proposed-next-steps)) |
| P4 | **Denoise quality with speakers** | checks now align samples and isolate denoising; fallback and reduced mixes are verified. Large RNNoise loss persists on `165346`; reduced RNNoise passes on `183203`. VAD and spectral preservation remain open |
| P5 | **Large gains** on quiet microphones: preamp +12…+17 dB, leveler up to +18 dB, normalisation on top | any residue left in frames classified as speech is raised with the voice |
| P6 | **No echo cancelling when processing is off** (`mixdown`) | the bleed stays at its natural level, less audible, but the copy still sits 11-18 ms behind the clean track |
| P7 | **Voice/system balance** (E5) | the voice goes to -14 LUFS, system sound keeps its level: on `dev-20261009-165346` the video ends up ~10 dB under the voice, unlike in the room |
| P8 | **Reference corpus and listening approval missing** | protocol now defined in §17, not yet executed as a listening study |
| P9 | **Double talk**: while the speaker talks over the video, the estimate's variance grows (the voice is noise for the estimator) | longer blocks help but follow the drift worse |
| P10 | Flaky test (B10) | — |
| P11 | GStreamer MP4s carry the AAC priming without an edit list | the effect on audio/video sync of the *original* files was not measured |
| P12 | **The linear echo model explains only 5-7 dB of the microphone** (2 dB on stereo music) | neither more taps, longer blocks, drift warping nor a stereo reference change it ([§19](#19-real-experiment-a-podcast-as-the-voice)); the cause is not identified |
| P13 | **The echo-only mask has nothing to work with when the voice is as loud as the echo** | the per-frame ratio sits at 0 dB with a podcast playing over PC speech; every threshold rule gives the same statistics as on a (presumed) silent-speaker recording; gating 24 dB on it is the pumping of P1/E4 |

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


## 16. Circuit audit and corrections

Audit of 2026-10-09. These are implementation corrections, **not a declaration
that E1–E7 are all met**. In particular, perceived stability, double-talk
quality, residual room sound and voice/system balance remain acceptance work.

### Confirmed defects

| Defect | Evidence / consequence | Current correction |
|---|---|---|
| Hidden dynamic normalisation | On `165346`, the old post-chain measurement was -24.43 LUFS, -10.78 dBTP, LRA 14.50 LU. The render requested LRA 11; even the target gain would put peaks at -0.35 dBTP. Either condition prevents linear mode. Another gain controller acted after the leveler and compressor | Explicit constant `volume` gain; one final oversampled limiter |
| Denoise check included upstream losses | The old comparison used raw analysis levels plus preamp gain, so high-pass/dehum losses also counted against RNNoise | Compare otherwise identical chains with and without denoising |
| Denoise check compared different samples | The 10/25 ms filter delay was not removed in the measurement trial | Shared `_voice_chain()` aligns each denoiser before subsequent stages and measurements |
| Pause envelope preceded delay compensation | VAD timestamps referred to the original voice, but pause gain acted on delayed audio | Compensate immediately after denoising; use analysis-sized frames for pause commands |
| Reduced mix and fallback were trusted without checking | The initial RNNoise check did not establish safety of the selected replacement | Re-measure each selected candidate; bypass if measured loss still exceeds 3 dB |
| Fallback noise floor used the wrong gain domain | `afftdn` received the original noise floor after the preamp had raised the signal | Add preamp gain to the fallback noise-floor parameter |
| Channel layout inferred from the wrong source | AEC writes mono; rendering previously used the captured microphone's channel count. Default mono/stereo conversion can attenuate each side by 3 dB | Use the actual cleaned source's channel count; measure after the same stereo conversion used for rendering |
| Delay trimming discarded the tail | Removing startup samples without feeding the buffered tail shortened the voice | Pad before denoiser and limiter, then trim their known delays |
| Limiter trim rounded to 48 kHz samples | The actual lookahead is `attack_samples - 1`: 383 samples at 192 kHz. Trimming 96 samples after downsampling advanced the signal by 0.25 sample | Pad/trim 383 samples at the internal rate in both processing and mixdown; sample-level system fidelity regression |
| Failed measurement could appear safe | Empty trial output could produce zero estimated loss | Fail the trial explicitly; do not interpret missing evidence as preserved voice |

The [FFmpeg loudnorm documentation](https://ffmpeg.org/ffmpeg-filters.html#loudnorm)
explicitly describes the conditions for reverting from linear to dynamic mode.
The former statement in §7 that `linear=true` guaranteed no gain riding was
incorrect. Constant gain removes that hidden controller; it does not prove
that the remaining leveler, compressor and pause envelope sound natural.

### Checks and real renders

On `165346`, the old guard reported 19.64 dB loss. Correcting alignment and
isolating the denoiser reduced this to about 17.69 dB, still unacceptable.
The replacement `afftdn`, measured at its actual input level, loses about
0.00 dB on the selected speech frames. On `183203`, a reduced RNNoise mix
of about 54% passes the recheck at about 2.7 dB loss. These are **median
frame-level losses**, not intelligibility or perceptual quality scores.

| Recording | Current final integrated loudness | Current final true peak | Current LRA |
|---|---:|---:|---:|
| `dev-20261009-165346` | -14.68 LUFS | -1.67 dBTP | 13.7 LU |
| `dev-20261009-183203` | -14.65 LUFS | -1.41 dBTP | 12.8 LU |

Measured from the encoded output using the same FFmpeg loudness pass. The
pre-audit rerender of `165346` was -14.52 LUFS, -2.52 dBTP, LRA 10.9 LU.
A larger LRA is not automatically worse: the removed hidden controller used
to compress it. These aggregate numbers do not establish steadier timbre.

An experiment limiting the constant gain to available peak headroom made the
voice-only regression output -18.06 LUFS instead of the required -14 ±1.
That experiment was rejected. Current code applies the target constant gain
and lets the existing limiter handle peaks. Sustained limiting is still a
risk to measure; target loudness alone is not an acceptance criterion.

Validation: `python3 -m pytest -q` completed with **49 passed** using
FFmpeg `6.1.1-3ubuntu5`. The pre-audit baseline is revision `d2144cf`; current
results use the working-tree corrections described here. Preserve the final
revision or patch with the audio artifacts when archiving this comparison.

Regression coverage now includes:

- Constant sample gain on a signal with large level changes and a transient,
  including a case where the target gain exceeds peak headroom.
- Denoise loss isolated from upstream attenuation.
- Rejection of damaging fallbacks and insufficient mix reductions.
- Denoiser delay and full output duration, including the buffered tail.
- System-track sample level, alignment and duration through a transparent
  PCM mix below the limiter ceiling.
- Failed denoise trials reported as failures.
- Existing encoded-output loudness, true peak, synthetic echo removal,
  synchronisation and original-restoration checks.

**Pause fades, verified on `dev-20261009-185025`** (the recording where the
click was found): around 23.744 s the largest jump between two samples went
from 0.289 (render with the stepped envelope) to 0.101 with the signal's
100 ms envelope at 0.32-0.35, so it is no longer a discontinuity relative to
the signal. The largest remaining jump of that file (22.978 s, 0.162 with an
envelope of 0.019) is in the **system track itself** at the same instant and
with the same size (0.161): it is something the computer played, carried
faithfully. `build-aux/audio-lab/clicks.py` lists such jumps.

The PCM system-track test isolates processing errors from AAC loss. It does
not establish transparency when the summed voice and system sound drive the
limiter. The existing synthetic echo case uses shaped noise and a fixed room
response; it does **not** cover real video speech, clock drift or moving speakers.
The system fidelity test reads one stereo channel explicitly to avoid the
stereo-to-mono measurement trap in §12.

## 17. Measurement and listening protocol

### Keep the evidence reproducible

For each reference recording, keep the untouched multitrack original, code
revision, FFmpeg version, selected stages and parameters, analysis/report JSON,
and output file. Record microphone/output devices, speaker volume, room,
headphone/speaker use and any movement. Keep human-labelled intervals separate
from VAD decisions: a detector must not grade itself.

The CLI `--dry-run` currently analyses the original first track directly; it
does not run AEC or the denoise guard. Its plan is therefore **not** the final
plan for speaker recordings. Use the `Report` returned by `audio.process()`
for the actual decisions, and save `report.to_json()` beside each candidate.
Temporary pause-command paths in reports are diagnostic, not reusable after
the processing temporary directory is removed.

### Listening, with controlled volume

For each recording prepare: microphone alone; untouched mic+system mix;
AEC-only mic+system mix; previous full processing; current full processing.
Also retain the isolated microphone after each relevant stage. A media player
opening the original multitrack file usually plays just one track, so it is
not a valid substitute for the untouched sum.

Compare the same time ranges, switching between files without changing playback
volume. Prepare an additional loudness-matched set by applying **one constant
gain per whole file**, bringing louder candidates down to the quietest
candidate. Do not normalise each excerpt independently: that would hide the
level instability being evaluated. Keep the native-level set to assess E1/E5.
Prefer headphones for comparison so playback-room echo does not contaminate
the judgment. Randomise A/B names when possible.

Use labelled examples of voice alone, system alone, double talk, quiet words,
consonant onsets, word endings, breaths, keyboard sounds, and changes of speaker
volume or microphone position. For every defect record its start/end time,
which candidate is worse, and whether it is lost speech, changing timbre,
pumping, echo, clicks, wrong balance or delay. A numeric score without a
location and description is not sufficient to tune the circuit.

For `165346`, the historical 6–11, 11–15, 15–19, 19–26, 26–30 and 30–37 s
intervals are useful starting points. They must be labelled by listening;
the old coherence table is not ground-truth speaker activity.

### Measure each requirement separately

| Requirement / failure | Measurement | Control against misleading results |
|---|---|---|
| Voice preservation | On synthetic mixtures, residual against known clean voice, gain error and correlation; on real speech, per-interval level and spectral changes | Retain absolute gain error as well as scale-invariant measures; attenuation must not score as cleanup |
| Echo removal | Echo-only residual level; reference coherence by band; synthetic residual against clean voice | Evaluate held-out intervals and double talk separately; in-sample least-squares coherence is optimistic |
| Stable processing | Per-speech-interval gain relative to aligned pre-stage audio; median, 90th/99th percentile and maximum change, plus spectral change | Use the same independently labelled speech intervals across candidates; exclude intended pauses without hiding quiet words |
| Noise reduction | Noise floor and loud transient levels in labelled pauses, before/after at matched speech level | SNR improvement can be caused by damaged or misclassified speech; include listening and speech preservation |
| System fidelity | Before encoding, difference from the original system track with voice absent; after AAC, gain and lag error plus residual | Below limiter threshold require transparency; evaluate limiter-active segments separately |
| Loudness and peaks | Final encoded mix LUFS/LRA/dBTP and isolated processed voice loudness | Measure final layout and codec; a voice target does not imply the same loudness for an arbitrary sum |
| Limiter side effects | Gain reduction envelope, time spent limiting, longest limiting interval | The current report only predicts whether voice peaks need limiting; envelope instrumentation is future work |
| Sync and duration | Sample-index lag and sample counts, plus audio/video event alignment | Test 44.1/48 kHz inputs, mono/stereo, nonzero timestamps and container priming; an audio correlation alone does not validate picture sync |
| Balance | Voice/system level ratio on matched labelled intervals; listening against the intended reference | “As heard in the room” is not directly recoverable from microphone/system digital levels without an acoustic reference |

For every adaptive stage, run an **ablation**: identical input and identical
remaining stages, with only that stage bypassed. Measure both its direct
output and the final rendered mix: later normalisation can erase a useful
noise reduction or amplify a harmful residue. Keep loudness matching fixed
across the comparison. Change one mechanism at a time before combining fixes.

## 18. Improvement strategy

The next work should add evidence and reduce unjustified processing, not
increase attenuation until one aggregate score looks better.

1. **Establish a small labelled corpus.** Include clean voice, quiet/noisy
   microphones, speakers and headphones, system speech/music, long pauses,
   double talk and movement. Keep some rooms/devices out of parameter tuning.
   Existing real recordings are regression examples, not an independent
   evaluation set. Obtain listening verdicts on the current corrections first.
2. **Instrument actual gain through the chain.** Log short-window gain for
   AEC residual suppression, denoiser, pauses, leveler, compressor and limiter.
   Identify which stage causes each reported fluctuation. Extend the current
   median denoise guard to upper-tail and contiguous-interval losses only
   after validation on labelled quiet speech; do not choose thresholds on one
   recording. Add spectral preservation checks because level alone misses
   metallic or muffled voice.
3. **Make decisions confidence-aware.** Treat uncertain echo/voice frames as
   uncertain rather than definitely silent. Test soft masks and hysteresis
   against clipped consonants and pumping. Consider limiting the leveler's
   additional gain during uncertain stretches; select any bound from measured
   failure cases and verify that quiet genuine speech is still intelligible.
4. **Improve AEC using independent evidence.** Estimate tail/clock drift from
   suitable reference-active intervals, validate on different intervals, and
   fall back conservatively when excitation is insufficient. Benchmark against
   established cancellers on the same corpus before replacing the current
   least-squares model. Test drift, long tails, non-linear speakers and double
   talk explicitly. Optional calibration should supply a prior, not assume
   the room or microphone position never changes.
5. **Define the balance policy.** E2 (unchanged system track), E1 (voice at a
   target) and E5 (room-like balance) can conflict. Specify which has priority
   and how the user supplies or confirms a reference balance. Do not silently
   infer a correct acoustic balance from residual echo energy.
6. **Add acceptance gates, then tune.** Require no missing/truncated speech,
   no sync regression and no system-track gain change below the limiter
   ceiling. Retain the existing numeric loudness/peak tests. Add perceptual
   acceptance per scenario, with failures retained in the corpus. Do not
   accept an echo/SNR improvement that worsens voice preservation.
7. **Track reproducibility and uncertainty.** Repeat only suspicious or
   changed cases; preserve random seeds and versions. Investigate the earlier
   flaky SNR result using saved waveforms and fixed speech labels. Report
   distributions across recordings, not just the best score or global mean.

Not implemented by this audit: acoustic calibration, a replacement AEC,
self-tuned echo masks, limiter-envelope telemetry, a new leveler, balance
policy, or AEC on the processing-off path. Those remain proposals whose
benefit must be demonstrated under the protocol above.

## 19. Real experiment: a podcast as the voice

Evening of 2026-10-09, after commit `58aa34d`. Recording
`dev-20261009-190333` (69.5 s, X11, Blue USB microphone, laptop speakers at
96%/88%), made through the running GUI with `build-aux/audio-lab/experiment.sh`.

**Setup.** A podcast playing on a phone next to the microphone is the
"voice": a real voice at a **constant level** (-34…-38 dBFS at the
microphone), so any level change of the processed voice is processing, not
the speaker. The PC played: nothing 0-11 s; **YouTube speech** (the system
track of `165346`, -25.7 LUFS) 12-30 s; nothing 31-34 s; **music**
(`Joystock - Here For You`, -15.8 LUFS, played at 50%) 35-60 s; nothing
61-69 s. The GUI processed it with the code loaded at 18:50; the original was
reprocessed with `58aa34d`. Listening set (microphone alone, untouched mix,
echo-cancelled mix, both renders, report JSON, timeline) in
`~/Videos/rec0/dev/listen-190333/`. **Not yet listened to.**

**Plan chosen** (`58aa34d`): high-pass 100 Hz; preamp +9.9 dB (voice at
-34 dBFS); RNNoise backed off to 64% (loss 2.9 dB); pauses 24 dB; compressor
2:1 (LRA 4 LU); constant gain +9.8 dB, limiter required. Output -14.2 LUFS,
-1.4 dBTP, LRA 4.3 LU.

### What was measured (`build-aux/audio-lab/evaluate.py`)

Since the system track is laid over untouched, **output − system track = the
processed voice** (plus echo residue and limiter action), and it can be
compared with the echo-cancelled microphone.

| Measure | Result |
|---|---|
| System track in the output | lag 0.000 ms, gain -0.12 dB (E2, E6 fine) |
| Microphone while the PC plays speech | -27…-33 dB, **the same level as the system track**: the speakers' echo at the microphone is as loud as the podcast; 50-80% of the microphone's energy is linearly explained by the system track |
| Voice gain, system silent (100 ms windows) | median +16.0 dB, p10 +8.6, lowered by >6 dB in 9.9% of windows, 21 gate transitions |
| Voice gain, system playing | median +13.3 dB, **p10 -7.2**, lowered by >6 dB in **27.5%** of windows (>12 dB: 17.1%), **88 gate transitions in 43 s** (code of 18:50: 33.7%, 116) |
| Discontinuities in the processed voice | none above the signal envelope (largest ratio 5.0 at 40.4 s, 0.015 on an envelope of 0.003: a pause gate opening on noise) |

The voice "better and worse by turns" (B9, E4) is this: while the PC plays,
the voice drops by more than 6 dB more than a quarter of the time, in short
stretches.

### Why: the echo-only mask has no information at this level

The per-frame ratio (cleaned microphone over removed echo, `ECHO_ONLY_DB`):

| Section | p5 | p25 | median | p75 | p95 | frames < 0 dB |
|---|---|---|---|---|---|---|
| podcast alone (1-11 s) | +74 | +79 | +84 | +88 | +93 | 0% |
| podcast + PC speech (13-30 s) | -17.8 | -9.1 | **-1.7** | +9.3 | +24.3 | **56%** |
| podcast + music (36-60 s) | -10.2 | -4.3 | **+0.1** | +6.0 | +12.7 | **50%** |

With a voice as loud as the echo the ratio is centred on 0 dB by
construction, and the threshold is a coin flip per frame. Rules compared
(raw frame ratio, energies smoothed over 5 and 9 frames, thresholds 0 and
-3 dB, a per-2 s-block floor at the 10th percentile plus 3 or 6 dB) give the
**same fraction of "echo-only" frames on `185025` 8-28 s (presumed silent
speaker) and on the podcast over PC speech** (55.5% vs 55.7%, 63.0 vs 61.9,
53.5 vs 48.6, 22.6 vs 23.7, …). Either `185025` is not a silent-speaker
recording (its microphone is at -24…-30 dB in 1-6 s with the system silent)
or the ratio does not separate the cases. In both readings, **a different
threshold will not fix the gating**; what would is removing more echo, so
that the residue sits well under the voice.

### The canceller removes 5-7 dB, whatever its parameters

Honest metric: total energy of the cleaned microphone against the microphone
(the podcast is uncorrelated with the reference, so a linear model can only
remove echo; overfitting costs < 0.5 dB with 2 s blocks).
`build-aux/audio-lab/aec_variants.py` and `stereo_ref.py`:

| Variant | `185025` 8-28 s (YouTube) | `190333` 13-30 s (PC speech) | `190333` 36-60 s (music) |
|---|---:|---:|---:|
| current (16 taps, 2 s blocks) | -6.5 dB | -7.2 dB | -2.2 dB |
| 32 taps | -7.2 | -8.0 | -2.8 |
| 4 s blocks | -6.3 | -7.1 | -1.9 |
| 32 taps, 4 s blocks | -6.8 | -7.6 | -2.2 |
| reference warped by the measured drift, 16 taps | -6.5 | -7.2 | -2.2 |
| warped, 32 taps, 4 s | -6.8 | -7.7 | -2.2 |
| warped, 32 taps, 8 s | -6.5 | -7.4 | -1.9 |
| stereo reference (L and R separately), 16 taps each | -5.1* | -5.7* | -1.9* |
| stereo reference, 24 taps each | -5.5* | -6.3* | -2.1* |

\* the stereo script measures on a single span without chunk crossfades; its
mono baseline is -5.1 / -5.7 / -1.3 dB, so the stereo gain is 0 / 0 / 0.6 dB.

Facts around it:

- **Clock drift is small here**: +6.8 / -3.4 / +15.4 ppm (the +80.9 ppm of
  §11 was measured on `161737`). Warping the reference changes nothing.
- **Stereo content is not the limit** for speech (L−R is 72 dB under L+R)
  and only part of it for music (L−R at -9.3 dB).
- **The lag of the microphone against the system track alternates** between
  ~4.6 and ~6.0 ms from one second to the next on `190333` (once 2.65 ms),
  identically against the left and the right channel, so it is not the two
  speakers. On `185025` it is 15.2-15.5 ms with one block at 16.5 ms. The
  normalised correlation peak is only 0.3-0.6 with speech, 0.1-0.2 with music.
- **The room response estimated over 4 s** (400 ms FIR, Wiener in the
  frequency domain at 16 kHz): direct path at 3.4 ms, only 46% of the energy
  within the first 20 ms, 85% within 300 ms, and a **flat plateau at
  -17…-20 dB for 300 ms** instead of a room decay. That is the signature of a
  **time-varying delay or of a component the reference does not predict**,
  not of a long reverberant tail.

### Hypotheses to test next (in this order)

1. **Time-varying delay between the monitor capture and the speakers.** The
   monitor of the sink and the DAC may not run sample-locked (PipeWire
   quantum scheduling, resampler state). Test: a loud broadband signal
   through the speakers with nobody talking (and the phone off), lag measured
   every 100 ms with sub-sample precision. If the lag jumps by ~1.4 ms, a
   block least-squares filter can only average over the jumps; the fix is
   to track the delay per block (or per frame) before estimating the room,
   or to capture the reference elsewhere.
2. **What the microphone hears is not what the monitor records**: a filter
   chain or equaliser on the sink, the per-channel volume (96%/88%,
   balance -0.08: a linear gain, harmless by itself), or loudspeaker
   distortion. Test: the same calibration signal, coherence per band between
   microphone and monitor; the achievable linear cancellation (ERLE) is the
   coherent fraction.
3. Only after 1-2: a residual-echo post-filter guided by the measured ERLE,
   and the confidence-aware pause depth of §18.3 (limit the depth while the
   system plays instead of gating 24 dB on a coin flip).

The calibration signal of §14.3 ("Calibrate speakers") is the tool for 1 and
2, and it needs the speaker to be silent: it must be done with the phone
podcast off.

### Reproducing

```bash
# Record (GUI running with `make start`), play speech then music from the PC
build-aux/audio-lab/experiment.sh /tmp/exp speech.wav music.wav
# Reprocess the original with the current code and evaluate it
rec0 process ~/Videos/rec0/dev/NAME.original.mp4 -o /tmp/NAME.new.mp4
python3 build-aux/audio-lab/evaluate.py ~/Videos/rec0/dev/NAME.original.mp4 /tmp/NAME.new.mp4
# Canceller variants
python3 build-aux/audio-lab/aec_variants.py; python3 build-aux/audio-lab/stereo_ref.py
```
