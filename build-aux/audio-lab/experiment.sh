#!/bin/bash
# Real recording experiment through the running rec0 GUI (`make start`, dev API):
# a podcast on a phone next to the microphone is the "voice"; the PC plays speech,
# then music, through the speakers. Writes timeline.txt (seconds from the start request).
#
#   experiment.sh OUT_DIR SPEECH.wav MUSIC.wav
set -u
cd "$(dirname "$0")/../.."
OUT=${1:?out dir}; SPEECH=${2:?speech wav}; MUSIC=${3:?music wav}
CTL="python3 build-aux/devctl.py"
mkdir -p "$OUT"; T0=$(date +%s.%N)
mark() { printf '%s %.2f\n' "$1" "$(echo "$(date +%s.%N) - $T0" | bc)" >> "$OUT/timeline.txt"; }
: > "$OUT/timeline.txt"
$CTL record start > /dev/null; mark start_requested
sleep 8; mark pc_speech_on
paplay --volume=65536 "$SPEECH"; mark pc_speech_off
sleep 5; mark pc_music_on
paplay --volume=32768 "$MUSIC"; mark pc_music_off
sleep 7; mark stop_requested
$CTL record stop > /dev/null
for i in $(seq 1 120); do
  sleep 2
  read -r rec proc recording < <($CTL state | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['last_recording'], d['processing'], d['recording'])")
  [ "$proc" = "None" ] && [ "$recording" = "False" ] && { echo "saved: $rec"; break; }
done
$CTL state | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['last_audio_report'], indent=1))"
cat "$OUT/timeline.txt"
