"""normalize_database_url: scheme rewrite + libpq SSL params -> asyncpg kwargs.

Neon's console string (`?sslmode=require&channel_binding=require`) used to reach
asyncpg.connect() verbatim and fail with TypeError. No real connection here.
"""

import inspect
from urllib.parse import parse_qsl, urlsplit

import asyncpg
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect
from sqlalchemy.engine import make_url

from app.data.db import normalize_database_url

NEON = (
    "postgresql://user:p%40ss@ep-x-123.eu-central-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


def _query(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def test_neon_url_translates_sslmode_and_drops_channel_binding():
    out = normalize_database_url(NEON)
    assert out.startswith(
        "postgresql+asyncpg://user:p%40ss@ep-x-123.eu-central-1.aws.neon.tech/neondb?"
    )
    assert _query(out) == [("ssl", "require")]


@pytest.mark.parametrize(
    "mode", ["require", "verify-ca", "verify-full", "prefer", "allow", "disable"]
)
def test_sslmode_only_becomes_ssl(mode):
    out = normalize_database_url(f"postgresql://u:p@h/db?sslmode={mode}")
    assert out == f"postgresql+asyncpg://u:p@h/db?ssl={mode}"


def test_channel_binding_only_is_dropped():
    out = normalize_database_url("postgresql://u:p@h/db?channel_binding=require")
    assert _query(out) == []
    assert "channel_binding" not in out


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql+asyncpg://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_no_query_string_behaviour_unchanged(given, expected):
    assert normalize_database_url(given) == expected


def test_other_params_preserved_in_order():
    out = normalize_database_url(
        "postgres://u:p@h/db?application_name=a%20b&sslmode=require"
        "&channel_binding=require&command_timeout=10&empty="
    )
    assert out.startswith("postgresql+asyncpg://u:p@h/db?")
    assert _query(out) == [
        ("application_name", "a b"),
        ("ssl", "require"),
        ("command_timeout", "10"),
        ("empty", ""),
    ]


def test_already_asyncpg_url_still_gets_query_fixed():
    url = "postgresql+asyncpg://u:p@h/db?sslmode=verify-full&channel_binding=require"
    out = normalize_database_url(url)
    assert out == "postgresql+asyncpg://u:p@h/db?ssl=verify-full"


def test_asyncpg_dialect_receives_only_kwargs_asyncpg_accepts():
    url = make_url(normalize_database_url(NEON))
    _, kwargs = asyncpg_dialect().create_connect_args(url)
    assert "sslmode" not in kwargs
    assert "channel_binding" not in kwargs
    assert kwargs["ssl"] == "require"
    assert kwargs["password"] == "p@ss"  # userinfo untouched
    accepted = inspect.signature(asyncpg.connect).parameters
    assert set(kwargs) <= set(accepted), set(kwargs) - set(accepted)
