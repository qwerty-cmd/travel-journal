"""
t-access-log-slug-exposure: the trip slug never reaches a uvicorn log line.

The slug is the link credential and it is in the URL by construction. These
tests prove two things separately:

1. `SlugRedactionFilter` rewrites exactly the slug segment of every slug-bearing
   path shape (API and SPA), keeps everything else, and leaves other paths
   byte-for-byte unchanged.
2. `log_config/uvicorn.json` — the file the Dockerfile CMD passes to
   `--log-config` — actually wires that filter onto the handlers, loaded the
   same way uvicorn loads it (through `uvicorn.config.Config`), so a config that
   parses but never attaches the filter fails here instead of silently shipping.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest
from uvicorn.config import Config

from log_config.redaction import REDACTED, SlugRedactionFilter, redact_path

SLUG = "somesecretslug123"
LOG_CONFIG_PATH = Path(__file__).resolve().parents[1] / "log_config" / "uvicorn.json"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _access_record(full_path: str, status: int = 200, method: str = "GET") -> logging.LogRecord:
    """A record shaped exactly like the one h11_impl/httptools_impl emit."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", method, full_path, "1.1", status),
        exc_info=None,
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/api/trips/{SLUG}", f"/api/trips/{REDACTED}"),
        (f"/api/trips/{SLUG}/stops", f"/api/trips/{REDACTED}/stops"),
        (
            f"/api/trips/{SLUG}/stops/abc-123/photos",
            f"/api/trips/{REDACTED}/stops/abc-123/photos",
        ),
        (f"/t/{SLUG}", f"/t/{REDACTED}"),
        (f"/t/{SLUG}/stops/abc-123", f"/t/{REDACTED}/stops/abc-123"),
        (f"/t/{SLUG}?tab=map", f"/t/{REDACTED}?tab=map"),
        (f"/api/trips/{SLUG}/stops?limit=10", f"/api/trips/{REDACTED}/stops?limit=10"),
        (f"/api/trips/{SLUG}?x=1", f"/api/trips/{REDACTED}?x=1"),
        # What the client sent, not what routing normalised, is what gets logged.
        (f"//api//trips/{SLUG}/bikes", f"//api//trips/{REDACTED}/bikes"),
        # A mistyped case 404s, but the slug it carries is still live.
        (f"/T/{SLUG}", f"/T/{REDACTED}"),
        (f"/API/Trips/{SLUG}/stops", f"/API/Trips/{REDACTED}/stops"),
    ],
)
def test_filter_redacts_slug_segment_and_keeps_the_rest(path: str, expected: str) -> None:
    record = _access_record(path, status=404)

    assert SlugRedactionFilter().filter(record) is True
    assert record.args == ("127.0.0.1:54321", "GET", expected, "1.1", 404)
    assert SLUG not in record.getMessage()


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/assets/x.js",
        "/",
        "/api/trips",  # no slug segment present
        "/api/trips/",
        "/t/",
        "/tours/x",  # prefix must be a whole segment
        "/api/tripsx/abc",
        "/assets/t/abc.js",  # only a leading prefix is slug-bearing
        "/api/health?probe=1",
    ],
)
def test_filter_leaves_non_slug_paths_unchanged(path: str) -> None:
    record = _access_record(path)
    original = record.args

    assert SlugRedactionFilter().filter(record) is True
    assert record.args == original


def test_filter_redacts_websocket_handshake_record() -> None:
    """uvicorn logs WS handshakes on `uvicorn.error` with args (client, path, status)."""
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        0,
        '%s - "WebSocket %s" %d',
        ("127.0.0.1:1", f"/api/trips/{SLUG}", 403),
        None,
    )
    SlugRedactionFilter().filter(record)
    assert record.getMessage() == f'127.0.0.1:1 - "WebSocket /api/trips/{REDACTED}" 403'


def test_filter_tolerates_records_without_tuple_args() -> None:
    record = logging.LogRecord("uvicorn", logging.INFO, __file__, 0, "Started", None, None)
    assert SlugRedactionFilter().filter(record) is True
    assert record.getMessage() == "Started"


def test_redact_path_only_touches_the_first_slug_segment() -> None:
    assert redact_path(f"/t/{SLUG}/t/other") == f"/t/{REDACTED}/t/other"


@pytest.fixture
def uvicorn_logging_via_config_file():
    """Apply the real config file exactly as `uvicorn --log-config` does; restore after."""
    saved = {
        name: (lg.handlers[:], lg.level, lg.propagate, lg.filters[:])
        for name in UVICORN_LOGGERS
        for lg in [logging.getLogger(name)]
    }
    # Config.__init__ calls configure_logging(), which dictConfig()s the .json file.
    Config(app="app.main:app", log_config=str(LOG_CONFIG_PATH), use_colors=False)
    yield
    for name, (handlers, level, propagate, filters) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate
        lg.filters[:] = filters


def _capture(logger_name: str) -> io.StringIO:
    """Point the configured handler(s) at a buffer, keeping their formatter and filters."""
    buffer = io.StringIO()
    for handler in logging.getLogger(logger_name).handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(buffer)
    return buffer


@pytest.mark.usefixtures("uvicorn_logging_via_config_file")
def test_config_file_redacts_access_log_output() -> None:
    buffer = _capture("uvicorn.access")

    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "GET",
        f"/api/trips/{SLUG}/stops?limit=5",
        "1.1",
        404,
    )
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "GET",
        "/api/health",
        "1.1",
        200,
    )

    output = buffer.getvalue()
    assert SLUG not in output
    assert f'"GET /api/trips/{REDACTED}/stops?limit=5 HTTP/1.1" 404' in output
    assert '"GET /api/health HTTP/1.1" 200' in output


@pytest.mark.usefixtures("uvicorn_logging_via_config_file")
def test_config_file_redacts_uvicorn_error_output() -> None:
    """The WS handshake line goes to `uvicorn.error` -> propagates to `uvicorn`'s handler."""
    buffer = _capture("uvicorn")

    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" %d', "127.0.0.1:1", f"/t/{SLUG}", 403
    )

    output = buffer.getvalue()
    assert SLUG not in output
    assert f'"WebSocket /t/{REDACTED}" 403' in output


def test_config_file_attaches_filter_to_every_handler() -> None:
    """Structural guard: a handler added to the file later must carry the filter too."""
    config = json.loads(LOG_CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["filters"]["redact_slug"]["()"] == "log_config.redaction.SlugRedactionFilter"
    for name, handler in config["handlers"].items():
        assert "redact_slug" in handler.get("filters", []), name
