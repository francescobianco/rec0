# rec0

Registratore video **dichiarativo** per Ubuntu/GNOME. Un file YAML descrive l'intera
sessione: apri il progetto, premi **REC**, registri. Niente scene da costruire a mano
come in OBS: la composizione è predeterminata.

## Come funziona

Il video è sempre composto su un **desktop virtuale** (di default
`assets/background.jpg`, oppure un colore o un'immagine a scelta), sul quale rec0
mostra una di due scene:

| Scena | Quando | Cosa si vede |
|---|---|---|
| **Primo piano** | rec0 ha il focus, oppure qualsiasi finestra *non* elencata nel progetto | la webcam |
| **Condivisione** | ha il focus una delle finestre elencate in `windows` | la finestra, nella **stessa posizione e dimensione** che ha sullo schermo reale, più la webcam in un cerchio |

Durante la registrazione, appena lasci la finestra di rec0, sullo schermo compare una
**bolla rotonda con la webcam**: sempre in primo piano, trascinabile, non prende mai il
focus. Nella scena di condivisione il cerchio della webcam nel video segue la bolla e la
copre esattamente, così la tua faccia non compare due volte.

Il cambio di scena è automatico e segue il focus, con una breve transizione. Se
sposti o ridimensioni la finestra, nel video si sposta anche lei. Per le tab del
browser basta fare il match sul titolo: cambiando tab, cambia la scena.

Le finestre non elencate non compaiono mai nel video: se passi a Slack o alla posta,
il video torna sul primo piano.

## Requisiti

```bash
sudo apt install python3-gi python3-yaml gir1.2-gtk-4.0 gir1.2-adw-1 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pipewire \
  gstreamer1.0-x x11-utils x11-xserver-utils
```

## Installazione

```bash
pipx install --system-site-packages .   # oppure: python3 -m rec0 ...
```

## Uso

```bash
rec0 init tutorial              # crea tutorial.yaml commentato
rec0 devices                    # webcam, microfoni, monitor e finestre aperte
rec0 check tutorial.yaml        # valida il progetto e verifica i dispositivi
rec0 tutorial.yaml              # interfaccia grafica
rec0 record tutorial.yaml       # registra da terminale (Ctrl+C per fermare)
rec0 record tutorial.yaml -d 60 --scene camera --no-bubble
rec0 pipeline tutorial.yaml     # stampa la pipeline GStreamer (debug)
```

Nella GUI: **Ctrl+R** avvia/ferma la registrazione, **Ctrl+O** apre un progetto. I
pulsanti *Automatica / Primo piano / Condivisione* permettono di forzare una scena.
Il progetto viene ricaricato automaticamente quando salvi il file YAML.

## Il file di progetto

```yaml
project: tutorial-python

video:
  resolution: 1920x1080      # oppure 720p, 1080p, 1440p, 4k
  fps: 30
  transition: 0.3            # secondi

background: sfondo.jpg       # colore (#RRGGBB) o immagine; default assets/background.jpg

camera:
  device: default            # default, /dev/videoN, parte del nome, "test"
  closeup: fullscreen        # oppure {position: center, width: 70%}
  overlay:                   # durante la condivisione; false per nasconderla
    position: bottom-right   # top-left, top, top-right, left, center, right, bottom-*
    width: 240               # pixel o percentuale
    shape: circle            # circle o rect (16:9)
    fit: cover               # cover (ritaglia), contain, stretch
  bubble:                    # bolla sullo schermo durante la registrazione; false per toglierla
    size: 200
    position: bottom-right   # posizione iniziale, poi la trascini dove vuoi

screen:
  monitor: primary           # primary, indice o nome (HDMI-1)
  margin: 40                 # bordo di desktop virtuale attorno allo schermo reale

windows:                     # match su titolo o classe, senza maiuscole/minuscole
  - match: Firefox
  - match: "Python 3 documentation"
  - match: gnome-terminal

audio:
  microphone: default        # default, false, parte del nome del dispositivo
  desktop: false             # audio di sistema

launch:                      # applicazioni da avviare all'apertura
  - command: firefox https://docs.python.org/3/
  - command: gnome-terminal
    cwd: ~/Develop

output:
  directory: ~/Videos/Tutorial
  filename: "{project}-{timestamp}.mp4"   # anche {date}, {format}
  format: mp4                # mp4 (H.264 + AAC) o mkv
```

Altri esempi in [`examples/`](examples/); `examples/test.yaml` usa solo sorgenti
sintetiche e funziona anche senza webcam.

## X11 e Wayland

- **X11**: tutto è automatico. rec0 cattura il monitor con `ximagesrc` e legge
  focus e geometria delle finestre con `xprop`/`xwininfo`.
- **Wayland**: lo schermo si cattura tramite XDG Desktop Portal + PipeWire. La prima
  volta GNOME chiede quale schermo condividere; l'autorizzazione viene ricordata
  (`rec0 forget` per azzerarla). Wayland non permette alle applicazioni di sapere quale
  finestra ha il focus, né dove si trova: lì le scene si cambiano con i pulsanti della
  GUI (o con `--scene`) e la condivisione mostra l'intero schermo.

## Architettura

| Modulo | Ruolo |
|---|---|
| `project.py` | carica e valida lo YAML (tutti gli errori in una volta) |
| `capture.py` | webcam, microfoni, monitor, portale ScreenCast |
| `focus.py` | segue la finestra attiva (X11) |
| `bubble.py` | bolla con la webcam sullo schermo (processo GTK3 separato) |
| `scenes.py` | geometria delle scene e transizioni (funzioni pure) |
| `recorder.py` | pipeline GStreamer e `Director`, che anima i cambi di scena |
| `app.py` | interfaccia GTK4 + Libadwaita |
| `cli.py` | comandi `rec0 …` |

La pipeline è sempre la stessa: `compositor` con sfondo, cattura del monitor ritagliata
(`videocrop`) sulla finestra attiva, e webcam. Le scene cambiano modificando a runtime
le proprietà dei pad del compositor, quindi senza interrompere la registrazione.

## Test

```bash
python3 -m pytest
```
