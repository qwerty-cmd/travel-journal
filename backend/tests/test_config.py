"""
``Settings`` validation errors must never echo env values.

A process that starts with a required env var missing prints pydantic's
``ValidationError`` to stderr, which on Container Apps lands in platform logs.
Without ``hide_input_in_errors=True`` that text carries ``input_value`` tails of
the other fields, including the ``GRAPH_*`` secrets.
"""

from __future__ import annotations

import pydantic
import pytest

from app.core.config import Settings

CLIENT_SECRET = "graph-client-secret-QX7rT2mK9vLp4WzN"
REFRESH_TOKEN = "graph-refresh-token-HB3sJ8dF6gYc1RuE"


def test_missing_required_var_error_names_field_but_hides_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("GRAPH_REFRESH_TOKEN", REFRESH_TOKEN)
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("S3_BUCKET_NAME", "bike-trip-photos")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(pydantic.ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]

    text = str(exc.value)
    assert "database_url" in text
    # Distinctive middle and tail fragments of each secret (8+ chars), so a
    # truncated repr of either value is still caught.
    for fragment in (
        "secret-QX7r",
        "mK9vLp4WzN",
        "token-HB3sJ8",
        "dF6gYc1RuE",
    ):
        assert fragment not in text
