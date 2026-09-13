"""
Trip reads — the query behind every request in the API.

Every one of the contract's eight endpoints is scoped by ``{slug}``, so
"which trip does this slug belong to" runs before anything else happens
(``docs/api-contract.md``, "Access: two slugs, no accounts"). This module owns
that query; ``app/core/security.py`` owns what the answer *means*.

Two boundaries this module holds deliberately:

- **It does not return an ``Access``.** Which of the two slugs was presented is
  a property of the *request*, not of the stored row — the row carries both. A
  repository that returned "rider" or "viewer" would be reporting a fact about
  an HTTP request it never saw, and would have to be called differently from a
  background job or a seed script that legitimately has no requester at all.
  Callers derive access by comparing the slug they were given against
  ``rider_slug`` / ``viewer_slug`` on the record.
- **It never raises ``ApiError``.** "No row matched" is a plain ``None``.
  Turning that into a 404 is a transport decision, and the 403-vs-404
  distinction it belongs to is enforced in one place (``core/security.py``)
  rather than in every module that can fail to find something. Anything in
  ``data/`` raising an HTTP-shaped error would also make this layer unusable
  outside a request.

SQLAlchemy Core against ``app/data/tables.py``, per that module's "Core, not
ORM" note. This is also the only layer allowed to name database columns: what
leaves here is a typed ``TripRecord``, never a ``Row`` the caller indexes by
string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import trips


@dataclass(frozen=True, slots=True)
class TripRecord:
    """
    One row of ``trips``, as a typed value rather than a database row.

    Frozen because it is read-only by construction: nothing in the request path
    that resolves a slug also edits the trip, and a mutable copy invites a
    caller to "fix" a slug in memory and be surprised the database disagrees.

    Both slugs are carried, not just the one that matched, because the caller
    needs both to work out which kind of link it was handed. That is also why
    this is not a Pydantic response model — ``TripOut`` deliberately has no slug
    fields, and these values must never be serialised into a response.
    """

    id: str
    name: str
    start_date: date
    rider_slug: str
    viewer_slug: str


async def get_by_slug(session: AsyncSession, slug: str) -> TripRecord | None:
    """
    The trip whose rider **or** viewer slug is ``slug``; ``None`` if there is none.

    One query over both columns rather than two round trips, and the caller is
    told nothing about *which* column matched — see the module docstring. Both
    slug columns are UNIQUE (``migrations/0001_initial_schema.sql``), so a slug
    identifies at most one trip through either column.

    ``None`` is a legitimate answer here, not an error: a mistyped link and a
    revoked one both land on it, and only the HTTP layer knows what to do about
    that. A slug Postgres cannot even represent is one more way of being no
    trip's slug — see the NUL guard below.
    """
    # A NUL byte cannot be a value of a Postgres `text` column at all: the
    # parameter is rejected by the server before any row is examined
    # (`invalid byte sequence for encoding "UTF8": 0x00`). This is reachable
    # from outside — uvicorn percent-decodes `%00` straight into the path
    # parameter, so `GET /api/trips/abc%00def` arrives here as `"abc\x00def"`.
    #
    # Guarded rather than allowed to raise, for two reasons:
    #
    # - **The layering.** A `DBAPIError` escaping this function would break the
    #   module docstring's promise that nothing in `data/` raises a transport
    #   error, and would move the 404 decision out of `core/security.py`, which
    #   is meant to be the single place "nothing resolved" becomes a status.
    # - **The error code, which is the load-bearing part.** An escaping driver
    #   error renders as `500 INTERNAL_ERROR`, and `code` is what the offline
    #   queue branches on to decide retry-vs-never-retry (decision-log entry 6).
    #   `INTERNAL_ERROR` means "the server broke, try again later", so a queued
    #   write against such a URL would retry forever on a request that can never
    #   succeed. `404 NOT_FOUND` is both true and terminal.
    #
    # No slug can contain a NUL, so returning `None` is not a shortcut around a
    # lookup that might have matched: it is the answer the lookup would give if
    # the database could be asked.
    if "\x00" in slug:
        return None

    statement = select(
        trips.c.id,
        trips.c.name,
        trips.c.start_date,
        trips.c.rider_slug,
        trips.c.viewer_slug,
    ).where(or_(trips.c.rider_slug == slug, trips.c.viewer_slug == slug))

    row = (await session.execute(statement)).first()
    if row is None:
        return None

    return TripRecord(
        id=row.id,
        name=row.name,
        start_date=row.start_date,
        rider_slug=row.rider_slug,
        viewer_slug=row.viewer_slug,
    )
