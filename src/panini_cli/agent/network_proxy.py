"""Task-owned HTTPS CONNECT proxy: exact public hosts, pinned transport addresses.

The sandbox must enforce proxy-only egress. Environment proxy variables alone
are never considered containment. This service holds no application credentials.
"""

import ipaddress
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import contextlib


def normalize_hosts(hosts):
    result = []
    for value in hosts:
        value = value.strip().lower().rstrip(".")
        if not value or len(value) > 253 or any(c in value for c in "/:@*\\"):
            raise ValueError("Network grants require exact DNS host names, without URLs, ports or wildcards.")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError("Network grants cannot authorize IP literals or local services.")
        labels = value.split(".")
        if len(labels) < 2 or any(not label or len(label) > 63 or not all(c.isascii() and (c.isalnum() or c == "-") for c in label)
                                  or label.startswith("-") or label.endswith("-") for label in labels):
            raise ValueError("Network grants require a public DNS host name.")
        if value not in result:
            result.append(value)
    return tuple(sorted(result))


def public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
        raise OSError("Destination resolves to a private, local or reserved address.")
    return addresses


class BoundedHTTPServer(ThreadingHTTPServer):
    """A loopback server with a hard worker cap; excess connections are dropped, not queued."""

    daemon_threads = True

    def __init__(self, address, handler, *, workers, timeout):
        self.workers = threading.BoundedSemaphore(workers)
        self.request_timeout = timeout
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        request.settimeout(self.request_timeout)
        if not self.workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class NetworkProxy:
    def __init__(self, hosts):
        self.hosts = frozenset(normalize_hosts(hosts))
        self.connections = set()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass  # Never log request paths or credentials.

            def do_CONNECT(self):
                if not owner.slots.acquire(blocking=False):
                    self.send_error(503)
                    return
                upstream = None
                try:
                    host, separator, port = self.path.rpartition(":")
                    if not separator or port != "443" or host.lower() not in owner.hosts:
                        self.send_error(403, "Host is outside this task's network grant")
                        return
                    addresses = public_addresses(host)
                    last_error = None
                    for family, kind, protocol, _, address in addresses:
                        upstream = socket.socket(family, kind, protocol)
                        upstream.settimeout(10)
                        try:
                            upstream.connect(address)  # Use the address actually validated above.
                            break
                        except OSError as error:
                            last_error = error
                            upstream.close()
                            upstream = None
                    if upstream is None:
                        raise last_error or OSError("No public destination is reachable")
                    with owner.lock:
                        owner.connections.update((self.connection, upstream))
                    self.send_response(200, "Connection Established")
                    self.end_headers()
                    # No HTTP body precedes CONNECT's tunnel. Unbuffered reads avoid
                    # hiding TLS bytes in BaseHTTPRequestHandler's header buffer.
                    sockets = (self.connection, upstream)
                    deadline = time.monotonic() + 600
                    while time.monotonic() < deadline:
                        readable, _, _ = select.select(sockets, (), (), 1)
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            (upstream if source is self.connection else self.connection).sendall(data)
                except OSError:
                    if upstream is None:
                        self.send_error(502, "Public destination connection failed")
                finally:
                    self.close_connection = True
                    with owner.lock:
                        owner.connections.discard(self.connection)
                        owner.connections.discard(upstream)
                    if upstream is not None:
                        upstream.close()
                    owner.slots.release()

            rbufsize = 0

            def do_GET(self):
                self.send_error(403, "Only HTTPS CONNECT is supported")

        self.server = BoundedHTTPServer(("127.0.0.1", 0), Handler, workers=32, timeout=10)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            for connection in self.connections:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
                connection.close()
        self.thread.join(timeout=2)
