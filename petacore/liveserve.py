"""A small, self-contained live-reload static file server.

Run as ``python3 -m petacore.liveserve <root> [--port N] [--host H]``.
This is what the Live Server page spawns: a static file server, in the
tradition of the well-known editor extension of the same name, not a
build tool. It serves the project's files exactly as they are and reloads
the browser automatically when one of them changes — no bundler, no
config, nothing to install.

Kept to the standard library on purpose, since it has to run on whatever
machine Petacore itself runs on, with nothing extra to set up first.
"""

import argparse
import functools
import http.server
import os
import re
import socket
import socketserver
import sys
import threading
import time

RELOAD_PATH = "/__petacore_reload__"
POLL_MS = 700
DEFAULT_PORT = 5500

# Left out of the change-tracking walk: irrelevant to what a browser is
# showing, and often the largest, slowest part of the tree to walk.
IGNORED_DIRS = {".git", ".petacore", ".petacore-trash", "node_modules",
                "__pycache__", ".venv", "venv", "dist", "build",
                ".idea", ".vscode", ".mypy_cache"}

_RELOAD_SNIPPET = (
    "<script>(function(){{var k={token!r};setInterval(function(){{"
    "fetch({path!r}).then(function(r){{return r.text();}})"
    ".then(function(t){{var c=parseInt(t,10);"
    "if(!isNaN(c)&&c!==k){{location.reload();}}}})"
    ".catch(function(){{}});}},{interval});}})();</script>"
)


class ChangeTracker:
    """A cheap 'has anything under root changed' token, kept current on a
    timer rather than recomputed on every poll — a reload check must never
    cost a full directory walk, or every open tab pays for it constantly.
    """

    def __init__(self, root: str, interval: float = 1.0):
        self.root = root
        self.interval = interval
        self.token = self._scan()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _scan(self) -> int:
        newest = 0.0
        for base, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs
                       if d not in IGNORED_DIRS and not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                try:
                    mtime = os.path.getmtime(os.path.join(base, name))
                except OSError:
                    continue
                if mtime > newest:
                    newest = mtime
        return int(newest * 1000)

    def _loop(self):
        while not self._stop:
            time.sleep(self.interval)
            try:
                self.token = self._scan()
            except OSError:
                pass

    def stop(self):
        self._stop = True


class ReloadingHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler, plus a reload endpoint and one small
    rewrite: an HTML page gets the reload snippet appended on its way out.
    Every other request — other file types, directory listings, 404s — is
    left entirely to the base class, so nothing about ordinary static
    serving is reimplemented here.
    """

    tracker: ChangeTracker = None    # set by serve() before requests arrive
    server_version = "PetacoreLiveServer/1"

    def log_message(self, fmt, *args):
        # Petacore's own console prefixes and timestamps this already;
        # the default format is noise on top of that.
        sys.stdout.write("  " + (fmt % args) + "\n")
        sys.stdout.flush()

    def do_GET(self):
        request_path = self.path.split("?", 1)[0]
        if request_path == RELOAD_PATH:
            self._reply_token()
            return

        local_path = self.translate_path(request_path)

        if os.path.isdir(local_path):
            # "/" (or any folder) implicitly serves its index.html — that
            # has to get the reload snippet too, or the address everyone
            # actually types never reloads.
            for index_name in ("index.html", "index.htm"):
                candidate = os.path.join(local_path, index_name)
                if os.path.isfile(candidate):
                    if not request_path.endswith("/"):
                        # same trailing-slash redirect the base class would
                        # do, so the page's relative links still resolve
                        self.send_response(301)
                        self.send_header("Location", request_path + "/")
                        self.end_headers()
                        return
                    self._serve_html(candidate)
                    return
            super().do_GET()
            return

        if local_path.lower().endswith((".html", ".htm")) \
                and os.path.isfile(local_path):
            self._serve_html(local_path)
            return

        super().do_GET()

    def _reply_token(self):
        body = str(self.tracker.token).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self, local_path):
        try:
            with open(local_path, "rb") as f:
                raw = f.read()
        except OSError:
            self.send_error(404, "File not found")
            return
        text = raw.decode("utf-8", "replace")
        snippet = _RELOAD_SNIPPET.format(
            token=self.tracker.token, path=RELOAD_PATH, interval=POLL_MS)
        if re.search(r"</body\s*>", text, re.IGNORECASE):
            text = re.sub(r"</body\s*>", snippet + "</body>", text,
                          count=1, flags=re.IGNORECASE)
        else:
            text += snippet
        encoded = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_free_port(preferred: int = DEFAULT_PORT, host: str = "127.0.0.1",
                   tries: int = 40) -> int:
    """The preferred port if it is free, otherwise the next ones in turn.

    Checked against the exact host Live Server will bind to — a port can be
    free on 127.0.0.1 and not on 0.0.0.0, or the reverse.
    """
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def lan_address():
    """This machine's address on the local network, or None off-network.

    Opens no connection — UDP has nothing to connect to at 8.8.8.8, this
    only asks the routing table which local address would be used.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def serve(root: str, port: int = DEFAULT_PORT, host: str = "127.0.0.1"):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)

    tracker = ChangeTracker(root)
    handler = functools.partial(ReloadingHandler, directory=root)
    handler.tracker = tracker
    # the class attribute above is what request instances actually read
    ReloadingHandler.tracker = tracker

    httpd = _Server((host, port), handler)
    bound_port = httpd.server_address[1]
    print(f"Serving {root}")
    print(f"  Local:   http://127.0.0.1:{bound_port}/")
    if host == "0.0.0.0":
        lan = lan_address()
        if lan:
            print(f"  Network: http://{lan}:{bound_port}/")
    sys.stdout.flush()

    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Petacore live-reload static file server")
    parser.add_argument("root", help="folder to serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"not a directory: {args.root}", file=sys.stderr)
        sys.exit(1)
    serve(args.root, args.port, args.host)


if __name__ == "__main__":
    main()
