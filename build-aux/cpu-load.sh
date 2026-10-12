#!/bin/bash
# CPU of rec0 and the X server while recording, as windows are presented one by one.
#   build-aux/cpu-load.sh XID [XID…]      (window ids from `xprop -root _NET_CLIENT_LIST`)
# Starts the GUI (`make start`, dev API), records for the duration of the test, prints the
# mean %CPU of rec0 and Xorg in each state and rec0's busiest threads (GStreamer names
# streaming threads after their elements: mix = compositor, queueN = the chain after it),
# then stops the recording and the GUI. Each state is sampled for 6 s.
cd "$(dirname "$0")/.." || exit 1
make start > /tmp/rec0-load.log 2>&1 &
for _ in $(seq 1 40); do python3 build-aux/devctl.py state >/dev/null 2>&1 && break; sleep 1; done
PID=$(pgrep -f "^/usr/bin/python3 -m rec0 " | head -1); XP=$(pidof Xorg | cut -d' ' -f1)
sample() { top -b -d 1 -n 6 -p "$PID,$XP" | awk -v l="$1" '$1 ~ /^[0-9]+$/ {cpu[$1]+=$9; n[$1]++} END {for (k in cpu) printf "%-36s pid %-8s %5.0f%%\n", l, k, cpu[k]/n[k]}'; }
threads() { top -H -b -n 2 -d 2 -p "$PID" | awk '/^ *PID/ {f++} f==2 && $1 ~ /^[0-9]+$/ {print $9, $NF}' | sort -nr | head -10 | tr '\n' ';'; echo; }
state() { python3 build-aux/devctl.py eval "d = win.director; result = dict(scene=d.scene, live=sorted(win.recorder.sources), dormant=sorted(d.dormant))" | tr -d '\n ' ; echo; }
sample "preview"
python3 build-aux/devctl.py record start >/dev/null; sleep 4
sample "recording"
for x in "$@"; do
  python3 build-aux/devctl.py eval "from rec0 import x11; win.director.focus_changed(x11.window_info($x)); result = 1" >/dev/null
  sleep 3; state; sample "+ window $x"
done
echo "threads: $(threads)"
python3 build-aux/devctl.py record stop >/dev/null; sleep 10
pkill -f "^/usr/bin/python3 -m rec0 "
grep -i "error" /tmp/rec0-load.log | head -3
