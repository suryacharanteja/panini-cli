"""Public-origin HTTP with DNS validation and connections pinned to vetted addresses."""

import http.client
import ipaddress
import socket
import ssl
import time

import httpx

MAX_RESPONSE = 8 * 1024 * 1024


def public_address(value):
    address = ipaddress.ip_address(value)
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


class _Stream(httpx.SyncByteStream):
    def __init__(self, reply, connection, deadline):
        self.reply, self.connection, self.deadline = reply, connection, deadline

    def __iter__(self):
        total = 0
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.ReadTimeout("Public HTTP response exceeded its time limit")
            if self.connection.sock:
                self.connection.sock.settimeout(remaining)
            try:
                chunk = self.reply.read1(65536)
            except OSError as exc:
                raise httpx.ReadError(str(exc)) from exc
            if not chunk:
                return
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise httpx.ReadError("Public HTTP response exceeds 8 MiB")
            yield chunk

    def close(self):
        self.reply.close()
        self.connection.close()


def origin(value):
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError("Use an absolute HTTP(S) URL without embedded credentials.")
    port = url.port or (443 if url.scheme == "https" else 80)
    if port != (443 if url.scheme == "https" else 80):
        raise ValueError("Public HTTP access allows only ports 80 and 443.")
    host = url.host.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local services require a separate explicit access grant.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not public_address(address):
            raise ValueError("Private, loopback and special-use addresses are not public destinations.")
    return url.scheme, host, port


def addresses(host, port):
    found = sorted({row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not found or any(not public_address(value) for value in found):
        raise ValueError("Destination DNS includes a private or special-use address.")
    return found


class PublicTransport(httpx.BaseTransport):
    """No second DNS lookup, inherited proxy, redirect or credential forwarding."""

    def handle_request(self, request):
        scheme, host, port = origin(request.url)
        timeout = min(30, request.extensions.get("timeout", {}).get("read") or 20)
        deadline = time.monotonic() + timeout
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            resolved = addresses(host, port)
            last_error = None
            for address in resolved:
                try:
                    connection.sock = socket.create_connection((address, port), timeout=timeout)
                    break
                except OSError as exc:
                    last_error = exc
            if connection.sock is None:
                raise last_error or OSError("No reachable public address")
            if scheme == "https":
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
            connection.putrequest(request.method, request.url.raw_path.decode("ascii"), skip_host=True)
            for name, value in request.headers.multi_items():
                if name.lower() not in {"connection", "proxy-authorization", "accept-encoding"}:
                    connection.putheader(name, value)
            connection.putheader("Accept-Encoding", "identity")
            connection.endheaders()
            for chunk in request.stream:
                connection.send(chunk)
            reply = connection.getresponse()
            if reply.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("Public HTTP requires an uncompressed response to enforce memory limits")
            return httpx.Response(reply.status, headers=reply.getheaders(),
                                  stream=_Stream(reply, connection, deadline), request=request)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            connection.close()
            raise httpx.TransportError(str(exc), request=request) from exc
