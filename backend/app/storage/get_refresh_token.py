"""
One-time helper: obtain the initial Microsoft Graph refresh token for the OneDrive sync.

**Run only by the owner, in their own terminal -- never in an agent session.** It
opens a browser for a real Microsoft sign-in and prints a live credential. No
agent runs this flow.

**How it works.** ``uv run python -m app.storage.get_refresh_token`` (from
``backend/``) runs the OAuth authorization-code flow with PKCE (S256) against the
same ``/common`` tenant endpoint ``onedrive_sync.TOKEN_URL`` redeems against, so
the token it prints is one ``onedrive_sync`` can use as-is. It reads
``GRAPH_CLIENT_ID`` / ``GRAPH_CLIENT_SECRET`` from settings, opens the authorize
URL, catches exactly one redirect on ``http://localhost:8765`` (register that URI
on the app's *Web* platform), exchanges the code, and prints the refresh token
once. It writes no file; store the token yourself as ``GRAPH_REFRESH_TOKEN``.

It never prints the access token, client secret, authorization code or PKCE
verifier, and on failure prints only Graph's ``error`` / ``error_description``.

**Related.** ``app/storage/onedrive_sync.py`` (the consumer), the cutover
runbook Section 1 (Microsoft Graph).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import webbrowser
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pydantic

from app.core.config import Settings, get_settings

# Must equal ``onedrive_sync.TOKEN_URL`` (asserted by a test). Duplicated rather
# than imported: importing onedrive_sync pulls in s3_client, which loads settings
# at import time, and a settings ValidationError echoes env values (secrets).
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
AUTHORIZE_URL = TOKEN_URL.replace("/token", "/authorize")
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
SCOPE = "offline_access Files.ReadWrite"


def build_authorize_url(client_id: str, state: str, code_challenge: str) -> str:
    return (
        AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "response_mode": "query",
                "scope": SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def parse_redirect(path_and_query: str, expected_state: str) -> str:
    """The authorization code from the redirect, or SystemExit (non-zero) with a message."""
    params = {k: v[0] for k, v in parse_qs(urlsplit(path_and_query).query).items()}
    if "error" in params:
        raise SystemExit(
            f"Sign-in failed: {params['error']}: {params.get('error_description', '')}"
        )
    if not secrets.compare_digest(params.get("state", ""), expected_state):
        raise SystemExit("Sign-in failed: state mismatch -- try again")
    if not params.get("code"):
        raise SystemExit("Sign-in failed: no authorization code in the redirect")
    return params["code"]


def exchange_code(client: httpx.Client, code: str, verifier: str, settings: Settings) -> str:
    """Redeem the code for a refresh token, or SystemExit with Graph's error only."""
    response = client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": settings.graph_client_id,
            "client_secret": settings.graph_client_secret,
            "scope": SCOPE,
            "code_verifier": verifier,
        },
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if not response.is_success:
        raise SystemExit(
            f"Token exchange rejected (HTTP {response.status_code}): "
            f"{body.get('error', '')}: {body.get('error_description', '')}"
        )
    if not body.get("refresh_token"):
        raise SystemExit("Token exchange returned no refresh token -- was offline_access granted?")
    return body["refresh_token"]


def _wait_for_redirect() -> str:
    """Serve exactly one request on the redirect URI; return its path + query."""
    captured: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            captured.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done -- you can close this tab and return to the terminal.")

        def log_message(self, format: str, *args: object) -> None:
            pass  # the default access log would print the authorization code

    with http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler) as server:
        server.handle_request()
    return captured[0] if captured else ""


def main() -> int:
    try:
        settings = get_settings()
    except pydantic.ValidationError as err:
        # Name fields only: str(err) would include input_value, i.e. env secrets.
        fields = sorted({".".join(str(p) for p in e["loc"]).upper() for e in err.errors()})
        raise SystemExit(
            "Settings are missing or invalid: "
            + ", ".join(fields)
            + " -- set them in your shell or backend/.env first"
        ) from None
    if not settings.graph_client_id or not settings.graph_client_secret:
        raise SystemExit("GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET must both be set first")

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )

    url = build_authorize_url(settings.graph_client_id, state, challenge)
    print(f"Opening your browser to sign in. If it does not open, visit:\n{url}\n")
    webbrowser.open(url)

    code = parse_redirect(_wait_for_redirect(), state)
    with httpx.Client(timeout=30.0) as client:
        refresh_token = exchange_code(client, code, verifier, settings)

    print(refresh_token)
    print(
        "store this in your password manager / Azure secret (GRAPH_REFRESH_TOKEN); "
        "never commit it or paste it into chat"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
