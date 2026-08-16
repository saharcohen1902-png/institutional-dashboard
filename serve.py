"""Serve the dashboard on localhost.

    python3 serve.py [port]

`/refresh` triggers update.py in a background thread so the page can rebuild
itself without a terminal.
"""

import http.server
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "last": None}


def _run_update():
    try:
        proc = subprocess.run(
            [sys.executable, "update.py"],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        _refresh_state["last"] = {
            "ok": proc.returncode == 0,
            "tail": (proc.stdout or "")[-2000:],
        }
    except Exception as e:  # noqa: BLE001
        _refresh_state["last"] = {"ok": False, "tail": str(e)}
    finally:
        _refresh_state["running"] = False


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/refresh":
            with _refresh_lock:
                started = not _refresh_state["running"]
                if started:
                    _refresh_state["running"] = True
                    threading.Thread(target=_run_update, daemon=True).start()
            body = json.dumps(
                {"started": started, "running": _refresh_state["running"],
                 "last": _refresh_state["last"]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # data.json changes under the server; never let a browser cache it.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4200
    if not os.path.exists(os.path.join(WEB_DIR, "data.json")):
        print("data.json missing — run: python3 update.py", file=sys.stderr)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
