"""HTTP/WebSocket previews through sandbox pipes, with no host network ingress.

Node's HTTP server gets virtual sockets from a preload adapter. Project HTTP and
upgrade handlers still run in MXC. The host gateway owns authentication and CSP;
it never connects to a project-selected TCP address or exposes host services.
"""

import asyncio
import base64
import contextlib
import http.client
import io
import json
import queue
import secrets
import shutil
import threading
import time
from urllib.parse import unquote

from .preview import CSP, Preview, PreviewServer, _parts

MAX_STREAM = 8 * 1024**2

# This adapter is copied into MXC scratch, outside writable project source. It
# attaches to one requested HTTP port without binding a real socket. No changes
# to the native sandbox's offline network or host-loopback policy are needed.
SHIM = r"""
const http = require('node:http'), {Duplex} = require('node:stream');
const readline = require('node:readline');
const token = process.env.PANINI_BRIDGE_TOKEN, port = Number(process.env.PANINI_BRIDGE_PORT);
const output = process.stdout.write.bind(process.stdout), channels = new Map();
let server;
function emit(value) { output(token + JSON.stringify(value) + '\n'); }
const listen = http.Server.prototype.listen, close = http.Server.prototype.close;
http.Server.prototype.listen = function(...args) {
  const selected = typeof args[0] === 'object' ? args[0].port : args[0];
  if (Number(selected) !== port) return listen.apply(this, args);
  if (server) throw Error('Only one HTTP preview server may use the bridge');
  server = this;
  this.address = () => ({address:'127.0.0.1', family:'IPv4', port});
  Object.defineProperty(this, 'listening', {get:()=>true, configurable:true});
  const callback = args.findLast(value => typeof value === 'function');
  if (callback) this.once('listening', callback);
  process.nextTick(() => { this.emit('listening'); emit({ready:true}); });
  return this;
};
http.Server.prototype.close = function(callback) {
  if (this !== server) return close.call(this, callback);
  for (const socket of channels.values()) socket.destroy();
  process.nextTick(() => { this.emit('close'); if (callback) callback(); });
  return this;
};
class Socket extends Duplex {
  constructor(id) {
    super({highWaterMark:65536}); this.id=id; this.remoteAddress='127.0.0.1';
    this.remotePort=1; this.localAddress='127.0.0.1'; this.localPort=port;
    this.on('error',()=>{});
  }
  _read() {}
  _write(data, encoding, callback) {
    for (let offset=0;offset<data.length;offset+=65536)
      emit({id:this.id,data:data.subarray(offset,offset+65536).toString('base64')});
    callback();
  }
  _final(callback) { emit({id:this.id,end:true}); callback(); }
  _destroy(error, callback) { channels.delete(this.id); emit({id:this.id,end:true}); callback(error); }
  setTimeout(value, callback) { if (callback) this.on('timeout',callback); return this; }
  setNoDelay() { return this; }
  setKeepAlive() { return this; }
}
readline.createInterface({input:process.stdin, crlfDelay:Infinity}).on('line', line => {
  if (line.length>100000 || !line.startsWith(token)) return;
  try {
    const message=JSON.parse(line.slice(token.length)); let socket=channels.get(message.id);
    if (message.open && server && channels.size<16) {
      socket=new Socket(message.id); channels.set(message.id,socket); server.emit('connection',socket);
    }
    if (!socket) return;
    if (message.data) socket.push(Buffer.from(message.data,'base64'));
    if (message.end) socket.destroy();
  } catch (_) { /* malformed pipe frames do not execute code */ }
});
"""


class Channel(io.RawIOBase):
    def __init__(self, owner, identifier):
        self.owner, self.id = owner, identifier
        self.chunks = queue.Queue(maxsize=32)
        self.pending = bytearray()
        self.total = 0
        self.deadline = time.monotonic() + 300
        self.websocket = False

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.pending:
            while True:
                try:
                    chunk = self.chunks.get(timeout=1 if self.websocket else max(.01, min(20, self.deadline - time.monotonic())))
                    break
                except queue.Empty:
                    if self.websocket and time.monotonic() < self.deadline and self.owner.terminal.running:
                        continue
                    raise TimeoutError("Sandbox preview response timed out") from None
            if chunk is None:
                return 0
            self.total += len(chunk)
            if self.total > MAX_STREAM or time.monotonic() > self.deadline:
                raise OSError("Sandbox preview stream exceeded its limits")
            self.pending.extend(chunk)
        count = min(len(buffer), len(self.pending))
        buffer[:count] = self.pending[:count]
        del self.pending[:count]
        return count

    def makefile(self, *args, **kwargs):
        return io.BufferedReader(self)

    def close(self):
        self.owner.finish(self.id)
        super().close()


class PipeBridge:
    def __init__(self, terminals):
        self.terminals, self.loop = terminals, asyncio.get_running_loop()
        self.token = "PANINI_" + secrets.token_hex(24) + ":"
        self.terminal, self.pending, self.channels = None, "", {}
        self.lock = asyncio.Lock()
        self.guard = threading.Lock()
        self.ready = asyncio.Event()

    def output(self, text):
        self.pending += text
        logs = []
        if len(self.pending) > 1024**2:
            self.pending = ""
            return "[preview output frame discarded: limit exceeded]\n"
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if not line.startswith(self.token):
                logs.append(line)
                continue
            channel = None
            try:
                frame = json.loads(line[len(self.token):])
                if frame.get("ready") is True:
                    self.ready.set()
                with self.guard:
                    channel = self.channels.get(frame.get("id"))
                    if channel:
                        chunk = base64.b64decode(frame["data"], validate=True) if "data" in frame else None
                        if chunk is not None and len(chunk) > 65536:
                            raise ValueError("Invalid bridge chunk")
                        channel.chunks.put_nowait(chunk)
            except (ValueError, KeyError, TypeError, queue.Full):
                # The project owns its responses, but not unbounded host buffering.
                if channel:
                    self.finish(channel.id)
        return "\n".join(logs) + ("\n" if logs else "")

    async def _send(self, frame):
        async with self.lock:
            if not self.terminal or not self.terminal.running:
                raise OSError("Sandbox preview process has ended")
            await self.terminals.send(self.terminal.id, self.token + json.dumps(frame, separators=(",", ":")))

    def send(self, frame):
        asyncio.run_coroutine_threadsafe(self._send(frame), self.loop).result(timeout=5)

    def open(self, data):
        identifier = secrets.token_hex(12)
        with self.guard:
            if len(self.channels) >= 16:
                raise OSError("Sandbox preview connection limit reached")
            channel = self.channels[identifier] = Channel(self, identifier)
        try:
            self.send({"id": identifier, "open": True})
            for offset in range(0, len(data), 32768):
                self.send({"id": identifier, "data": base64.b64encode(data[offset:offset+32768]).decode()})
            return channel
        except BaseException:
            self.finish(identifier)
            raise

    def finish(self, identifier):
        with self.guard:
            channel = self.channels.pop(identifier, None)
            if channel:
                with contextlib.suppress(queue.Full):
                    channel.chunks.put_nowait(None)
        if not self.loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(self._send({"id": identifier, "end": True}), self.loop)
            future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    def close(self):
        for identifier in list(self.channels):
            self.finish(identifier)


class LiveServer(PreviewServer):
    def __init__(self, workspace, bridge):
        super().__init__(workspace, workspace)
        self.bridge = bridge

        class Handler(self.Handler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                parsed = self.authorize()
                if parsed is None:
                    return
                channel = None
                responded = False
                try:
                    path = unquote(parsed.path, errors="strict").removeprefix("/")
                    if path:
                        _parts(path.rstrip("/").replace("/.vite/", "/vite/"))
                    if self.command not in {"GET", "HEAD", "POST"}:
                        self.reply(405)
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 32768 or self.headers.get("Transfer-Encoding"):
                        self.reply(413)
                        return
                    body = self.rfile.read(length) if length else b""
                    if sum(len(k) + len(v) for k, v in self.headers.items()) > 32768:
                        self.reply(431)
                        return
                    websocket = self.headers.get("Upgrade", "").lower() == "websocket"
                    headers = []
                    for name, value in self.headers.items():
                        if name.lower() not in {"authorization", "proxy-authorization", "cookie", "connection", "content-length"}:
                            headers.append(f"{name}: {value}")
                    headers += ["Connection: Upgrade" if websocket else "Connection: close", f"Content-Length: {length}"]
                    request = f"{self.command} {self.path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
                    channel = bridge.open(request.encode("latin1") + body)
                    response = http.client.HTTPResponse(channel, method=self.command)
                    response.begin()
                    responded = True
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in {"connection", "transfer-encoding", "content-length", "content-security-policy", "set-cookie", "location"}:
                            self.send_header(name, value)
                    location = response.getheader("Location")
                    if location and location.startswith("/") and not location.startswith("//"):
                        self.send_header("Location", location)
                    self.send_header("Content-Security-Policy", CSP)
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "Upgrade" if websocket and response.status == 101 else "close")
                    self.end_headers()
                    self.close_connection = True
                    if websocket and response.status == 101:
                        self.wfile.flush()
                        self._websocket(channel, response.fp)
                    else:
                        total = 0
                        while chunk := response.read(65536):
                            total += len(chunk)
                            if total > MAX_STREAM:
                                raise OSError("Preview response too large")
                            if self.command != "HEAD":
                                self.wfile.write(chunk)
                    response.close()
                except (OSError, ValueError, http.client.HTTPException):
                    if not responded:
                        self.reply(404 if channel is None else 502)
                    self.close_connection = True
                finally:
                    if channel:
                        channel.close()

            def _websocket(self, channel, stream):
                channel.websocket = True
                channel.deadline = time.monotonic() + min(3600, bridge.terminals.settings.terminal_timeout)
                def incoming():
                    total = 0
                    try:
                        self.connection.settimeout(1)
                        while time.monotonic() < channel.deadline:
                            try:
                                data = self.connection.recv(32768)
                            except TimeoutError:
                                if channel.id not in bridge.channels:
                                    break
                                continue
                            if not data:
                                break
                            total += len(data)
                            if total > MAX_STREAM or time.monotonic() > channel.deadline:
                                break
                            bridge.send({"id": channel.id, "data": base64.b64encode(data).decode()})
                    except OSError:
                        pass
                    finally:
                        bridge.finish(channel.id)
                threading.Thread(target=incoming, daemon=True).start()
                while data := stream.read1(65536):
                    self.wfile.write(data)
                    self.wfile.flush()

            do_HEAD = do_POST = do_GET

        self.server.RequestHandlerClass = Handler

    def close(self):
        self.bridge.close()
        super().close()


async def start_live(previews, terminals, entry, args=(), port=5173):
    policy = previews.broker.policy()
    if policy.backend != "mxc" or not policy.local_preview or policy.read_only:
        raise ValueError("Live previews require workspace-write MXC and --local-preview")
    from .tools import Workspace
    path = Workspace(policy.workspace).resolve(entry)
    if not path.is_file() or path.suffix.lower() not in {".js", ".mjs", ".cjs"}:
        raise ValueError("Live preview entry must be a workspace Node.js module")
    if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a) > 4096 for a in args):
        raise ValueError("Live preview accepts at most 32 bounded string arguments")
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Virtual preview port must be 1024-65535")
    node = shutil.which("node")
    if not node:
        raise OSError("Live previews require Node.js")
    bridge = PipeBridge(terminals)
    terminal = await terminals.start(f"Node live preview: {entry}", process_argv=[node, str(path), *args],
        bridge={"source": SHIM, "token": bridge.token, "port": port}, on_output=bridge.output)
    bridge.terminal = terminal
    try:
        await asyncio.wait_for(bridge.ready.wait(), 20)
        server = LiveServer(policy.workspace, bridge).start()
    except BaseException:
        await terminals.stop(terminal.id)
        raise
    previews.counter += 1
    preview = Preview(f"p{previews.counter}", str(path.relative_to(policy.workspace)), server,
                      {**previews.broker.authorization(), "previewMode": "sandbox-http", "terminalId": terminal.id,
                       "transport": "stdio", "hostLoopback": "deny"})
    previews.sessions[preview.id] = preview
    preview.watchdog = asyncio.create_task(previews._expire(preview.id))
    return preview
