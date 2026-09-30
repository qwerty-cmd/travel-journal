"""
Join requests -- the ``join_requests`` table from migration 0003 (decision-log Entry 29).

**Context.** A signed-in user asks to ride on a trip; a leader decides. This
module starts with the one read the trip view needs before any request can be
made through the API: whether the caller has a pending request on a trip, which
is what makes ``TripOut.viewer.role`` ``pending`` (``docs/api-contract.md``,
"Access: public trips, members and leaders"). The write side (create, cancel,
decide, unblock) arrives with ``t-am-join-requester`` and the leader tasks.

**How it works.** SQLAlchemy Core against ``app/data/tables.py``. One query per
call and no cache, like ``memberships.py``. It never raises ``ApiError``: what a
pending request means for a response is ``app/core/security.py``'s decision.

**Related.** ``app/data/repositories/memberships.py`` (the ``trip_members``
reads; a different table, hence a different module).
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import join_requests


async def has_pending(session: AsyncSession, trip_id: str, user_id: str) -> bool:
    """
    True when ``user_id`` has a pending join request on ``trip_id``.

    At most one can exist (``ux_join_requests_one_pending``). Decided,
    cancelled and blocked requests are not pending, so they answer ``False``.
    """
    return bool(
        await session.scalar(
            select(
                exists().where(
                    join_requests.c.trip_id == trip_id,
                    join_requests.c.user_id == user_id,
                    join_requests.c.state == "pending",
                )
            )
        )
    )
