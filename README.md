# rec0

Registratore video **dichiarativo** per GNOME. Un file YAML descrive l'intera
sessione: apri il progetto, premi registra. Niente scene da costruire a mano come in
OBS: la composizione è predeterminata.

## Come funziona

Il video è sempre composto su un **desktop virtuale** (di default
`assets/background.jpg`, oppure un colore o un'immagine a scelta), sul quale rec0
mostra una di due scene:

| Scena | Quando | Cosa si vede |
|---|---|---|
| **Primo piano** | rec0 ha il focus, oppure qualsiasi finestra *non* elencata nel progetto | la webcam |
| **Condivisione** | ha il focus una delle finestre elencate in `windows` | la finestra, nella **stessa posizione e dimensione** che ha sullo schermo reale, più la webcam in un cerchio |

Il cambio di scena è automatico e segue il focus, con una breve transizione. Se
sposti o ridimensioni la finestra, nel video si sposta anche lei. Per le tab del
browser basta fare il match sul titolo: cambiando tab, cambia la scena.

Durante la registrazione, appena lasci la finestra di rec0, sullo schermo compare una
**bolla rotonda con la webcam**: sempre in primo piano, trascinabile, non prende mai il
focus. Nella scena di condivisione il cerchio della webcam nel video segue la bolla e la
copre esattamente, così la tua faccia non compare due volte.

### Privacy

Le finestre non elencate nel progetto non compaiono mai nel video. In più rec0 ha una
**privacy list** di siti che non vengono registrati nemmeno dentro una finestra
condivisa: posta (gmail.com, outlook, libero…), chat (WhatsApp, Telegram, Slack…),
password manager, PayPal e banche online. Se una tab privata va in focus:

- da primo piano, la scena **non passa** in condivisione finché non torni su una tab
  condivisibile;
- durante la condivisione, il livello schermo si **congela** sull'ultimo fotogramma
  sicuro. Webcam e audio continuano a essere registrati.

Il flusso dello schermo è ritardato di 0,4 s: nascondere e congelare sono immediati,
mentre mostrare nuovo contenuto avviene solo dopo che i fotogrammi catturati prima del
cambio sono stati scartati. Nemmeno un fotogramma della tab privata finisce nel video.

X11 espone il titolo delle finestre, non l'URL: ogni dominio è riconosciuto dal nome
o dalle parole che il sito mette nel titolo (es. "Gmail"). La lista si personalizza
nel progetto:

```yaml
privacy:
  allow: [app.slack.com]            # toglie voci dalla lista predefinita
  block:                            # aggiunge le tue
    - miabanca.example
    - {domain: intranet.example, titles: ["Intranet"]}
```

## Installazione

Dipendenze (Ubuntu):

```bash
sudo apt install python3-gi python3-gi-cairo python3-yaml gir1.2-gtk-4.0 gir1.2-gtk-3.0 \
  gir1.2-adw-1 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  gstreamer1.0-pipewire gstreamer1.0-x gettext
```

Poi:

```bash
make install      # installa per il tuo utente in ~/.local (senza root)
make uninstall    # rimuove tutto
```

rec0 compare nella panoramica Attività, apre i file `.yaml` dal file manager (Apri
con…) e si integra con GNOME: istanza singola, notifiche, progetti recenti, blocco
della sospensione durante la registrazione, cartella Video come destinazione.

Per le distribuzioni c'è il build system **Meson** (`meson setup _build && meson
install -C _build`) e un manifest **Flatpak** in `build-aux/flatpak/`.

## Uso

Interfaccia grafica: `rec0` oppure `rec0 progetto.yaml`.

| Scorciatoia | Azione |
|---|---|
| Ctrl+R | avvia/ferma la registrazione (con conto alla rovescia) |
| Ctrl+1 / 2 / 3 | scena automatica / primo piano / condivisione |
| Ctrl+O, Ctrl+N, Ctrl+E | apri, nuovo, modifica il file del progetto |
| Ctrl+, | preferenze |
| Ctrl+? | scorciatoie da tastiera |

Dalla barra in basso si scelgono **webcam e microfono**; la scelta viene ricordata e
vale al posto di quella del progetto.

Da terminale:

```bash
rec0 init tutorial              # crea tutorial.yaml commentato
rec0 devices                    # webcam, microfoni, monitor e finestre aperte
rec0 check tutorial.yaml        # valida il progetto e verifica i dispositivi
rec0 record tutorial.yaml       # registra senza GUI (Ctrl+C per fermare)
rec0 record tutorial.yaml -d 60 --scene camera --no-bubble
```

L'interfaccia è in inglese con traduzione italiana: segue la lingua del sistema
(`LANGUAGE=it rec0` per forzarla).

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

privacy:
  allow: []
  block: []

audio:
  microphone: default        # default, false, parte del nome del dispositivo
  desktop: false             # audio di sistema

launch:                      # applicazioni da avviare all'apertura
  - command: firefox https://docs.python.org/3/
  - command: gnome-terminal
    cwd: ~/Develop

output:
  directory: ~/Video/Tutorial          # default: cartella Video, sottocartella rec0
  filename: "{project}-{timestamp}.mp4"   # anche {date}, {format}
  format: mp4                # mp4 (H.264 + AAC) o mkv
```

Altri esempi in [`examples/`](examples/); `examples/test.yaml` usa solo sorgenti
sintetiche e funziona anche senza webcam.

## X11 e Wayland

- **X11**: tutto è automatico. rec0 cattura il monitor con `ximagesrc` e legge focus
  e geometria delle finestre direttamente da Xlib.
- **Wayland**: lo schermo si cattura tramite XDG Desktop Portal + PipeWire. La prima
  volta GNOME chiede quale schermo condividere; l'autorizzazione viene ricordata
  (`rec0 forget` per azzerarla). Wayland non permette alle applicazioni di sapere quale
  finestra ha il focus né dove si trova: lì le scene si cambiano a mano e la
  condivisione mostra l'intero schermo.

## Sviluppo

```bash
make start                      # avvia dal sorgente (profilo di sviluppo + API locale)
make api ARGS="state"           # pilota l'istanza in esecuzione
make test                       # test
make pot                        # aggiorna po/rec0.pot
```

`make start` usa l'ID `io.github.francescobianco.Rec0.Devel`, così convive con la
versione installata; la barra del titolo a strisce indica il profilo di sviluppo.

### API locale di sviluppo

Con `make start` rec0 espone un'API HTTP su `127.0.0.1` (porta e token casuali in
`$XDG_RUNTIME_DIR/rec0-devapi.json`), pensata per guidare l'app durante lo sviluppo e
per i test automatici. `build-aux/devctl.py` (o `make api ARGS=…`) è il client:

```bash
make api ARGS="state"                        # stato dell'app in JSON
make api ARGS="open examples/test.yaml"      # apre un progetto
make api ARGS="record start"                 # start | stop | toggle
make api ARGS="scene share"                  # auto | camera | share
make api ARGS="focus 'Docs - Google Chrome' google-chrome 100 100 1200 800"
make api ARGS="screenshot ui.png"            # PNG della sola finestra di rec0
make api ARGS="action app.preferences"       # attiva una qualsiasi azione
make api ARGS="eval 'result = win.get_title()'"
make api ARGS="log"
```

### Architettura

| Modulo | Ruolo |
|---|---|
| `project.py` | carica e valida lo YAML (tutti gli errori in una volta) |
| `privacy.py` | privacy list dei siti da non registrare |
| `capture.py` | webcam, microfoni, monitor, portale ScreenCast |
| `x11.py` | accesso diretto a Xlib (ctypes): finestra attiva, geometrie, monitor |
| `focus.py` | segue la finestra attiva |
| `scenes.py` | geometria delle scene e transizioni (funzioni pure) |
| `recorder.py` | pipeline GStreamer e `Director`, che anima le scene e applica la privacy |
| `bubble.py` | bolla con la webcam sullo schermo (processo GTK3 separato) |
| `app.py` | interfaccia GTK4 + Libadwaita |
| `devapi.py` | API locale di sviluppo |
| `cli.py` | comandi `rec0 …` |

La pipeline è sempre la stessa: `compositor` con sfondo, cattura del monitor (ritardata
e ritagliata sulla finestra attiva) e webcam. Le scene cambiano modificando a runtime le
proprietà dei pad del compositor, quindi senza interrompere la registrazione.
