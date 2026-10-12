# Audio lab

Scripts behind the measurements in `docs/tech/audio-processing.md` (§16-§20).
They are development tools, not part of the application.

| Script | What it does |
|---|---|
| `experiment.sh OUT SPEECH.wav MUSIC.wav` | records through the running GUI (`make start`) while the PC plays speech, then music; writes `timeline.txt` |
| `evaluate.py ORIGINAL.mp4 OUTPUT.mp4 [VOICE.wav] [timeline.txt]` | system-track fidelity, echo coherence, per-second and per-100 ms voice gain (stability), discontinuities, loudness |
| `clicks.py FILE…` | the largest sample-to-sample jumps of a file's audio, relative to its envelope |
| `aec_variants.py` | the canceller's total residual reduction with more taps, longer blocks, a drift-warped reference |
| `stereo_ref.py` | the canceller with the left and right system channels as separate references |
| `ltas.py ORIGINAL OUTPUT…` | long-term spectrum of each output relative to the microphone over all frames, both normalised to 300-3000 Hz (§20) |
| `spectrum.py ORIGINAL OUTPUT [track]` | third-octave spectrum of the loud (speech) frames of the output relative to the microphone, both normalised to 300-3000 Hz: what the chain does to the voice's balance (§20) |

The recordings they refer to are in `~/Videos/rec0/dev/` (not in the repository).
