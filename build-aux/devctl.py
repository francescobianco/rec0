#!/usr/bin/env python3
"""Client for the rec0 development API (see rec0/devapi.py).

    devctl.py state
    devctl.py log
    devctl.py screenshot [out.png]
    devctl.py open PROJECT.r0
    devctl.py record start|stop|toggle
    devctl.py scene auto|camera|share
    devctl.py focus "TITLE" [WM_CLASS] [x y w h]
    devctl.py action app.about [TARGET]
    devctl.py eval "result = win.get_title()"
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def info():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    path = Path(runtime) / "rec0-devapi.json"
    try:
        return json.loads(path.read_text())
    except OSError:
        sys.exit("rec0 is not running with the dev API (start it with `make start`)")


def request(method, route, body=None):
    i = info()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{i['port']}/{route}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {i['token']}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"{e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach rec0: {e.reason}")


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    if cmd in ("state", "log"):
        ctype, data = request("GET", cmd)
    elif cmd == "screenshot":
        out = Path(args[0] if args else "rec0-screenshot.png")
        _, data = request("GET", "screenshot")
        out.write_bytes(data)
        print(out.resolve())
        return
    elif cmd == "open":
        ctype, data = request("POST", "open", {"path": str(Path(args[0]).resolve())})
    elif cmd == "record":
        ctype, data = request("POST", "record", {"action": args[0] if args else "toggle", "countdown": 0})
    elif cmd == "scene":
        ctype, data = request("POST", "scene", {"mode": args[0]})
    elif cmd == "focus":
        body = {"title": args[0], "wm_class": args[1] if len(args) > 1 else ""}
        if len(args) >= 6:
            body["rect"] = [int(v) for v in args[2:6]]
        ctype, data = request("POST", "focus", body)
    elif cmd == "action":
        body = {"name": args[0]}
        if len(args) > 1:
            body["target"] = args[1]
        ctype, data = request("POST", "action", body)
    elif cmd == "eval":
        ctype, data = request("POST", "eval", {"code": args[0]})
    else:
        sys.exit(__doc__)
    print(data.decode())


if __name__ == "__main__":
    main(sys.argv[1:])
