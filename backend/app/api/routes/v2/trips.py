"""
Trip routes under ``/api/v2/trips`` (decision-log Entry 29).

**Context.** The v2 surface locates trips by id, not by slug. This module
starts with the anonymous Discover list, ``GET /api/v2/trips``
(``docs/api-contract.md``, "Notes per endpoint (v2)").

**How it works.**

- **Gate.** The list is anonymous and declares no gate: it reads no session, so
  every caller gets the same page. It lists public trips only, and the
  repository applies that filter before the cursor, so no cursor can reach a
  private trip. ``tests/test_route_dependency_audit.py`` exempts it by route
  name (``list_public_trips``).
- **Rate limit.** ``public-read``, declared first so it answers before anything
  touches the database. The HEAD sibling declares it too.
- **Cursor.** Keyset pagination on ``(lastPublicStopAt, id)``. The cursor is
  base64url (no padding) of the compact JSON ``[lastPublicStopAt|null, id]`` of
  the previous page's last row. It is opaque to the client and checked strictly
  on the way in: it must decode *and* re-encode to exactly the string sent, so
  any edit that doesn't produce another well-formed position is a ``422``. A
  well-formed edited cursor only moves the position within the public list.

**Related.** ``app/data/repositories/trips.py`` (``list_public``),
``app/models/trip.py`` (``TripPageOut``, ``TripSummaryOut``).
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.responses import PUBLIC_READ_429, error_responses
from app.core.errors import ApiError
from app.core.ratelimit import limit_public_read
from app.data.db import SessionDep
from app.data.repositories import trips as trips_repo
from app.models.trip import TripPageOut, TripSummaryOut

router = APIRouter(prefix="/trips", tags=["trips"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

BAD_CURSOR_MESSAGE = "cursor is not a valid page cursor; pass back a nextCursor unchanged."

Position = tuple[datetime | None, str]


def encode_cursor(position: Position) -> str:
    """base64url (unpadded) of the compact JSON ``[isoInstant|null, id]``."""
    at, trip_id = position
    raw = json.dumps([at.isoformat() if at is not None else None, trip_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> Position:
    """The position ``cursor`` encodes; ``ApiError.validation`` for anything else."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at_text, trip_id = json.loads(raw.decode("utf-8"))
        if not isinstance(trip_id, str) or not trip_id or "\x00" in trip_id:
            raise ValueError
        # JSON can spell a lone surrogate (`"\ud800"`), which is no UTF-8 text
        # Postgres could compare against: UnicodeEncodeError, so a 422.
        trip_id.encode("utf-8")
        if at_text is None:
            at = None
        elif isinstance(at_text, str):
            at = datetime.fromisoformat(at_text)
            if at.tzinfo is None:
                raise ValueError
        else:
            raise ValueError
        # Canonical only: whatever doesn't re-encode to exactly what was sent
        # was not issued by this server.
        if encode_cursor((at, trip_id)) != cursor:
            raise ValueError
        # The instant goes to the database in UTC. An offset that pushes it past
        # year 1 or 9999 overflows here (OverflowError), rather than in the
        # driver as a 500; anything that converts is inside timestamptz's range.
        if at is not None:
            at = at.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, RecursionError, binascii.Error, UnicodeError):
        raise ApiError.validation(BAD_CURSOR_MESSAGE) from None
    return at, trip_id


@router.get(
    "",
    dependencies=[Depends(limit_public_read)],
    summary="List public trips",
    response_description="One page of public trips, most recently active first.",
    responses=error_responses(
        {
            HTTPStatus.UNPROCESSABLE_ENTITY: "`limit` is outside 1-50, or `cursor` is not a "
            "`nextCursor` this endpoint issued.",
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** The Discover list: the public trips anyone can browse, with no
account (decision-log Entry 29). Private trips never appear.

**How it works.** Anonymous: a session, if sent, is ignored, and the response is
the same for every caller. Each item carries `riderCount` (active members,
leaders included) and `lastPublicStopAt`, the latest `arrivedAt` among stops
visible to the public — those with `arrivedAt <= now() - publicDelayHours` on the
server clock. Sorted by `lastPublicStopAt` descending with nulls last, then `id`
ascending; trips with no visible stop come last. Pages hold `limit` items
(default 20, 1-50). `nextCursor` is opaque; pass it back as `cursor` unchanged
for the next page. It is `null` on the last page. A cursor this endpoint did not
issue is a `422`.

**Related APIs.** `GET /api/v2/trips/{tripId}` for one trip,
`GET /api/v2/me/trips` for the trips the caller is a member of.
""",
)
async def list_public_trips(
    session: SessionDep,
    cursor: Annotated[
        str | None,
        Query(
            description="The `nextCursor` from the previous page, unchanged. Omit it for the "
            "first page. Anything this endpoint did not issue is rejected with 422 / "
            "VALIDATION_ERROR."
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE_SIZE,
            description="How many trips per page, from 1 to 50 inclusive; defaults to 20. "
            "Anything else is rejected with 422 / VALIDATION_ERROR.",
        ),
    ] = DEFAULT_PAGE_SIZE,
) -> TripPageOut:
    """One page of public trips, fetched with one extra row to learn whether more follow."""
    after = decode_cursor(cursor) if cursor is not None else None
    rows = await trips_repo.list_public(session, after=after, limit=limit + 1)
    page = rows[:limit]
    next_cursor = (
        encode_cursor((page[-1].last_public_stop_at, page[-1].id)) if len(rows) > limit else None
    )
    return TripPageOut(
        items=[
            TripSummaryOut(
                id=row.id,
                name=row.name,
                startDate=row.start_date,
                riderCount=row.rider_count,
                lastPublicStopAt=row.last_public_stop_at,
            )
            for row in page
        ],
        nextCursor=next_cursor,
    )


# HEAD sibling: the same handler, schema-excluded (decision-log Entry 11).
router.add_api_route(
    "",
    list_public_trips,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)
