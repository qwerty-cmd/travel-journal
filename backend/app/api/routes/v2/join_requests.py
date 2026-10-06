"""
The requester's side of per-person join requests (decision-log Entry 29 §5).

**Context.** A signed-in user asks to ride on a trip, can withdraw the request,
and sees where their requests stand (``docs/api-contract.md``, "Join request
create", "Cancel", "Me / join-requests"). A leader's decision is another task
(``t-am-join-leader``). Task ``t-am-join-requester``.

**How it works.**

- **Gates.** ``POST /api/v2/trips/{tripId}/join-requests`` declares
  ``require_trip_join_requester``: session (``401``), then the reader's rule,
  so a private trip the caller isn't active on is the byte-identical ``404``
  and can't be joined by id. Cancel and the list are about the caller's own
  requests, so they declare ``require_session`` only; someone else's request
  is the same ``404`` as an unknown id.
- **Rate limits**, each declared first: ``join`` on the create, ``writes`` on
  cancel, ``public-read`` on the list and its schema-excluded HEAD sibling.
- **Rules under lock.** ``app/data/repositories/join_requests.py`` decides
  duplicates, blocks, cooldowns and caps under a lock on the trip row and the
  caller's user row, and returns an outcome; this module maps it to a status
  and message. The 409 messages are written for the requester (the UI shows
  them verbatim, ``docs/design/screens/join-request.md``) and carry nothing
  from any other record. A block reads as a rejection, as ``/me/join-requests``
  reports it.
- **One clock.** ``_utcnow`` is the request's time for the 7-day cooldown and
  the timestamps written; a test replaces it to move time.

**Related.** ``app/core/security.py`` (the gates), ``app/models/join_request.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from app.api.responses import PATH_PARAMETERS_422, PUBLIC_READ_429, error_responses
from app.api.routes.v2.rider_writes import V2_WRITE_401, V2_WRITES_429
from app.core.errors import ApiError
from app.core.ratelimit import limit_join, limit_public_read, limit_writes
from app.core.security import (
    TripJoinRequesterContext,
    require_session,
    require_trip_join_requester,
)
from app.core.sessions import SessionUser
from app.data.db import SessionDep
from app.data.repositories import join_requests as join_repo
from app.data.repositories.join_requests import (
    CancelOutcome,
    CreateOutcome,
    JoinRequestRecord,
)
from app.models.join_request import JoinRequestCreate, JoinRequestState, MyJoinRequestOut

router = APIRouter(tags=["join-requests"])

# The 409 messages (contract, "Join request create"; DESIGN.md X5: dev words them).
ALREADY_A_MEMBER_MESSAGE = "You're already on this trip."
# Blocked and rejected-within-7-days share one message: a requester is never told
# they were blocked (`/me/join-requests` reports blocked as rejected).
NOT_APPROVED_MESSAGE = (
    "A leader didn't approve your last request to join this trip, so you can't ask again yet."
)
REVOKED_RECENTLY_MESSAGE = (
    "You were removed from this trip less than 7 days ago. You can ask to join again "
    "7 days after you were removed."
)
USER_CAP_MESSAGE = (
    "You already have 20 requests waiting for a decision. Cancel one, or wait for a "
    "leader to decide, before asking to join another trip."
)
TRIP_CAP_MESSAGE = "This trip has too many requests waiting for a decision. Please try again later."
# Cancel.
JOIN_REQUEST_NOT_FOUND_MESSAGE = "We couldn't find this join request."
ALREADY_DECIDED_MESSAGE = "A leader has already decided this request, so it can't be cancelled."

_REFUSALS = {
    CreateOutcome.ACTIVE_MEMBER: ALREADY_A_MEMBER_MESSAGE,
    CreateOutcome.BLOCKED: NOT_APPROVED_MESSAGE,
    CreateOutcome.REJECTED_RECENTLY: NOT_APPROVED_MESSAGE,
    CreateOutcome.REVOKED_RECENTLY: REVOKED_RECENTLY_MESSAGE,
    CreateOutcome.USER_CAP: USER_CAP_MESSAGE,
    CreateOutcome.TRIP_CAP: TRIP_CAP_MESSAGE,
}

SESSION_401 = (
    "No valid session: none sent, expired, signed out or revoked, or the account is disabled. "
    "If a session cookie was sent it is cleared (`Max-Age=0`)."
)


def _utcnow() -> datetime:
    """The request's clock. One function, so a test can move time forward."""
    return datetime.now(UTC)


def _out(record: JoinRequestRecord) -> MyJoinRequestOut:
    """A request as its requester sees it: ``blocked`` is reported as ``rejected``."""
    state = JoinRequestState(record.state)
    if state is JoinRequestState.BLOCKED:
        state = JoinRequestState.REJECTED
    return MyJoinRequestOut(
        id=record.id,
        tripId=record.trip_id,
        tripName=record.trip_name,
        state=state,
        message=record.message,
        createdAt=record.created_at,
    )


# --------------------------------------------------------------------------
# Create: `require_trip_join_requester`, `join`
# --------------------------------------------------------------------------


@router.post(
    "/trips/{tripId}/join-requests",
    dependencies=[Depends(limit_join)],
    status_code=HTTPStatus.CREATED,
    summary="Ask to join a trip",
    response_description="The new pending request (201).",
    responses={
        HTTPStatus.OK: {
            "model": MyJoinRequestOut,
            "description": "**Not a second request.** The caller already has a pending "
            "request on this trip, so nothing was created and that request is returned "
            "unchanged -- its `message` too, even where this request sent a different one.",
        },
        **error_responses(
            {
                HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
                HTTPStatus.NOT_FOUND: "No trip has this id, **or** the trip is private and the "
                "caller is not an active member of it. The two are byte-identical, so a trip id "
                "can't be used to learn whether a private trip exists. Private trips can't be "
                "joined by id. Nothing was written.",
                HTTPStatus.CONFLICT: "The caller may not ask to join this trip now: they are "
                "already an active member (leaders included); a leader blocked them, or "
                "rejected a request of theirs less than 7 days ago; someone else removed them "
                "from the trip less than 7 days ago (leaving on their own doesn't count); they "
                "already have 20 pending requests; or the trip already has 100. The message is "
                "written to be shown verbatim, and reports a block as a rejection. Nothing was "
                "written. Never-retry.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed validation: `message` is "
                "over 280 characters after trimming, or is not a string.",
                HTTPStatus.TOO_MANY_REQUESTS: "The `join` limit: 10 requests an hour per "
                "account, or per client address for a request with no session. Checked before "
                "the session and the trip, so nothing was written. Retry after `Retry-After` "
                "seconds.",
            }
        ),
    },
    description="""
**Context.** How a signed-in user asks a public trip's leaders to let them ride
(decision-log Entry 29). A request never grants anything by itself; a leader
decides it.

**How it works.** `limit_join` runs first (10 an hour per account), then
`require_trip_join_requester`: a valid session (401, for every trip id alike),
then the trip (404 if it doesn't exist, or if it is private and the caller is
not an active member of it). Then, under a lock on the trip and on the caller's
account: a pending request already there is returned with `200`, unchanged.
Otherwise `409` for an active member, a block, a rejection or a revocation by
someone else less than 7 days ago, 20 pending requests of the caller's, or 100
pending on the trip. Otherwise a pending request is stored with the trimmed
`message` (empty → null) and returned with `201`.

**Related APIs.** `POST /api/v2/join-requests/{requestId}/cancel` withdraws it;
`GET /api/v2/me/join-requests` lists the caller's requests;
`GET /api/v2/trips/{tripId}` reports `viewer.role` `pending` meanwhile.
""",
)
async def create_join_request(
    context: Annotated[TripJoinRequesterContext, Depends(require_trip_join_requester)],
    session: SessionDep,
    body: JoinRequestCreate,
    response: Response,
) -> MyJoinRequestOut:
    """Make the caller's pending request on the trip, or hand back the one already pending."""
    result = await join_repo.create(
        session,
        trip_id=context.trip.id,
        user_id=context.user.user_id,
        message=body.message,
        now=_utcnow(),
    )
    if result.outcome in _REFUSALS:
        raise ApiError.conflict(_REFUSALS[result.outcome])
    if result.outcome is CreateOutcome.EXISTING:
        response.status_code = HTTPStatus.OK
    return _out(result.request)


# --------------------------------------------------------------------------
# Cancel: `require_session`, `writes`
# --------------------------------------------------------------------------


@router.post(
    "/join-requests/{requestId}/cancel",
    dependencies=[Depends(limit_writes)],
    summary="Cancel your join request",
    response_description="The request, now cancelled.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: SESSION_401 + " Nothing was changed.",
            HTTPStatus.NOT_FOUND: "No join request has this id, **or** it is someone else's. "
            "The two are identical. Nothing was changed.",
            HTTPStatus.CONFLICT: "A leader has already decided the request (approved, "
            "rejected or blocked), so it can't be cancelled. Nothing was changed.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
        }
    ),
    description="""
**Context.** A requester can withdraw a request a leader hasn't decided yet
(decision-log Entry 29).

**How it works.** `limit_writes` runs first, then `require_session` (401). The
caller's own pending request becomes `cancelled`; one already cancelled is a
`200` with nothing changed, so a retry is safe. A decided request is `409`.
Someone else's request and an unknown id are the same `404`. Cancelling starts
no cooldown: the caller may ask again at once.

**Related APIs.** `POST /api/v2/trips/{tripId}/join-requests` to ask again;
`GET /api/v2/me/join-requests` lists the caller's requests.
""",
)
async def cancel_join_request(
    user: Annotated[SessionUser, Depends(require_session)],
    session: SessionDep,
    request_id: Annotated[
        str,
        Path(
            alias="requestId",
            description="The join request's id, as `MyJoinRequestOut.id` gives it. Must be "
            "one of the caller's own requests.",
        ),
    ],
) -> MyJoinRequestOut:
    """Cancel the caller's own pending request; idempotent once cancelled."""
    result = await join_repo.cancel(
        session, request_id=request_id, user_id=user.user_id, now=_utcnow()
    )
    if result.outcome is CancelOutcome.NOT_FOUND:
        raise ApiError.not_found(JOIN_REQUEST_NOT_FOUND_MESSAGE)
    if result.outcome is CancelOutcome.DECIDED:
        raise ApiError.conflict(ALREADY_DECIDED_MESSAGE)
    return _out(result.request)


# --------------------------------------------------------------------------
# The caller's own list: `require_session`, `public-read`, HEAD sibling
# --------------------------------------------------------------------------


@router.get(
    "/me/join-requests",
    dependencies=[Depends(limit_public_read)],
    summary="The caller's join requests",
    response_description="The caller's join requests, newest first, at most 100.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: SESSION_401,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** Where a requester sees how their requests stand (decision-log
Entry 29): waiting, approved, not approved or cancelled.

**How it works.** `limit_public_read` runs first, then `require_session` (401).
Lists every request the caller has made, in any state, newest `createdAt` first,
at most 100, each with its trip's name. A request a leader blocked is reported
as `rejected`, and no cooldown date is given. An account with no requests gets
`200` with `[]`.

**Related APIs.** `POST /api/v2/trips/{tripId}/join-requests` makes one;
`POST /api/v2/join-requests/{requestId}/cancel` withdraws one;
`GET /api/v2/me/trips` lists the trips the caller is already on.
""",
)
async def list_my_join_requests(
    user: Annotated[SessionUser, Depends(require_session)], session: SessionDep
) -> list[MyJoinRequestOut]:
    """The caller's own requests, newest first, blocked reported as rejected."""
    return [_out(record) for record in await join_repo.list_for_user(session, user.user_id)]


# HEAD sibling: the same handler, schema-excluded (decision-log Entry 11).
router.add_api_route(
    "/me/join-requests",
    list_my_join_requests,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)
