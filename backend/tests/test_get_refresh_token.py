"""
The one-time Graph refresh-token helper -- ``app/storage/get_refresh_token.py``.

Implementation-following tests (not a priority-tier module): they cover the
pure pieces of the PKCE authorization-code flow -- building the authorize URL,
parsing the single redirect, redeeming the code -- plus ``main()``: its refusal to
start without client credentials or with invalid settings, and one full
success path (PKCE challenge derivation, stdout secrecy).

**Nothing here opens a browser, binds a socket or reaches the network.** The
token endpoint is faked at the wire with ``httpx.MockTransport``, and every test
of ``main()`` replaces ``webbrowser.open`` and ``_wait_for_redirect`` with
functions that fail the test if they are ever called.

Secrets are sentinels chosen to be unmistakable, so the secrecy assertions can
search every ``SystemExit`` message for them.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
from collections.abc import Callable
from typing import Self
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.config import Settings
from app.storage import get_refresh_token as helper

TOKEN_URL = helper.TOKEN_URL

CLIENT_ID = "client-id-SENTINEL-4f1c"
CLIENT_SECRET = "client-secret-SENTINEL-9b2e"
AUTH_CODE = "auth-code-SENTINEL-7d3a"
VERIFIER = "pkce-verifier-SENTINEL-1e8f"
REFRESH_TOKEN = "refresh-token-SENTINEL-c05d"
STATE = "state-SENTINEL-66aa"
CHALLENGE = "challenge-SENTINEL-2b7c"

ACCESS_TOKEN = "access-token-SENTINEL-a11e"

SECRETS = (CLIENT_SECRET, AUTH_CODE, VERIFIER)


def _settings(client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET) -> Settings:
    # model_construct: no env/.env lookup, so the Graph values are exactly these.
    return Settings.model_construct(
        graph_client_id=client_id,
        graph_client_secret=client_secret,
        graph_refresh_token="",
    )


def _assert_no_secrets(message: object) -> None:
    text = str(message)
    for secret in SECRETS:
        assert secret not in text, f"SystemExit message leaked a secret: {text!r}"


# --------------------------------------------------------------------------- #
# TOKEN_URL
# --------------------------------------------------------------------------- #


def test_token_url_matches_onedrive_sync() -> None:
    # Imported here only: onedrive_sync pulls in s3_client, which loads settings.
    from app.storage import onedrive_sync

    assert helper.TOKEN_URL == onedrive_sync.TOKEN_URL


# --------------------------------------------------------------------------- #
# build_authorize_url
# --------------------------------------------------------------------------- #


def test_authorize_url_targets_the_same_tenant_endpoint_as_token_url() -> None:
    url = urlsplit(helper.build_authorize_url(CLIENT_ID, STATE, CHALLENGE))
    token = urlsplit(TOKEN_URL)

    assert (url.scheme, url.netloc) == (token.scheme, token.netloc)
    assert url.path == token.path.removesuffix("/token") + "/authorize"


def test_authorize_url_carries_the_pkce_code_flow_parameters() -> None:
    query = parse_qs(urlsplit(helper.build_authorize_url(CLIENT_ID, STATE, CHALLENGE)).query)
    params = {k: v[0] for k, v in query.items()}

    assert all(len(v) == 1 for v in query.values())
    assert params["client_id"] == CLIENT_ID
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == "http://localhost:8765"
    assert params["state"] == STATE
    assert params["code_challenge"] == CHALLENGE
    assert params["code_challenge_method"] == "S256"
    scopes = params["scope"].split()
    assert "offline_access" in scopes
    assert "Files.ReadWrite" in scopes


# --------------------------------------------------------------------------- #
# parse_redirect
# --------------------------------------------------------------------------- #


def test_parse_redirect_returns_the_code() -> None:
    assert helper.parse_redirect(f"/?code={AUTH_CODE}&state={STATE}", STATE) == AUTH_CODE


def test_parse_redirect_exits_on_error_param() -> None:
    with pytest.raises(SystemExit) as exc:
        helper.parse_redirect(
            f"/?error=access_denied&error_description=user+cancelled&state={STATE}", STATE
        )
    assert exc.value.code not in (0, None)
    assert "access_denied" in str(exc.value.code)


def test_parse_redirect_exits_on_state_mismatch() -> None:
    with pytest.raises(SystemExit) as exc:
        helper.parse_redirect(f"/?code={AUTH_CODE}&state=someone-elses-state", STATE)
    assert exc.value.code not in (0, None)
    assert "state" in str(exc.value.code)
    _assert_no_secrets(exc.value.code)


def test_parse_redirect_exits_on_missing_state() -> None:
    with pytest.raises(SystemExit) as exc:
        helper.parse_redirect(f"/?code={AUTH_CODE}", STATE)
    assert exc.value.code not in (0, None)
    _assert_no_secrets(exc.value.code)


def test_parse_redirect_exits_on_missing_code() -> None:
    with pytest.raises(SystemExit) as exc:
        helper.parse_redirect(f"/?state={STATE}", STATE)
    assert exc.value.code not in (0, None)


def test_parse_redirect_exits_on_empty_path() -> None:
    # _wait_for_redirect returns "" if no request was captured.
    with pytest.raises(SystemExit) as exc:
        helper.parse_redirect("", STATE)
    assert exc.value.code not in (0, None)


# --------------------------------------------------------------------------- #
# exchange_code
# --------------------------------------------------------------------------- #


def _client(*, respond: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(respond))


def test_exchange_code_returns_the_refresh_token() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "at", "refresh_token": REFRESH_TOKEN, "token_type": "Bearer"},
        )

    with _client(respond=respond) as client:
        assert helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings()) == REFRESH_TOKEN


def test_exchange_code_posts_the_authorization_code_grant() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"refresh_token": REFRESH_TOKEN})

    with _client(respond=respond) as client:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == TOKEN_URL
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == AUTH_CODE
    assert form["code_verifier"] == VERIFIER
    assert form["redirect_uri"] == "http://localhost:8765"
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET


def test_exchange_code_non_2xx_exits_with_only_graphs_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "AADSTS70008: The provided authorization code has expired.",
                "trace_id": "trace-should-not-matter",
            },
        )

    with _client(respond=respond) as client, pytest.raises(SystemExit) as exc:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())

    message = str(exc.value.code)
    assert exc.value.code not in (0, None)
    assert "invalid_grant" in message
    assert "AADSTS70008" in message
    assert "400" in message
    _assert_no_secrets(message)


def test_exchange_code_2xx_without_refresh_token_exits() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer"})

    with _client(respond=respond) as client, pytest.raises(SystemExit) as exc:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())

    assert exc.value.code not in (0, None)
    _assert_no_secrets(exc.value.code)


def test_exchange_code_2xx_with_empty_refresh_token_exits() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": ""})

    with _client(respond=respond) as client, pytest.raises(SystemExit):
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())


def test_exchange_code_non_json_error_body_is_handled() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502, text="<html>Bad Gateway</html>", headers={"content-type": "text/html"}
        )

    with _client(respond=respond) as client, pytest.raises(SystemExit) as exc:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())

    message = str(exc.value.code)
    assert exc.value.code not in (0, None)
    assert "502" in message
    _assert_no_secrets(message)


def test_exchange_code_non_json_2xx_body_exits() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with _client(respond=respond) as client, pytest.raises(SystemExit) as exc:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())
    _assert_no_secrets(exc.value.code)


def test_exchange_code_non_dict_json_body_exits_cleanly() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1])

    with _client(respond=respond) as client, pytest.raises(SystemExit) as exc:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())

    assert exc.value.code not in (0, None)
    assert isinstance(exc.value.code, str)
    _assert_no_secrets(exc.value.code)


def test_exchange_code_non_dict_json_error_body_exits_cleanly() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=[1])

    with _client(respond=respond) as client, pytest.raises(SystemExit) as exc:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())

    assert exc.value.code not in (0, None)
    assert "400" in str(exc.value.code)
    _assert_no_secrets(exc.value.code)


def test_exchange_code_error_does_not_echo_secrets_graph_reflected_back() -> None:
    # The message carries only error/error_description -- any other field in
    # the body (even one that happens to echo request values) is dropped.
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
                "echo": {"client_secret": CLIENT_SECRET, "code": AUTH_CODE, "v": VERIFIER},
            },
        )

    with _client(respond=respond) as client, pytest.raises(SystemExit) as exc:
        helper.exchange_code(client, AUTH_CODE, VERIFIER, _settings())
    assert "invalid_client" in str(exc.value.code)
    _assert_no_secrets(exc.value.code)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if main() gets as far as the browser or the socket."""

    def browser(*args: object, **kwargs: object) -> bool:
        pytest.fail("webbrowser.open was called")

    def wait() -> str:
        pytest.fail("_wait_for_redirect was called")

    monkeypatch.setattr(helper.webbrowser, "open", browser)
    monkeypatch.setattr(helper, "_wait_for_redirect", wait)


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [("", CLIENT_SECRET), (CLIENT_ID, ""), ("", "")],
    ids=["no-client-id", "no-client-secret", "neither"],
)
def test_main_exits_without_client_credentials(
    monkeypatch: pytest.MonkeyPatch,
    no_side_effects: None,
    capsys: pytest.CaptureFixture[str],
    client_id: str,
    client_secret: str,
) -> None:
    monkeypatch.setattr(helper, "get_settings", lambda: _settings(client_id, client_secret))

    with pytest.raises(SystemExit) as exc:
        helper.main()

    assert exc.value.code not in (0, None)
    assert "GRAPH_CLIENT_ID" in str(exc.value.code)
    _assert_no_secrets(exc.value.code)
    out = capsys.readouterr()
    _assert_no_secrets(out.out + out.err)


def test_main_invalid_settings_exits_with_field_names_only(
    monkeypatch: pytest.MonkeyPatch,
    no_side_effects: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_secret = "postgresql://u:db-password-SENTINEL-5e5e@host/db"
    s3_secret = "s3-secret-SENTINEL-8c8c"
    monkeypatch.setenv("DATABASE_URL", db_secret)
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", s3_secret)
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", CLIENT_SECRET)
    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_BUCKET_NAME"):
        monkeypatch.delenv(name, raising=False)

    def failing_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setattr(helper, "get_settings", failing_settings)

    with pytest.raises(SystemExit) as exc:
        helper.main()

    message = str(exc.value.code)
    assert exc.value.code not in (0, None)
    for field in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_BUCKET_NAME"):
        assert field in message
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__
    out = capsys.readouterr()
    assert "SENTINEL" not in message
    assert "SENTINEL" not in out.out + out.err
    for secret in (db_secret, s3_secret, CLIENT_SECRET):
        assert secret not in message


def test_main_success_sends_s256_challenge_of_the_posted_verifier_and_prints_only_the_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened: list[str] = []
    posted: list[dict[str, str]] = []

    def browser(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        return True

    def wait() -> str:
        assert len(opened) == 1, "redirect awaited before the browser was opened"
        state = parse_qs(urlsplit(opened[0]).query)["state"][0]
        return f"/?code={AUTH_CODE}&state={state}"

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        posted.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "token_type": "Bearer",
            },
        )

    real_client = httpx.Client

    def mock_client(*args: object, **kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(helper, "get_settings", lambda: _settings())
    monkeypatch.setattr(helper.webbrowser, "open", browser)
    monkeypatch.setattr(helper, "_wait_for_redirect", wait)
    monkeypatch.setattr(helper.httpx, "Client", mock_client)

    assert helper.main() == 0

    assert len(opened) == 1
    assert len(posted) == 1
    form = posted[0]
    verifier = form["code_verifier"]
    assert form["code"] == AUTH_CODE

    challenge = parse_qs(urlsplit(opened[0]).query)["code_challenge"][0]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    assert "=" not in challenge
    assert challenge != verifier

    out = capsys.readouterr()
    printed = out.out + out.err
    assert REFRESH_TOKEN in out.out
    for secret in (ACCESS_TOKEN, CLIENT_SECRET, AUTH_CODE, verifier):
        assert secret not in printed
    assert json.dumps(REFRESH_TOKEN) not in printed  # no JSON body dump


# --------------------------------------------------------------------------- #
# _wait_for_redirect's request handler
# --------------------------------------------------------------------------- #


def _capture_handler_class(monkeypatch: pytest.MonkeyPatch) -> type:
    """Run _wait_for_redirect against a fake HTTPServer that binds nothing.

    ``Handler`` is a local class inside ``_wait_for_redirect``, so the only way
    to reach it without restructuring the module is to intercept the server
    constructor it is passed to.
    """
    captured: list[type] = []

    class FakeServer:
        def __init__(self, address: object, handler: type) -> None:
            captured.append(handler)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def handle_request(self) -> None:
            return None

    monkeypatch.setattr(helper.http.server, "HTTPServer", FakeServer)
    assert helper._wait_for_redirect() == ""
    assert len(captured) == 1
    return captured[0]


def test_redirect_handler_does_not_log_the_authorization_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler_cls = _capture_handler_class(monkeypatch)
    assert issubclass(handler_cls, http.server.BaseHTTPRequestHandler)

    handler = handler_cls.__new__(handler_cls)
    path = f"/?code={AUTH_CODE}&state={STATE}"
    handler.path = path
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.client_address = ("127.0.0.1", 50000)

    handler.log_request(200)
    handler.log_message('"%s" %s %s', handler.requestline, "200", "-")

    out = capsys.readouterr()
    assert AUTH_CODE not in out.out + out.err
