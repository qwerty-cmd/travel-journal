"""
The SPA fallback serves real dist-root files as themselves (t-offline-app-shell).

Without that, `/sw.js` came back as index.html and the service worker never
registered. The file branch is a trust boundary: `full_path` is attacker text,
already percent-decoded by the server, so every escape below must fall through
to index.html and never return the sentinel placed just outside the static dir.

Escape requests are driven through a hand-built ASGI scope, not TestClient:
httpx normalises `..` / `%2e%2e` client-side, which would make these tests
vacuous. The scope carries `path` exactly as uvicorn would produce it
(percent-decoded, no dot-segment removal), and each case asserts the decoded
path still contains the escape before checking the response.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote

import pytest
from conftest import make_test_client

from app.core.config import get_settings

SENTINEL = "TOP-SECRET-SENTINEL-7f3a"
INDEX = "<!doctype html><html><body>SPA shell</body></html>"


@pytest.fixture
def spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ModuleType, Path]]:
    """Real app rebuilt against a temp dist, with sentinels just outside it."""
    import app.main

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("// bundle\n", encoding="utf-8")
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('fetch', () => {});\n", encoding="utf-8")
    (dist / "icons").mkdir()
    (dist / "icons" / "nested.txt").write_bytes(b"nested file body\n")
    (dist / "api").mkdir()
    (dist / "api" / "nope").write_text("MUST-NOT-BE-SERVED\n", encoding="utf-8")

    # Outside dist: a plain sibling file, and a sibling dir whose name shares
    # the "dist" prefix (catches a startswith() containment check).
    (tmp_path / "secret.txt").write_text(SENTINEL, encoding="utf-8")
    (tmp_path / "dist-evil").mkdir()
    (tmp_path / "dist-evil" / "secret.txt").write_text(SENTINEL, encoding="utf-8")

    monkeypatch.setenv("STATIC_FILES_DIR", str(dist))
    get_settings.cache_clear()
    module = importlib.reload(app.main)
    assert any(getattr(r, "path", "") == "/{full_path:path}" for r in module.app.routes)
    try:
        yield module, tmp_path
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        importlib.reload(app.main)


def _raw_get(asgi_app, raw_path: str) -> tuple[int, dict[str, str], bytes]:
    """GET `raw_path` straight into the ASGI app, decoded the way uvicorn does."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1", "replace"),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    status = 0
    headers: dict[str, str] = {}
    body = bytearray()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            headers.update({k.decode().lower(): v.decode() for k, v in message["headers"]})
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(asgi_app(scope, receive, send))
    return status, headers, bytes(body)


def _escape_paths(root: Path) -> list[str]:
    sentinel = root / "secret.txt"
    return [
        "/../secret.txt",
        "/../../secret.txt",
        "/icons/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2E%2E/secret.txt",
        "/%2e%2E/secret.txt",
        "/.%2e/secret.txt",
        "/..%2fsecret.txt",
        "/..%2Fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "/icons%2f..%2f..%2fsecret.txt",
        "/..%5csecret.txt",
        "/%2e%2e%5csecret.txt",
        "/..\\secret.txt",
        "/icons\\..\\..\\secret.txt",
        "/../dist-evil/secret.txt",
        "/%2e%2e/dist-evil/secret.txt",
        # Absolute paths: joining an absolute path onto root discards root.
        "/" + sentinel.as_posix(),
        "//" + sentinel.as_posix().lstrip("/"),
        "/" + str(sentinel),
        "/" + str(sentinel).replace("\\", "%5c"),
        "/" + sentinel.as_posix().replace("/", "%2f"),
    ]


def test_escape_attempts_never_return_the_sentinel(spa) -> None:
    module, root = spa
    for raw in _escape_paths(root):
        decoded = unquote(raw)
        # Non-vacuity: the escape really reaches the server undecoded-away.
        assert ".." in decoded or "secret.txt" in decoded, raw
        status, headers, body = _raw_get(module.app, raw)
        assert SENTINEL.encode() not in body, f"sentinel leaked via {raw!r}"
        assert status == HTTPStatus.OK, raw
        assert body.decode() == INDEX, f"{raw!r} did not fall through to index.html"
        assert headers["content-type"].startswith("text/html"), raw


def test_escape_path_actually_reaches_spa_fallback_unnormalised(spa) -> None:
    """Guards the harness: `full_path` must arrive with the `..` intact."""
    module, _ = spa
    route = next(r for r in module.app.routes if getattr(r, "path", "") == "/{full_path:path}")
    match_scope = {"type": "http", "method": "GET", "path": unquote("/%2e%2e/secret.txt")}
    _, child = route.matches(match_scope)
    assert child["path_params"]["full_path"] == "../secret.txt"


def test_sw_js_is_served_as_javascript(spa) -> None:
    module, _ = spa
    response = make_test_client(module.app).get("/sw.js")
    assert response.status_code == HTTPStatus.OK
    assert "javascript" in response.headers["content-type"]
    assert "text/html" not in response.headers["content-type"]
    assert "addEventListener" in response.text


def test_nested_real_file_is_served_as_itself(spa) -> None:
    module, _ = spa
    response = make_test_client(module.app).get("/icons/nested.txt")
    assert response.status_code == HTTPStatus.OK
    assert response.text == "nested file body\n"


@pytest.mark.parametrize("path", ["/", "/icons", "/icons/"])
def test_directory_path_serves_index_html(spa, path: str) -> None:
    module, _ = spa
    response = make_test_client(module.app).get(path)
    assert response.status_code == HTTPStatus.OK
    assert response.text == INDEX


def test_deep_link_serves_index_html(spa) -> None:
    module, _ = spa
    response = make_test_client(module.app).get("/t/abc")
    assert response.status_code == HTTPStatus.OK
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")


def test_unknown_api_path_is_404_envelope_even_if_file_exists(spa) -> None:
    module, _ = spa
    response = make_test_client(module.app).get("/api/nope")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "MUST-NOT-BE-SERVED" not in response.text
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == "NOT_FOUND"


NUL_PATHS = ["/%00", "/sw.js%00", "/t/a%00b"]


def _assert_nul_paths_serve_index(module: ModuleType) -> None:
    for raw in NUL_PATHS:
        assert "\x00" in unquote(raw), raw
        status, headers, body = _raw_get(module.app, raw)
        assert status == HTTPStatus.OK, raw
        assert body.decode() == INDEX, f"{raw!r} did not fall through to index.html"
        assert headers["content-type"].startswith("text/html"), raw


def test_nul_byte_paths_serve_index_html(spa) -> None:
    _assert_nul_paths_serve_index(spa[0])


def test_nul_byte_paths_serve_index_html_when_resolve_rejects_nul(
    spa, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux's resolve() raises "embedded null character"; Windows doesn't.
    Mirror Linux so this runs the failing branch on every OS."""
    real_resolve = Path.resolve

    def linux_like_resolve(self: Path, *args, **kwargs) -> Path:
        if "\x00" in str(self):
            raise ValueError("embedded null character in path")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", linux_like_resolve)
    _assert_nul_paths_serve_index(spa[0])
