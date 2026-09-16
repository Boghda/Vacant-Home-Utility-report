#!/usr/bin/env python3
"""
get_zendesk_token.py
--------------------
One-time helper to mint a Zendesk OAuth **access token** from an OAuth client
(client ID + secret) using the Authorization Code grant.

Zendesk has no headless client-credentials grant and issues no refresh tokens,
and its access tokens are long-lived — so the pattern for automation is:
run this once locally, then store the printed access token as the
ZENDESK_OAUTH_TOKEN secret. The report itself (report_exporter.py) only ever
uses that Bearer token; it never sees the client id/secret.

Prerequisites (Zendesk Admin → Apps and integrations → APIs → OAuth Clients):
  * an OAuth client whose "Redirect URLs" includes the redirect URI below
    (default: http://localhost:8080/callback).

Configure via env vars or a [zendesk_oauth] section in credentials.ini:
  ZENDESK_SUBDOMAIN, ZENDESK_CLIENT_ID, ZENDESK_CLIENT_SECRET,
  and optionally ZENDESK_REDIRECT_URI (default http://localhost:8080/callback).

Usage:
  python get_zendesk_token.py
Then copy the printed access token into the ZENDESK_OAUTH_TOKEN secret (or the
[zendesk] oauth_token field of credentials.ini for local runs).
"""

import os
import time
import json
import secrets
import configparser
import threading
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DEFAULT_REDIRECT = "http://localhost:8080/callback"
SCOPE = "read"


def load_client():
    cfg = {
        "subdomain":     os.environ.get("ZENDESK_SUBDOMAIN"),
        "client_id":     os.environ.get("ZENDESK_CLIENT_ID"),
        "client_secret": os.environ.get("ZENDESK_CLIENT_SECRET"),
        "redirect_uri":  os.environ.get("ZENDESK_REDIRECT_URI"),
    }

    ini_path = SCRIPT_DIR / "credentials.ini"
    if ini_path.exists():
        p = configparser.ConfigParser()
        p.read(ini_path)
        cfg["subdomain"]     = cfg["subdomain"]     or p.get("zendesk",       "subdomain",     fallback=None)
        cfg["client_id"]     = cfg["client_id"]     or p.get("zendesk_oauth", "client_id",     fallback=None)
        cfg["client_secret"] = cfg["client_secret"] or p.get("zendesk_oauth", "client_secret", fallback=None)
        cfg["redirect_uri"]  = cfg["redirect_uri"]  or p.get("zendesk_oauth", "redirect_uri",  fallback=None)

    cfg["redirect_uri"] = cfg["redirect_uri"] or DEFAULT_REDIRECT

    missing = [k for k in ("subdomain", "client_id", "client_secret") if not cfg.get(k)]
    if missing:
        raise SystemExit(
            f"Missing OAuth client config: {missing}. Set them as env vars "
            f"(ZENDESK_SUBDOMAIN / ZENDESK_CLIENT_ID / ZENDESK_CLIENT_SECRET) "
            f"or in the [zendesk_oauth] section of credentials.ini."
        )
    return cfg


def capture_code(redirect_uri, expected_state):
    """Run a tiny local server to catch the OAuth redirect and return the code."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            result["code"]  = (params.get("code")  or [None])[0]
            result["state"] = (params.get("state") or [None])[0]
            result["error"] = (params.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = ("Authorization received — you can close this tab and return "
                   "to the terminal." if result.get("code")
                   else f"Authorization failed: {result.get('error')}")
            self.wfile.write(f"<html><body><p>{msg}</p></body></html>".encode())

        def log_message(self, *args):
            pass  # silence default request logging

    server = HTTPServer((host, port), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server, result


def main():
    cfg = load_client()
    base = f"https://{cfg['subdomain']}.zendesk.com"
    state = secrets.token_urlsafe(16)

    server, result = capture_code(cfg["redirect_uri"], state)

    authorize_url = f"{base}/oauth/authorizations/new?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     cfg["client_id"],
        "scope":         SCOPE,
        "redirect_uri":  cfg["redirect_uri"],
        "state":         state,
    })

    print("Opening your browser to authorize the OAuth client...")
    print(f"If it does not open, visit this URL manually:\n{authorize_url}\n")
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass

    print(f"Waiting for the redirect to {cfg['redirect_uri']} ...")
    # handle_request() (running in the thread) blocks until one request arrives
    # and populates `result`; poll until it does.
    while not result:
        time.sleep(0.2)
    server.server_close()

    if result.get("error"):
        raise SystemExit(f"Authorization failed: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("State mismatch — aborting (possible CSRF).")
    code = result.get("code")
    if not code:
        raise SystemExit("No authorization code received.")

    print("Exchanging authorization code for an access token...")
    payload = json.dumps({
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri":  cfg["redirect_uri"],
        "scope":         SCOPE,
    }).encode()

    req = urllib.request.Request(
        f"{base}/oauth/tokens",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Token exchange failed: HTTP {e.code} — {e.read().decode()}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise SystemExit(f"No access_token in response: {token_data}")

    print("\n=== Success ===")
    print(f"Scope:        {token_data.get('scope')}")
    print(f"Access token: {access_token}")
    print(
        "\nStore this as the ZENDESK_OAUTH_TOKEN GitHub secret (or the "
        "[zendesk] oauth_token field of credentials.ini for local runs).\n"
        "The token is long-lived; keep it secret and revoke it in Zendesk if leaked."
    )


if __name__ == "__main__":
    main()
