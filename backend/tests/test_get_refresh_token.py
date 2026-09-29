"""
The one-time Graph refresh-token helper -- ``app/storage/get_refresh_token.py``.

Implementation-following tests (not a priority-tier module): they cover the
pure pieces of the PKCE authorization-code flow -- building the authorize URL,
parsing the single redirect, redeeming the code -- plus ``main()``'s refusal to
start without client credentials.

**Nothing here opens a browser, binds a socket or reaches the network.** The
token endpoint is faked at the wire with ``httpx.MockTransport``, and every test
of ``main()`` replaces ``webbrowser.open`` and ``_wait_for_redirect`` with
functions that fail the test if they are ever called.

Secrets are sentinels chosen to be unmistakable, so the secrecy assertions can
search every ``SystemExit`` message for them.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.config import Settings
from app.storage import get_refresh_token as helper
from app.storage.onedrive_sync import TOKEN_URL

CLIENT_ID = "client-id-SENTINEL-4f1c"
CLIENT_SECRET = "client-secret-SENTINEL-9b2e"
AUTH_CODE = "auth-code-SENTINEL-7d3a"
VERIFIER = "pkce-verifier-SENTINEL-1e8f"
REFRESH_TOKEN = "refresh-token-SENTINEL-c05d"
STATE = "state-SENTINEL-66aa"
CHALLENGE = "challenge-SENTINEL-2b7c"

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
