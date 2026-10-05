"""
Member list and peer leadership under ``/api/v2/trips/{tripId}`` (decision-log Entry 29).

**Context.** A trip is led by its leaders, as peers: any leader can promote an
active rider, any leader can step down, and any member can leave, as long as
the trip keeps at least one leader (``docs/api-contract.md``, "Notes per
endpoint (v2)" → Members). Task ``t-am-trip-leadership``.

**How it works.**

- **Gates.** ``GET .../members`` declares ``require_trip_member_read`` (the
  writer gate's order, on a read: ``401`` → ``404`` → ``403``), because a member
  list must never reach a non-member. Promote and step-down declare
  ``require_trip_leader``; leave declares ``require_trip_writer_by_id``, so a
  rider can leave too.
- **Rate limits.** ``public-read`` on the GET and its schema-excluded HEAD
  sibling, ``writes`` on the three POSTs, each declared first.
- **The lock.** Every change runs in ``app/data/repositories/memberships.py``,
  which locks the trip row (``SELECT … FOR UPDATE``) before re-reading and
  counting the rows it decides on, so concurrent step-downs can't leave a trip
  with no leader. The repository returns an outcome; this module maps it to a
  status. Re-checked under the lock, a caller whose own row changed between the
  gate and the lock gets the answer the gate would now give.
- **Payload.** ``MemberOut`` only: user id, display name, role, joined-at.
  Never a username, a revoked row, or anything about sessions.

**Related.** ``app/core/security.py`` (the gates), ``app/models/member.py``,
``DELETE .../members/{userId}`` (revoke) and the join-request routes, which are
other tasks.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from app.api.responses import PATH_PARAMETERS_422, PUBLIC_READ_429, error_responses
from app.api.routes.v2.rider_writes import V2_TRIP_404, V2_WRITE_401, V2_WRITES_429
from app.core.errors import ApiError
from app.core.ratelimit import limit_public_read, limit_writes
from app.core.security import (
    NO_LONGER_A_RIDER_MESSAGE,
    NOT_A_LEADER_MESSAGE,
    TripWriterContext,
    require_trip_leader,
    require_trip_member_read,
    require_trip_writer_by_id,
)
from app.data.db import SessionDep
from app.data.repositories import memberships
from app.data.repositories.memberships import ChangeOutcome, ChangeResult
from app.models.member import MemberOut

router = APIRouter(prefix="/trips/{tripId}", tags=["trips"])

# The contract's 409 for the last leader stepping down or leaving, verbatim.
LAST_LEADER_MESSAGE = "Promote another rider to leader first"
# Promote on a target with no active membership on this trip.
MEMBER_NOT_FOUND_MESSAGE = "We couldn't find this member on this trip."

MEMBER_403 = (
    "Signed in, and the trip is visible to the caller (public, or they have a membership row "
    'on it), but they are not an active member: "You\'re not a rider on this trip." for a '
    'non-member of a public trip, "You\'re no longer a rider on this trip." for a revoked '
    "member."
)
LEADER_403 = (
    "Signed in, and the trip is visible to the caller (public, or they have a membership row "
    'on it), but they are not an active leader: "You\'re not a rider on this trip." for a '
    'non-member of a public trip, "You\'re no longer a rider on this trip." for a revoked '
    'member, "You\'re not a leader on this trip." for an active rider. Nothing was changed.'
)
LAST_LEADER_409 = (
    'The caller is the trip\'s only active leader: "Promote another rider to leader first". '
    "Checked under a lock on the trip row, so of two leaders stepping down at once, the second "
    "gets this. Nothing was changed; it fails the same way on retry until another leader exists."
)
LEADER_GATE = """`limit_writes` runs first, then `require_trip_leader`: a valid
session (401, for every trip id alike), then the trip (404 if it doesn't exist,
or if it is private and the caller has no membership row on it), then an active
**leader** membership (403)."""

MemberReadDep = Annotated[TripWriterContext, Depends(require_trip_member_read)]
LeaderDep = Annotated[TripWriterContext, Depends(require_trip_leader)]
WriterDep = Annotated[TripWriterContext, Depends(require_trip_writer_by_id)]


def _refused(result: ChangeResult) -> ApiError:
    """The error for a refused promote, step-down or leave, re-decided under the trip lock."""
    if result.outcome is ChangeOutcome.LAST_LEADER:
        return ApiError.conflict(LAST_LEADER_MESSAGE)
    if result.outcome is ChangeOutcome.NOT_A_LEADER:
        return ApiError.forbidden(NOT_A_LEADER_MESSAGE)
    return ApiError.forbidden(NO_LONGER_A_RIDER_MESSAGE)


# --------------------------------------------------------------------------
# Member list: `require_trip_member_read`, `public-read`, HEAD sibling
# --------------------------------------------------------------------------


async def list_members(context: MemberReadDep, session: SessionDep) -> list[MemberOut]:
    """The trip's active members, oldest first."""
    return await memberships.list_active(session, context.trip.id)


router.add_api_route(
    "/members",
    list_members,
    methods=["GET"],
    dependencies=[Depends(limit_public_read)],
    summary="List a trip's members",
    response_description="The trip's active members, in `joinedAt` order, oldest first.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
            "revoked, or the account is disabled. Checked **before** the trip is looked up, so "
            "the answer is the same for a public trip, a private trip and a trip id that "
            "doesn't exist. If a session cookie was sent it is cleared (`Max-Age=0`).",
            HTTPStatus.FORBIDDEN: MEMBER_403,
            HTTPStatus.NOT_FOUND: V2_TRIP_404,
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** Who is on the trip, for its members: riders get a read-only list,
leaders use it to promote (decision-log Entry 29). A member list is never shown
to anyone who is not an active member, on a public trip either.

**How it works.** `limit_public_read` runs first, then `require_trip_member_read`:
a valid session (401, for every trip id alike), then the trip (404 if it doesn't
exist, or if it is private and the caller has no membership row on it -- the
same 404 the v2 reads give), then an active membership as rider or leader (403).
Only active members are listed, oldest `joinedAt` first; revoked and departed
members are not. Each entry is the user id, display name, role and joined-at,
never a username.

**Related APIs.** `POST /api/v2/trips/{tripId}/members/{userId}/promote`,
`POST /api/v2/trips/{tripId}/step-down`, `POST /api/v2/trips/{tripId}/leave`.
""",
)
router.add_api_route(
    "/members",
    list_members,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)


# --------------------------------------------------------------------------
# Promote and step-down: `require_trip_leader`, `writes`
# --------------------------------------------------------------------------


@router.post(
    "/members/{userId}/promote",
    dependencies=[Depends(limit_writes)],
    summary="Promote a rider to leader",
    response_description="The member after the change, now a leader.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
            HTTPStatus.FORBIDDEN: LEADER_403,
            HTTPStatus.NOT_FOUND: "No trip has this id, or the trip is private and the caller "
            "has no membership row on it (byte-identical to a trip id that doesn't exist); "
            "**or** the trip is visible but `userId` has no active membership on it -- never a "
            "member, revoked, departed, or only a pending requester. Nothing was changed.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
        }
    ),
    description=f"""
**Context.** Leadership is shared by peers: any leader can make an active rider
a leader too (decision-log Entry 29).

**How it works.** {LEADER_GATE} Then, under a lock on the trip row, the target
must have an active membership on this trip, else 404. A rider becomes a
leader; promoting an existing leader (the caller included) is a 200 with
nothing changed, so a retry is safe.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/members` lists the user ids;
`POST /api/v2/trips/{{tripId}}/step-down` is the way back.
""",
)
async def promote_member(
    context: LeaderDep,
    session: SessionDep,
    user_id: Annotated[
        str,
        Path(
            alias="userId",
            description="The account id of the member to promote, as `MemberOut.userId` "
            "gives it. Must be an active member of this trip.",
        ),
    ],
) -> MemberOut:
    """Promote ``userId`` to leader; idempotent for an existing leader."""
    result = await memberships.promote(session, context.trip.id, context.user.user_id, user_id)
    if result.outcome is ChangeOutcome.NOT_FOUND:
        raise ApiError.not_found(MEMBER_NOT_FOUND_MESSAGE)
    if result.outcome is not ChangeOutcome.DONE:
        raise _refused(result)
    return result.member


@router.post(
    "/step-down",
    dependencies=[Depends(limit_writes)],
    summary="Step down from leading a trip",
    response_description="The caller's membership after the change, now a rider.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
            HTTPStatus.FORBIDDEN: LEADER_403,
            HTTPStatus.NOT_FOUND: V2_TRIP_404,
            HTTPStatus.CONFLICT: LAST_LEADER_409,
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
        }
    ),
    description=f"""
**Context.** A leader can hand back leadership and stay on the trip as a rider,
as long as another leader remains (decision-log Entry 29).

**How it works.** {LEADER_GATE} Then, under a lock on the trip row
(`SELECT … FOR UPDATE`), the caller's own row is re-read and the other active
leaders counted. None → 409 with nothing changed. Otherwise the caller becomes
a rider. Because every membership change takes the same lock, two leaders
stepping down at once can't both succeed on a two-leader trip.

**Related APIs.** `POST /api/v2/trips/{{tripId}}/members/{{userId}}/promote`
to add a leader first; `POST /api/v2/trips/{{tripId}}/leave` to leave entirely.
""",
)
async def step_down(context: LeaderDep, session: SessionDep) -> MemberOut:
    """Turn the caller from leader into rider, unless they are the last leader."""
    result = await memberships.step_down(session, context.trip.id, context.user.user_id)
    if result.outcome is not ChangeOutcome.DONE:
        raise _refused(result)
    return result.member


# --------------------------------------------------------------------------
# Leave: `require_trip_writer_by_id`, `writes`
# --------------------------------------------------------------------------


@router.post(
    "/leave",
    dependencies=[Depends(limit_writes)],
    status_code=HTTPStatus.NO_CONTENT,
    response_class=Response,
    summary="Leave a trip",
    response_description="The caller is no longer a member of the trip.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
            HTTPStatus.FORBIDDEN: MEMBER_403 + " Nothing was changed.",
            HTTPStatus.NOT_FOUND: V2_TRIP_404,
            HTTPStatus.CONFLICT: LAST_LEADER_409,
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
        }
    ),
    description="""
**Context.** Any member, rider or leader, can leave a trip on their own
(decision-log Entry 29). A trip must keep a leader, so the last one has to
promote someone first.

**How it works.** `limit_writes` runs first, then `require_trip_writer_by_id`:
a valid session (401, for every trip id alike), then the trip (404 if it
doesn't exist, or if it is private and the caller has no membership row on it),
then an active membership as rider or leader (403). Then, under a lock on the
trip row, a leader with no other active leader gets 409 with nothing changed.
Otherwise the membership's `revoked_at` is set with `revoked_by` = the caller;
the row is kept. Leaving starts no join-request cooldown. Afterwards the caller
is `none` on the trip: a private trip answers them 404 on reads.

**Related APIs.** `POST /api/v2/trips/{tripId}/step-down` to stay as a rider
instead; `POST /api/v2/trips/{tripId}/join-requests` to ask to rejoin.
""",
)
async def leave_trip(context: WriterDep, session: SessionDep) -> None:
    """End the caller's own membership, unless they are the last leader."""
    result = await memberships.leave(session, context.trip.id, context.user.user_id)
    if result.outcome is not ChangeOutcome.DONE:
        raise _refused(result)
