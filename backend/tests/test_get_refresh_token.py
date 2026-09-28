"""
Tests for the one-time Graph refresh-token helper (``app.storage.get_refresh_token``).

No real network, browser or port: token calls go through ``httpx.MockTransport``,
``webbrowser.open`` is recorded, and the one-shot redirect server is either
replaced or given a fake in-memory connection. The secrets checks run over
captured stdout + stderr.
"""

from __future__ import annotations

import base64
import builtins
import hashlib
import io
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.config import Settings
from app.storage import get_refresh_token as mod
from app.storage.onedrive_sync import TOKEN_URL

CLIENT_ID = "client-id-123"
CLIENT_SECRET = "s3cr3t-CLIENT-value"
CODE = "AUTH-CODE-xyz789"
ACCESS_TOKEN = "ACCESS-TOKEN-must-not-leak"
REFRESH_TOKEN = "REFRESH-TOKEN-abc456"
STORE_LINE = (
    "store this in your password manager / Azure secret (GRAPH_REFRESH_TOKEN); "
    "never commit it or paste it into chat"
)


def _settings(client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET) -> Settings:
    # model_construct: no validation, no .env read.
    return Settings.model_construct(graph_client_id=client_id, graph_client_secret=client_secret)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _exit_message(exc: pytest.ExceptionInfo[SystemExit]) -> str:
    # SystemExit(str) -> Python prints it to stderr and exits 1.
    assert isinstance(exc.value.code, str), "must exit non-zero with a message"
    return exc.value.code


def _token_client(status: int, body: dict | str, seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- AC1: authorize URL ---------------------------------------------------------


def test_authorize_url_targets_common_authorize_with_all_params() -> None:
    url = mod.build_authorize_url(CLIENT_ID, "STATE", "CHALLENGE")
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.netloc == "login.microsoftonline.com"
    assert parts.path == "/common/oauth2/v2.0/authorize"
    assert urlsplit(TOKEN_URL).path.replace("/token", "/authorize") == parts.path
    q = _query(url)
    assert q["client_id"] == CLIENT_ID
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "http://localhost:8765"
    assert q["response_mode"] == "query"
    assert {"offline_access", "Files.ReadWrite"} <= set(q["scope"].split())
    assert q["state"] == "STATE"
    assert q["code_challenge"] == "CHALLENGE"
    assert q["code_challenge_method"] == "S256"


# --- AC3: redirect parsing -------------------------------------------------------


def test_parse_redirect_matching_state_returns_code() -> None:
    assert mod.parse_redirect(f"/?code={CODE}&state=S1", "S1") == CODE


@pytest.mark.parametrize("path", [f"/?code={CODE}&state=OTHER", f"/?code={CODE}", "/"])
def test_parse_redirect_bad_or_missing_state_exits(path: str) -> None:
    with pytest.raises(SystemExit) as exc:
        mod.parse_redirect(path, "S1")
    msg = _exit_message(exc)
    assert "state mismatch" in msg
    assert CODE not in msg


def test_parse_redirect_error_param_exits_with_error_and_description() -> None:
    path = "/?error=access_denied&error_description=The+user+declined&state=S1"
    with pytest.raises(SystemExit) as exc:
        mod.parse_redirect(path, "S1")
    msg = _exit_message(exc)
    assert "access_denied" in msg
    assert "The user declined" in msg


def test_parse_redirect_matching_state_without_code_exits() -> None:
    with pytest.raises(SystemExit) as exc:
        mod.parse_redirect("/?state=S1", "S1")
    _exit_message(exc)


# --- AC4 / AC5: token exchange ---------------------------------------------------


def test_exchange_code_posts_full_form_and_returns_refresh_token() -> None:
    seen: list[httpx.Request] = []
    body = {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}
    with _token_client(200, body, seen) as client:
        assert mod.exchange_code(client, CODE, "VERIFIER", _settings()) == REFRESH_TOKEN

    [req] = seen
    assert req.method == "POST"
    assert str(req.url) == TOKEN_URL
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert form == {
        "grant_type": "authorization_code",
        "code": CODE,
        "redirect_uri": _query(mod.build_authorize_url(CLIENT_ID, "s", "c"))["redirect_uri"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "offline_access Files.ReadWrite",
        "code_verifier": "VERIFIER",
    }


@pytest.mark.parametrize("status", [400, 401, 500])
def test_exchange_code_non_2xx_exits_with_only_graph_error(status: int) -> None:
    seen: list[httpx.Request] = []
    body = {
        "error": "invalid_grant",
        "error_description": "AADSTS70008: code expired",
        "trace_id": "TRACE-ID-SHOULD-NOT-APPEAR",
        "access_token": ACCESS_TOKEN,
    }
    with _token_client(status, body, seen) as client, pytest.raises(SystemExit) as exc:
        mod.exchange_code(client, CODE, "VERIFIER", _settings())
    msg = _exit_message(exc)
    assert "invalid_grant" in msg
    assert "AADSTS70008: code expired" in msg
    for leaked in ("TRACE-ID-SHOULD-NOT-APPEAR", ACCESS_TOKEN, CLIENT_SECRET, CODE, "VERIFIER"):
        assert leaked not in msg


def test_exchange_code_non_json_error_body_is_not_echoed() -> None:
    seen: list[httpx.Request] = []
    with _token_client(502, "<html>RAW-BODY-SHOULD-NOT-APPEAR</html>", seen) as client:
        with pytest.raises(SystemExit) as exc:
            mod.exchange_code(client, CODE, "VERIFIER", _settings())
    assert "RAW-BODY-SHOULD-NOT-APPEAR" not in _exit_message(exc)


def test_exchange_code_2xx_without_refresh_token_exits() -> None:
    seen: list[httpx.Request] = []
    with _token_client(200, {"access_token": ACCESS_TOKEN}, seen) as client:
        with pytest.raises(SystemExit) as exc:
            mod.exchange_code(client, CODE, "VERIFIER", _settings())
    assert ACCESS_TOKEN not in _exit_message(exc)


# --- main(): wiring, AC2, AC6, AC7 -----------------------------------------------


class _Harness:
    """Records browser/server/network use during main()."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
        self.opened: list[str] = []
        self.server_started = False
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.token_body: dict = {
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "token_type": "Bearer",
        }
        self.redirect: str | None = None  # None -> echo the real state back

        real_client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.token_status, json=self.token_body)

        def client_factory(**kwargs: object) -> httpx.Client:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        def fake_wait() -> str:
            self.server_started = True
            if self.redirect is not None:
                return self.redirect
            state = _query(self.opened[-1])["state"]
            return f"/?code={CODE}&state={state}"

        monkeypatch.setattr(mod, "get_settings", lambda: settings)
        monkeypatch.setattr(mod.webbrowser, "open", lambda url, *a, **k: self.opened.append(url))
        monkeypatch.setattr(mod, "_wait_for_redirect", fake_wait)
        monkeypatch.setattr(mod.httpx, "Client", client_factory)


@pytest.mark.parametrize(
    ("client_id", "client_secret"), [("", CLIENT_SECRET), (CLIENT_ID, ""), ("", "")]
)
def test_main_missing_credentials_exits_before_browser_server_or_network(
    monkeypatch: pytest.MonkeyPatch, client_id: str, client_secret: str
) -> None:
    h = _Harness(monkeypatch, _settings(client_id, client_secret))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    msg = _exit_message(exc)
    assert "GRAPH_CLIENT_ID" in msg and "GRAPH_CLIENT_SECRET" in msg
    assert h.opened == []
    assert h.server_started is False
    assert h.requests == []


def test_main_success_prints_refresh_token_once_and_no_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = _Harness(monkeypatch, _settings())
    assert mod.main() == 0

    out, err = capsys.readouterr()
    assert out.count(REFRESH_TOKEN) == 1
    assert REFRESH_TOKEN not in err
    assert STORE_LINE in out.splitlines()
    [req] = h.requests
    verifier = parse_qs(req.content.decode())["code_verifier"][0]
    for leaked in (ACCESS_TOKEN, CLIENT_SECRET, CODE, verifier):
        assert leaked not in out
        assert leaked not in err

    # The printed URL is the one the browser opened.
    [url] = h.opened
    assert url in out


def test_main_pkce_challenge_is_s256_of_the_posted_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, _settings())
    mod.main()
    challenge = _query(h.opened[0])["code_challenge"]
    form = {k: v[0] for k, v in parse_qs(h.requests[0].content.decode()).items()}
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert form["code"] == CODE
    assert form["redirect_uri"] == _query(h.opened[0])["redirect_uri"]


def test_main_state_mismatch_exits_without_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = _Harness(monkeypatch, _settings())
    h.redirect = f"/?code={CODE}&state=forged"
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert "state mismatch" in _exit_message(exc)
    assert h.requests == []
    out, err = capsys.readouterr()
    assert CODE not in out + err + _exit_message(exc)


def test_main_token_rejection_prints_no_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = _Harness(monkeypatch, _settings())
    h.token_status = 400
    h.token_body = {"error": "invalid_client", "error_description": "bad secret"}
    with pytest.raises(SystemExit) as exc:
        mod.main()
    msg = _exit_message(exc)
    assert "invalid_client" in msg and "bad secret" in msg
    out, err = capsys.readouterr()
    for leaked in (CLIENT_SECRET, CODE, REFRESH_TOKEN):
        assert leaked not in out + err + msg


def test_main_writes_no_files(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    _Harness(monkeypatch, _settings())
    writes: list[tuple[object, str]] = []
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(c in mode for c in "wax+"):
            writes.append((file, mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.chdir(tmp_path)
    mod.main()
    assert writes == []
    assert list(tmp_path.iterdir()) == []


# --- _wait_for_redirect: one request, access log silenced ------------------------


class _FakeConnection:
    """In-memory stand-in for the accepted socket -- no port is bound."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.sent = b""

    def makefile(self, mode: str, bufsize: int = -1) -> io.BytesIO:
        return io.BytesIO(self._raw)

    def sendall(self, data: bytes) -> None:
        self.sent += data


def test_wait_for_redirect_returns_path_and_never_logs_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = f"GET /?code={CODE}&state=S1 HTTP/1.1\r\nHost: localhost:8765\r\n\r\n".encode()
    conn = _FakeConnection(raw)
    bound: list[tuple[str, int]] = []
    handled: list[int] = []

    class FakeServer:
        def __init__(self, address: tuple[str, int], handler_cls: type) -> None:
            bound.append(address)
            self._handler_cls = handler_cls

        def __enter__(self) -> FakeServer:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def handle_request(self) -> None:
            handled.append(1)
            self._handler_cls(conn, ("127.0.0.1", 50000), self)

    monkeypatch.setattr(mod.http.server, "HTTPServer", FakeServer)
    assert mod._wait_for_redirect() == f"/?code={CODE}&state=S1"
    assert bound == [("localhost", 8765)]
    assert handled == [1]
    assert conn.sent.startswith(b"HTTP/1.0 200")
    out, err = capsys.readouterr()
    assert CODE not in out + err
