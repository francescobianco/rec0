# Changelog

All notable changes to rec0 are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Scenes

- Automatic scene switching on Wayland: a GNOME Shell extension, installed
  with rec0, tells it which window has the focus. Scenes follow the focus and
  private pages freeze the screen as on X11 (`make shell-extension` installs
  and enables it from the source tree).
- Windows recorded one by one on Wayland too: with version 2 of the GNOME
  Shell extension each shared window is captured on its own through Mutter's
  screencast API (no dialog, no whole desktop, rec0's own window never shows),
  followed as it moves, resizes or closes. With the first version of the
  extension rec0 falls back to the whole screen.
- Wayland window streams no longer carry the pointer: Mutter drew it onto
  recycled buffers holding older frames, so windows flickered back in time
  and pointers showed twice. The pointer is not drawn on Wayland for now.
- The preview shows the webcam where the on-screen bubble is, or will appear
  when recording starts, instead of the configured overlay position.
- On Wayland a window's class is its app id: `match: gnome-terminal` now
  also matches `org.gnome.Terminal` (separators are ignored), and privacy
  rules recognise Flatpak and Wayland ids such as `org.mozilla.Thunderbird`.
- When the whole screen is shared (Wayland) and its shape differs from the
  video's (a 16:10 laptop in a 16:9 video), it is framed with rounded corners
  like a shared window, between the bands of background.
- The webcam bubble can be resized: hovering it shows a handle in the
  bottom-right corner, or on the opposite side when the bubble is near the
  right or bottom edge of the screen. The video overlay follows its size, and
  the size is saved in the project (`camera.bubble.size`).
- Back from a private page to a shareable one, the window plays again: it
  stayed frozen on its last frame until it was closed.

### Audio

- Pauses are lowered with sample-continuous fades. The previous envelope was a
  volume step every 21 ms frame, an audible click at every transition: on a
  real recording the jump between two samples went from 0.008 before that
  stage to 0.122 after it.
- Loudness normalisation is a measured constant gain followed by the true-peak
  limiter: `loudnorm linear=true` silently fell back to dynamic gain riding when
  the loudness range or the peaks exceeded its limits.
- The denoise check compares the same chain with and without the denoiser on
  sample-aligned frames; the reduced RNNoise mix and the spectral fallback are
  verified too, and denoising is bypassed when even the fallback takes more
  than 3 dB of voice.
- Filter delays are compensated right after each denoiser, with the buffered
  tail padded in; the limiter's lookahead is undone at its internal rate.
- The echo-cancelled voice is mono: its stereo duplication is now at full level
  and loudness is measured after it.
- Technical dossier: circuit audit, measurement and listening protocol,
  improvement strategy (`docs/tech/audio-processing.md`).

## [0.1.0] - 2026-10-09

First public release.

### Recording

- Declarative projects: one YAML file (`.r0`, with its own MIME type) describes
  the webcam, the windows to share, audio and output.
- Two automatic scenes composed on a virtual desktop: a webcam **close-up**
  while rec0 or any unlisted window has focus, and **share** when a listed
  window does, shown at its real on-screen position and size inside a rounded,
  accent-coloured frame.
- Each shared window is captured on its own; presented windows stay on the
  virtual desktop when focus moves on, and fullscreen windows fill the video.
- Webcam bubble on screen while recording: always on top, draggable, never
  takes focus; in the video the webcam overlay follows it exactly. Double
  click brings rec0 forward.
- Countdown that runs the real pipeline (warm-up, nothing written), a
  frame-exact start with a preview-only flash, and a short CRT power-off as
  closing, recorded over one extra second after stop with the audio fading out.
- Webcam mirrored by default.

### Privacy

- Built-in privacy list: web mail, chats, password managers and banks are
  never recorded, recognised by window title; desktop messaging, mail and
  password apps are recognised by window class.
- The screen stream is delayed by 0.4 s, so hiding or freezing a window
  always happens before any frame of a private page reaches the video.
- Per-project `privacy.allow` / `privacy.block` lists.

### Audio

- Adaptive post-processing for YouTube-ready voice: it measures the recording
  and switches on only what is needed (declip, high-pass, de-hum, RNNoise
  denoise with feedback control, expander, leveler, EQ, de-esser, compressor,
  two-pass EBU R128 loudness with a true-peak limiter).
- Voice activity detection drives the processing.
- Multitrack: microphone and system sound are recorded on separate tracks; only
  the voice is processed, and system sound is laid over it untouched.
- Speaker echo canceller: with speakers instead of headphones, what they play
  is estimated from the system sound track (room reflections and clock drift
  included) and removed from the microphone before processing.
- Stretches where the microphone held only the speakers' echo count as pauses,
  so the voice processor never raises what the speakers played.
- Every filter's delay is compensated, so the processed voice stays in sync
  with the picture and with the system sound.
- The original file is kept next to the result; `rec0 process` works on any
  existing video.

### Interface

- GTK4 + Libadwaita app: live preview matching the video aspect ratio, webcam
  and microphone pickers, audio optimization toggle saved in the project,
  native toasts, keyboard shortcuts, GNOME integration (single instance,
  notifications, recent projects, suspend inhibition, Videos folder).
- Command line: `rec0 init | check | record | process | devices | pipeline | forget`.
- English interface with Italian translation.

### Performance

- H.264 encoding and webcam MJPEG decoding on the GPU through VA-API when
  available, with automatic fallback to software (`REC0_NO_HW=1` forces it).
- The preview alone is composed at its own size and each window is shrunk
  right after capture; recordings stay at full resolution.

### Fixed

- Recordings no longer stop when rec0 goes to the background (the preview's
  lower frame rate failed to negotiate and stopped the whole pipeline).
- System sound no longer sounds like a room echo: the speakers' copy picked up
  by the microphone was raised with the voice and laid ~45 ms late over it.
- The mouse pointer is drawn by rec0 itself: ximagesrc darkened its edges,
  missed window moves and drew a hidden pointer as a grey square.
- In Matroska files the audio is no longer 21 ms late on the video (AAC
  priming, which that container does not record).
- Audio clicks from the live mixer, and a webcam captured at bubble size.

[Unreleased]: https://github.com/francescobianco/rec0/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/francescobianco/rec0/releases/tag/v0.1.0
