For a program like this, on Ubuntu with GNOME, I would pick Python 3 + GTK4 + GStreamer, with a YAML file to configure each recording project.

I would not use Electron, I would not write a video engine from scratch and, at least at first, I would avoid Qt as well.

The idea I like is to build a declarative video recorder: you describe what you want to record and how the sources should be laid out, and the program takes care of everything else.

## 1. The stack I would choose

| Component                          | Technology                    |
| ---------------------------------- | ----------------------------- |
| Language                           | Python 3                      |
| Graphical interface                | GTK4 + Libadwaita             |
| Video recording and composition    | GStreamer                     |
| Desktop capture                    | PipeWire + XDG Desktop Portal |
| Webcam                             | Video4Linux2 / PipeWire       |
| Audio                              | PipeWire                      |
| Configuration                      | YAML                          |
| Output                             | MP4 (H.264 + AAC)             |
| Development environment            | VS Code, or Neovim            |

Why Python? Because the value of this program is not in video encoding, but in orchestrating the sources, managing the configuration and keeping the interface simple. Python is perfect for that.

Why GStreamer? Because you can build pipelines that capture several audio/video streams, compose them, keep them in sync and produce a single file.

And GTK4 gives you an application that is truly integrated into GNOME, without dragging along a heavy graphical framework.

## 2. How I picture the program

```
Recorder — tutorial-python.yaml

Ready

Browser
Main window
Webcam
Terminal

Microphone on

00:00:00

REC
```

A conceptual sketch of the interface: a preview, the current project, an audio meter and a record button.

No dozens of controls like OBS. Only what is needed to quickly produce a video ready to upload to YouTube.

## 3. The heart would be the YAML file

I imagine something like this:

```
project: tutorial-python

video:
  resolution: 1920x1080
  fps: 30

audio:
  microphone: default
  desktop: false

sources:
  - id: browser
    type: window
    match: "Firefox"
    layout: fullscreen

  - id: webcam
    type: camera
    device: default
    position: bottom-right
    width: 320
    height: 180

output:
  directory: ~/Videos/Tutorial
  filename: "{project}-{timestamp}.mp4"
  format: mp4
```

Open the project, press REC and record.

The YAML configuration could also describe applications to start automatically, for example Firefox with a URL, or a terminal in the project directory.

I would keep two concepts apart:

- Sources: what is captured.
- Layout: how the sources are composed in the final video.

That way you could have several layouts without redefining the devices.

## 4. The main technical problem: Wayland

On a modern Ubuntu GNOME, the most delicate part is capturing individual windows.

Wayland does not let an arbitrary application freely capture the windows of other applications. Capture normally goes through the XDG Desktop Portal and PipeWire, and may require the user to explicitly select the windows that are allowed.

This means that a YAML entry like `match: "Firefox"` can identify the desired source, but does not guarantee that it can be captured automatically without interaction.

So I would plan an initial step that associates the sources, reusing the permissions when the desktop allows it.

## 5. Minimal architecture

I would write four Python modules:

- `project.py`: loads and validates the YAML.
- `capture.py`: manages sources and permissions.
- `recorder.py`: builds the GStreamer pipelines and records.
- `app.py`: provides the GTK interface.

The advantage is that the engine could also work without a graphical interface, with commands like:

```
recorder check tutorial.yaml
recorder record tutorial.yaml
```

## 6. A possibly even faster alternative

If the goal were an extremely robust prototype, I would also consider Python + OBS WebSocket, leaving all the composition and recording work to OBS.

Your program would then simply be a YAML controller with a minimal interface. OBS already has an engine to handle audio and video sources.

However, for a standalone, lightweight application that is well integrated into Ubuntu, I would prefer GTK4 + GStreamer.

One design choice I would make right away: the program should be neither a video editor nor an OBS clone. It should be a project-based video recorder, where a YAML file defines the whole recording session.

There is, however, one important architectural question for you: do you want to record several windows and the webcam at the same time, composing them into a single video, or record the sources separately and compose them later?
