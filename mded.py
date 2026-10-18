#!/usr/bin/env python3
"""mded: edit markdown while looking at the rendered page.

Serves a local editor for the .md files in DIR. Blocks render through pandoc
with the site's stylesheet; click a block to edit its markdown. A configurable
build command produces the "official" html.
"""

import argparse
import functools
import html
import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

args = None
render_cache = {}
render_pool = ThreadPoolExecutor(max_workers=8)
build_lock = threading.Lock()


def git_root(path):
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def inside(base, path):
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    return path == base or path.startswith(base + os.sep)


def render(src):
    if src in render_cache:
        return render_cache[src]
    proc = subprocess.run(["pandoc", "-f", args.pandoc_from, "-t", "html", "--wrap=preserve"],
                          input=src, capture_output=True, text=True)
    if proc.returncode != 0:
        return '<pre class="mded-error">' + html.escape(proc.stderr, quote=False) + "</pre>"
    render_cache[src] = proc.stdout
    return proc.stdout


def newest_first(names):
    return sorted(names, key=lambda f: os.path.getmtime(os.path.join(args.dir, f)), reverse=True)


def md_files():
    return newest_first(f for f in os.listdir(args.dir) if f.endswith(".md") and not f.startswith("."))


def mtime_of(path):
    """The file's version token, compared as an exact string by the client."""
    return str(os.stat(path).st_mtime_ns)


def read_md(path):
    with open(path, encoding="utf-8") as f:
        return {"text": f.read(), "mtime": mtime_of(path)}


def md_path(name):
    if not name or "/" in name or not name.endswith(".md"):
        raise ValueError("bad file name")
    path = os.path.join(args.dir, name)
    if not inside(args.dir, path):
        raise ValueError("bad file name")
    return path


def style_blocks(page):
    head = page.split("</head>", 1)[0]
    return "\n".join(re.findall(r"<style[^>]*>.*?</style>", head, re.DOTALL))


def built_css():
    """The <style> of the newest page the build already produced that has
    highlighted code, so the editor gets exactly the official CSS."""
    pages = newest_first(f for f in os.listdir(args.dir)
                         if f.endswith(".html") and os.path.exists(os.path.join(args.dir, f[:-5] + ".md")))
    for f in pages:
        with open(os.path.join(args.dir, f), encoding="utf-8", errors="replace") as fh:
            style = style_blocks(fh.read())
        if "code span.kw" in style:
            return style
    return ""


def pandoc_template_css():
    """The <style> pandoc's standalone template adds (body width, margins,
    syntax highlighting). The code sample makes pandoc include the latter."""
    proc = subprocess.run(["pandoc", "-s", "-t", "html", "--metadata", "pagetitle=x"],
                          input="```python\nx = 1\n```\n", capture_output=True, text=True)
    return style_blocks(proc.stdout)


@functools.cache
def page_style():
    style = built_css() if args.built_css else ""
    if not style:
        style = pandoc_template_css() if args.pandoc_css else ""
        if args.style:
            with open(args.style) as f:
                style += f.read()
    return style


def config():
    rel_dir = os.path.relpath(args.dir, args.root)
    return {
        "dir": args.dir,
        "base": "/files/" + ("" if rel_dir == "." else quote(rel_dir) + "/"),
        "style": page_style(),
        "build": args.build,
        "files": md_files(),
    }


def run_build(name):
    cmd = args.build.replace("{file}", shlex.quote(name or ""))
    with build_lock:
        try:
            proc = subprocess.run(cmd, shell=True, cwd=args.dir, capture_output=True,
                                  text=True, timeout=600)
            return {"code": proc.returncode, "output": proc.stdout + proc.stderr, "cmd": cmd}
        except subprocess.TimeoutExpired as e:
            return {"code": -1, "output": f"timed out\n{e.stdout or ''}{e.stderr or ''}", "cmd": cmd}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if args.verbose:
            super().log_message(fmt, *a)

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                with open(os.path.join(HERE, "editor.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/api/config":
                return self.send(200, config())
            if url.path == "/api/file":
                return self.send(200, read_md(md_path(q.get("name", [""])[0])))
            if url.path == "/api/watch":
                return self.watch(md_path(q.get("name", [""])[0]), q.get("mtime", [""])[0])
            if url.path.startswith("/files/"):
                path = os.path.join(args.root, unquote(url.path[len("/files/"):]))
                if not inside(args.root, path):
                    return self.send(403, {"error": "outside root"})
                if os.path.isdir(path):
                    path = os.path.join(path, "index.html")
                if not os.path.isfile(path):
                    return self.send(404, {"error": "not found"})
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                with open(path, "rb") as f:
                    return self.send(200, f.read(), ctype)
            self.send(404, {"error": "not found"})
        except (ValueError, OSError) as e:
            self.send(400, {"error": str(e)})

    def watch(self, path, last):
        """Server-sent events: push the file's text whenever its mtime moves
        away from `last`. Runs until the browser disconnects."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        idle = 0.0
        try:
            while True:
                try:
                    now = mtime_of(path)
                    if now != last:
                        msg = read_md(path)
                except OSError:
                    now, msg = "", {"deleted": True}
                if now != last:
                    last = now
                    self.wfile.write(b"data: " + json.dumps(msg).encode() + b"\n\n")
                    self.wfile.flush()
                    idle = 0.0
                elif idle >= 15:
                    self.wfile.write(b": ping\n\n")  # notices closed tabs
                    self.wfile.flush()
                    idle = 0.0
                time.sleep(0.3)
                idle += 0.3
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        # A custom header forces a CORS preflight we never answer, so other
        # sites open in the browser can't drive this server.
        if self.headers.get("X-Mded") != "1":
            return self.send(403, {"error": "missing X-Mded header"})
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/render":
                return self.send(200, {"html": list(render_pool.map(render, data["blocks"]))})
            if url.path == "/api/save":
                path = md_path(data["name"])
                exists = os.path.exists(path)
                if data.get("create") and exists:
                    return self.send(409, {"error": "file already exists"})
                if not data.get("create") and not data.get("force"):
                    if not exists or mtime_of(path) != data.get("mtime"):
                        return self.send(409, {"error": "file changed on disk"})
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data["text"])
                return self.send(200, {"mtime": mtime_of(path), "files": md_files()})
            if url.path == "/api/build":
                if not args.build:
                    return self.send(400, {"error": "no build command configured (--build)"})
                return self.send(200, run_build(data.get("name")))
            self.send(404, {"error": "not found"})
        except (ValueError, KeyError, OSError) as e:
            self.send(400, {"error": str(e)})


def main():
    global args
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dir", help="directory containing the .md files")
    p.add_argument("--root", help="directory served for images/links (default: git root of DIR, else DIR)")
    p.add_argument("--style", help="html file with <style> to inject (e.g. pandoc -H header); used when no built page has the CSS")
    p.add_argument("--build", default="", help="shell command run in DIR to build; {file} is replaced by the current .md name")
    p.add_argument("--pandoc-from", default="markdown-smart", help="pandoc input format (default: markdown-smart)")
    p.add_argument("--no-built-css", dest="built_css", action="store_false",
                   help="don't take CSS from already-built .html pages in DIR")
    p.add_argument("--no-pandoc-css", dest="pandoc_css", action="store_false",
                   help="don't include the CSS from pandoc's standalone template")
    p.add_argument("--port", type=int, default=8123)
    p.add_argument("--no-open", action="store_true", help="don't open a browser")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    args.dir = os.path.realpath(os.path.expanduser(args.dir))
    args.root = os.path.realpath(os.path.expanduser(args.root or git_root(args.dir) or args.dir))
    if args.style:
        args.style = os.path.realpath(os.path.join(args.dir, os.path.expanduser(args.style)))
    if not inside(args.root, args.dir):
        sys.exit("DIR must be inside --root")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"mded: editing {args.dir}\n      {url}", flush=True)
    if not args.no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
