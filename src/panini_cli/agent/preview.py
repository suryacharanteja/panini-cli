"""Bounded, authenticated loopback previews of built assets; no project code runs on the host."""

import asyncio
import hmac
import secrets
import stat
import threading
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..filesystem import read_bounded
from .network_proxy import BoundedHTTPServer
from .tools import is_secret

MAX_FILE_BYTES = 32 * 1024**2
MAX_PREVIEWS = 2
MIME_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json", ".map": "application/json", ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".wasm": "application/wasm",
}
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
       "connect-src 'self'; worker-src 'none'; object-src 'none'; frame-src 'none'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _parts(value):
    if not value or any(c in value for c in ("\\", ":", "\0", "%")):
        raise OSError("Invalid preview asset path.")
    parts = value.split("/")
    if any(not part or part.startswith(".") or is_secret(part) for part in parts):
        raise OSError("Preview paths cannot expose hidden files, secrets or traversal.")
    return parts


def _check_chain(workspace, target):
    try:
        parts = target.relative_to(workspace).parts
    except ValueError:
        raise OSError("Preview output must be inside the workspace.") from None
    current = workspace
    for part in parts:
        if part.startswith(".") or is_secret(part):
            raise OSError("Preview output cannot include hidden directories or recognized secrets.")
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("Preview output cannot include symlinks or directory junctions.")


def read_asset(workspace, root, relative):
    target = root.joinpath(*_parts(relative))
    _check_chain(workspace, target)
    kind = MIME_TYPES.get(target.suffix.lower())
    if kind is None:
        raise OSError("Unsupported preview asset type.")
    data = read_bounded(target, root, MAX_FILE_BYTES,
                        validate=lambda opened: _parts(opened.relative_to(root).as_posix()))
    return data, kind


class PreviewServer:
    def __init__(self, workspace, root):
        self.workspace, self.root = workspace, root
        self.token = secrets.token_urlsafe(32)
        self.cookie = "panini_preview_" + secrets.token_hex(8)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", kind="text/plain; charset=utf-8", *, headers=()):
                self.close_connection = True
                self.send_response(status)
                self.send_header("Connection", "close")
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Cross-Origin-Opener-Policy", "same-origin")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def authorize(self):
                if self.headers.get("Host") != f"127.0.0.1:{owner.server.server_port}":
                    self.reply(403)
                    return
                origin = self.headers.get("Origin")
                if (origin and origin != f"http://127.0.0.1:{owner.server.server_port}") or self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                    self.reply(403)
                    return
                try:
                    parsed = urlsplit(self.path)
                    valid_token = hmac.compare_digest(parsed.path, "/_panini/launch/" + owner.token)
                except (ValueError, TypeError):
                    self.reply(400)
                    return
                if parsed.scheme or parsed.netloc or len(self.path) > 8192:
                    self.reply(400)
                    return
                if valid_token:
                    if self.command != "GET":
                        self.reply(405)
                        return
                    self.reply(303, headers=(("Location", "/"),
                        ("Set-Cookie", f"{owner.cookie}={owner.token}; HttpOnly; SameSite=Strict; Path=/")))
                    return
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                    credential = cookies.get(owner.cookie)
                    authorized = credential is not None and hmac.compare_digest(credential.value, owner.token)
                except (ValueError, TypeError, CookieError):
                    authorized = False
                if not authorized:
                    self.reply(403)
                    return
                return parsed

            def do_GET(self):
                parsed = self.authorize()
                if parsed is None:
                    return
                try:
                    path = unquote(parsed.path, errors="strict").removeprefix("/") or "index.html"
                    parts = _parts(path)
                    try:
                        data, kind = read_asset(owner.workspace, owner.root, "/".join(parts))
                    except FileNotFoundError:
                        # SPA navigation falls back only for extensionless routes.
                        if Path(parts[-1]).suffix:
                            raise
                        data, kind = read_asset(owner.workspace, owner.root, "index.html")
                    self.reply(200, data, kind)
                except (OSError, UnicodeError, ValueError):
                    self.reply(404)

            do_HEAD = do_GET

        self.Handler = Handler
        self.server = BoundedHTTPServer(("127.0.0.1", 0), Handler, workers=16, timeout=5)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/_panini/launch/{self.token}"

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@dataclass
class Preview:
    id: str
    path: str
    server: PreviewServer
    policy: dict
    watchdog: asyncio.Task | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def info(self):
        return {"id": self.id, "path": self.path, "url": self.server.url,
                "mode": self.policy.get("previewMode", "built-assets"), "running": not self.closed.is_set()}


class Previews:
    def __init__(self, broker):
        self.broker = broker
        self.sessions = {}
        self.counter = 0
        self.lock = asyncio.Lock()
        self.terminals = None

    async def start(self, path="dist"):
        async with self.lock:
            return await self._start(path)

    async def _start(self, path):
        policy = self.broker.policy()
        if policy.backend != "mxc" or not policy.local_preview:
            raise OSError("Built-asset previews require --sandbox mxc --local-preview, or the explicit preview command.")
        if len(self.sessions) >= MAX_PREVIEWS:
            raise OSError(f"At most {MAX_PREVIEWS} previews can run at once; stop one first.")
        if not isinstance(path, str) or not path.strip():
            raise OSError("A built output directory is required.")
        candidate = Path(path)
        root = candidate if candidate.is_absolute() else policy.workspace / candidate
        _check_chain(policy.workspace, root)
        root = root.resolve(strict=True)
        if root == policy.workspace or not root.is_relative_to(policy.workspace) or not root.is_dir():
            raise OSError("Preview a built subdirectory such as dist, not the workspace root.")
        await asyncio.to_thread(read_asset, policy.workspace, root, "index.html")
        authorization = self.broker.authorization()
        server = PreviewServer(policy.workspace, root).start()
        self.counter += 1
        preview = Preview(f"p{self.counter}", root.relative_to(policy.workspace).as_posix(), server,
                          {**authorization, "previewMode": "built-assets", "previewServing": "read-only"})
        self.sessions[preview.id] = preview
        preview.watchdog = asyncio.create_task(self._expire(preview.id))
        return preview

    async def _expire(self, preview_id):
        await asyncio.sleep(max(1, min(self.broker.settings.terminal_timeout, 86400)))
        await self.stop(preview_id)

    def list(self):
        return [preview.info() for preview in self.sessions.values()]

    async def stop(self, preview_id):
        try:
            preview = self.sessions.pop(preview_id)
        except KeyError:
            raise OSError(f"No preview session {preview_id}.") from None
        if preview.watchdog is not asyncio.current_task():
            preview.watchdog.cancel()
        try:
            await asyncio.to_thread(preview.server.close)
        finally:
            terminal_id = preview.policy.get("terminalId")
            if terminal_id and self.terminals:
                await self.terminals.stop(terminal_id)
            preview.closed.set()

    async def close(self):
        for preview_id in list(self.sessions):
            await self.stop(preview_id)
