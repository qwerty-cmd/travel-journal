"""
Trip membership reads — the ``trip_members`` table from migration 0003 (decision-log Entry 29).

**Context.** Under Entry 29 a slug only *locates* a trip. Whether the caller may
write to it is answered by their row in ``trip_members``, read here on every
request (``docs/api-contract.md``, "Access: public trips, members and
leaders" → The gates). This module is the read side. The writes (claim approval,
promote, revoke, leave) arrive with the tasks that build those endpoints.

**How it works.** SQLAlchemy Core against ``app/data/tables.py``.

- **No cache, ever.** Every call is one query. A revocation therefore takes
  effect on the caller's very next request, which is the contract's rule
  ("Membership is read from Postgres on every request, with no cache").
- **Revoked is not "never a member".** A revoked row is kept as history
  (``revoked_at`` set, ``ux_trip_members_active`` only covers active rows), and
  the gate answers a revoked member with a different ``403`` message from a
  stranger. So ``get_for_user`` returns the active row when there is one, and
  otherwise the most recently revoked row, rather than ``None``.
- **It never raises ``ApiError``.** No row is ``None``; what that means for the
  request is ``app/core/security.py``'s decision.

**Related.** ``app/core/security.py`` (``require_trip_writer``,
``require_trip_access``), ``app/data/repositories/sessions.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import trip_members
from app.models.member import MemberRole


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """
    One user's standing on one trip.

    ``active`` is False for a revoked (or self-departed) membership. ``role`` is
    the role that row carried, which for a revoked row is history only and grants
    nothing.
    """

    role: MemberRole
    active: bool


async def get_for_user(
    session: AsyncSession, trip_id: str, user_id: str
) -> MembershipRecord | None:
    """
    ``user_id``'s membership on ``trip_id``: the active row, else the latest revoked one, else None.

    One query. Active rows sort first (``revoked_at IS NULL``), then revoked
    rows newest first, and only the first row is read. There is at most one
    active row per (trip, user) (``ux_trip_members_active``), so the answer is
    never ambiguous.
    """
    row = (
        await session.execute(
            select(trip_members.c.role, trip_members.c.revoked_at)
            .where(trip_members.c.trip_id == trip_id, trip_members.c.user_id == user_id)
            .order_by(
                trip_members.c.revoked_at.is_(None).desc(),
                trip_members.c.revoked_at.desc(),
            )
            .limit(1)
        )
    ).first()

    if row is None:
        return None

    return MembershipRecord(role=MemberRole(row.role), active=row.revoked_at is None)
