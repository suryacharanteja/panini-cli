"""OAuth PKCE and bearer credentials for trusted remote MCP server configuration."""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from . import auth
from .public_http import PublicTransport, origin


def target(name, spec):
    return "PANINI-CLI/MCP/" + hashlib.sha256((name + spec["url"]).encode()).hexdigest()


def access_token(name, spec):
    variable = spec.get("token_env")
    if variable:
        return os.getenv(variable, "")
    saved = auth.read_key(target=target(name, spec))
    if not saved:
        return None
    try:
        record = json.loads(saved)
    except ValueError:
        return saved  # Explicitly saved bearer credentials.
    if record.get("expires_at", 0) > time.time() + 30:
        return record["access_token"]
    if not record.get("refresh_token"):
        return None
    with httpx.Client(transport=PublicTransport(), trust_env=False, follow_redirects=False, timeout=20) as client:
        response = client.post(record["token_endpoint"], data={"grant_type": "refresh_token",
            "refresh_token": record["refresh_token"], "client_id": record["client_id"], "resource": spec["url"]})
        response.raise_for_status()
        updated = response.json()
    record.update(updated)
    record["expires_at"] = time.time() + int(updated.get("expires_in", 3600))
    auth.save_key(json.dumps(record), target=target(name, spec))
    return record["access_token"]


def login(name, spec, *, bearer=None, open_browser=True, timeout=180):
    if origin(spec["url"])[0] != "https":
        raise ValueError("Remote MCP authentication requires HTTPS")
    if bearer is not None:
        auth.save_key(bearer, target=target(name, spec))
        return "Bearer credential saved securely"
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    received = {}
    complete = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            valid = (self.headers.get("Host") == f"127.0.0.1:{server.server_port}" and parsed.path == "/callback"
                     and secrets.compare_digest(query.get("state", [""])[0], state))
            if valid and query.get("code"):
                received["code"] = query["code"][0]
                complete.set()
            self.send_response(200 if valid else 400)
            self.end_headers()
            self.wfile.write(b"You may close this window." if valid else b"Invalid OAuth state")

    from .network_proxy import BoundedHTTPServer
    server = BoundedHTTPServer(("127.0.0.1", 0), Callback, workers=4, timeout=5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    try:
        with httpx.Client(transport=PublicTransport(), trust_env=False, follow_redirects=False, timeout=20) as client:
            oauth = spec.get("oauth") or {}
            metadata_url = oauth.get("metadata_url")
            if not metadata_url:
                base = httpx.URL(spec["url"])
                resource = client.get(str(base.copy_with(path="/.well-known/oauth-protected-resource", query=None)))
                resource.raise_for_status()
                issuers = resource.json().get("authorization_servers") or []
                if not issuers:
                    raise ValueError("MCP server must provide OAuth metadata or oauth.metadata_url")
                issuer = issuers[0].rstrip("/")
                metadata_url = issuer + "/.well-known/oauth-authorization-server"
            if origin(metadata_url)[0] != "https":
                raise ValueError("OAuth metadata requires HTTPS")
            metadata = client.get(metadata_url)
            metadata.raise_for_status()
            metadata = metadata.json()
            for key in ("authorization_endpoint", "token_endpoint"):
                if origin(metadata[key])[0] != "https":
                    raise ValueError("OAuth endpoints require HTTPS")
            client_id = oauth.get("client_id")
            if not client_id:
                registration = metadata.get("registration_endpoint")
                if not registration or origin(registration)[0] != "https":
                    raise ValueError("Configure oauth.client_id; server has no HTTPS dynamic registration")
                registered = client.post(registration, json={"client_name": "PANINI CLI", "redirect_uris": [redirect],
                    "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"]})
                registered.raise_for_status()
                client_id = registered.json()["client_id"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            url = metadata["authorization_endpoint"] + "?" + urlencode({"response_type": "code", "client_id": client_id,
                "redirect_uri": redirect, "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "scope": oauth.get("scopes", ""), "resource": spec["url"]})
            print("Authorize this configured MCP connection: " + url, flush=True)
            if open_browser:
                webbrowser.open(url)
            if not complete.wait(timeout):
                raise TimeoutError("OAuth authorization timed out")
            token = client.post(metadata["token_endpoint"], data={"grant_type": "authorization_code", "client_id": client_id,
                "code": received["code"], "redirect_uri": redirect, "code_verifier": verifier, "resource": spec["url"]})
            token.raise_for_status()
            record = token.json()
            record.update(client_id=client_id, token_endpoint=metadata["token_endpoint"],
                          expires_at=time.time() + int(record.get("expires_in", 3600)))
            auth.save_key(json.dumps(record), target=target(name, spec))
            return "OAuth credential saved securely"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
