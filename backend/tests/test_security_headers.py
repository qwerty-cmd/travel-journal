"""
Every response carries the baseline security headers -- API and SPA alike.

``X-Content-Type-Options: nosniff``, an explicit
``Referrer-Policy: strict-origin-when-cross-origin`` (so a ``/t/<slug>`` path
never reaches the OSM tile servers, while they still get the origin their tile
policy asks for), and framing refused via both ``X-Frame-Options: DENY`` and
``Content-Security-Policy: frame-ancestors 'none'``.

The SPA routes only exist when ``frontend/dist`` does, so the SPA tests rebuild
the app against a temporary dist -- a green result without one would say
nothing about the fallback or ``/assets``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

import app.main
from app.core.config import get_settings

EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
}


def assert_security_headers(response) -> None:
    for name, value in EXPECTED.items():
        assert response.headers.get(name) == value, (name, response.headers.get(name))


@pytest.fixture
def spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("// bundle\n", encoding="utf-8")
    (dist / "index.html").write_text("<!doctype html><title>SPA</title>\n", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('fetch', () => {});\n", encoding="utf-8")

    monkeypatch.setenv("STATIC_FILES_DIR", str(dist))
    get_settings.cache_clear()
    module = importlib.reload(app.main)
    try:
        yield module
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        importlib.reload(app.main)


def test_api_route_has_security_headers() -> None:
    response = TestClient(app.main.app).get("/api/health")
    assert response.status_code == 200
    assert_security_headers(response)


def test_api_error_envelope_has_security_headers() -> None:
    """Responses built by an exception handler (here the unknown-/api 404) too."""
    response = TestClient(app.main.app).get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert_security_headers(response)


def test_spa_fallback_has_security_headers(spa: ModuleType) -> None:
    response = TestClient(spa.app).get("/t/some-trip-slug/stops")
    assert response.status_code == 200
    assert "<title>SPA</title>" in response.text
    assert_security_headers(response)


@pytest.mark.parametrize("path", ["/assets/app.js", "/sw.js"])
def test_static_file_has_security_headers(spa: ModuleType, path: str) -> None:
    response = TestClient(spa.app).get(path)
    assert response.status_code == 200
    assert "html" not in response.headers["content-type"]
    assert_security_headers(response)
