"""
Per-person join requests, both sides (decision-log Entry 29 §5).

**Context.** A signed-in user asks to ride on a trip, can withdraw the request,
and sees where their requests stand (``docs/api-contract.md``, "Join request
create", "Cancel", "Me / join-requests"; task ``t-am-join-requester``). The
trip's leaders list the requests, decide them and lift blocks ("Trip
join-requests (leader)", "Decision", "Unblock"; task ``t-am-join-leader``).

**How it works.**

- **Gates.** ``POST /api/v2/trips/{tripId}/join-requests`` declares
  ``require_trip_join_requester``: session (``401``), then the reader's rule,
  so a private trip the caller isn't active on is the byte-identical ``404``
  and can't be joined by id. Cancel and the list are about the caller's own
  requests, so they declare ``require_session`` only; someone else's request
  is the same ``404`` as an unknown id. The leader list, decision and unblock
  declare ``require_trip_leader``; a request id on another trip is the same
  ``404`` as an unknown one.
- **Rate limits**, each declared first: ``join`` on the create, ``writes`` on
  cancel, decision and unblock, ``public-read`` on the two lists and their
  schema-excluded HEAD siblings.
- **Rules under lock.** ``app/data/repositories/join_requests.py`` decides
  duplicates, blocks, cooldowns and caps under a lock on the trip row and the
  caller's user row, and returns an outcome; this module maps it to a status
  and message. The 409 messages are written for the requester (the UI shows
  them verbatim, ``docs/design/screens/join-request.md``) and carry nothing
  from any other record. A block reads as a rejection, as ``/me/join-requests``
  reports it. The leader routes re-check the caller's leadership under the
  trip lock and answer as the gate would now (``403``); their payload is
  ``TripJoinRequestOut``, the requester's id and display name, never a
  username.
- **One clock.** ``_utcnow`` is the request's time for the 7-day cooldown and
  the timestamps written; a test replaces it to move time.

**Related.** ``app/core/security.py`` (the gates), ``app/models/join_request.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.responses import PATH_PARAMETERS_422, PUBLIC_READ_429, error_responses
from app.api.routes.v2.members import LEADER_403
from app.api.routes.v2.rider_writes import V2_TRIP_404, V2_WRITE_401, V2_WRITES_429
from app.core.errors import ApiError
from app.core.ratelimit import limit_join, limit_public_read, limit_writes
from app.core.security import (
    NO_LONGER_A_RIDER_MESSAGE,
    NOT_A_LEADER_MESSAGE,
    TripJoinRequesterContext,
    TripWriterContext,
    require_session,
    require_trip_join_requester,
    require_trip_leader,
)
from app.core.sessions import SessionUser
from app.data.db import SessionDep
from app.data.repositories import join_requests as join_repo
from app.data.repositories.join_requests import (
    CancelOutcome,
    CreateOutcome,
    JoinRequestRecord,
    LeaderOutcome,
    LeaderResult,
    TripJoinRequestRecord,
)
from app.models.join_request import (
    JoinDecisionCreate,
    JoinRequestCreate,
    JoinRequestState,
    JoinRequestVia,
    MyJoinRequestOut,
    PersonOut,
    TripJoinRequestOut,
)

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


# --------------------------------------------------------------------------
# The leader's side: list, decide, unblock (t-am-join-leader)
# --------------------------------------------------------------------------

# Decide on a request already decided another way, or withdrawn by its requester.
DECIDED_OTHERWISE_MESSAGE = (
    "This request has already been decided another way, or withdrawn by the person who sent it."
)
# Unblock on a request that isn't blocked.
NOT_BLOCKED_MESSAGE = "This request isn't blocked, so there's nothing to unblock."

LEADER_REQUEST_404 = (
    "No trip has this id, or the trip is private and the caller has no membership row on it "
    "(byte-identical to a trip id that doesn't exist); **or** the trip is visible but no join "
    "request with this id is on it -- an unknown id and another trip's request are the same "
    "404. Nothing was changed."
)

LeaderDep = Annotated[TripWriterContext, Depends(require_trip_leader)]
RequestIdPath = Annotated[
    str,
    Path(
        alias="requestId",
        description="The join request's id, as `TripJoinRequestOut.id` gives it. Must be a "
        "request on this trip.",
    ),
]


def _trip_out(record: TripJoinRequestRecord) -> TripJoinRequestOut:
    """A request as the trip's leaders see it: requester id and display name, never a username."""
    return TripJoinRequestOut(
        id=record.id,
        requester=PersonOut(userId=record.user_id, displayName=record.display_name),
        state=JoinRequestState(record.state),
        via=JoinRequestVia(record.via),
        message=record.message,
        createdAt=record.created_at,
    )


def _leader_result(result: LeaderResult, conflict_message: str) -> TripJoinRequestOut:
    """Map a decide/unblock outcome, re-decided under the trip lock, to a response or error."""
    if result.outcome is LeaderOutcome.NOT_FOUND:
        raise ApiError.not_found(JOIN_REQUEST_NOT_FOUND_MESSAGE)
    if result.outcome is LeaderOutcome.CONFLICT:
        raise ApiError.conflict(conflict_message)
    if result.outcome is LeaderOutcome.NOT_A_LEADER:
        raise ApiError.forbidden(NOT_A_LEADER_MESSAGE)
    if result.outcome is LeaderOutcome.NOT_A_MEMBER:
        raise ApiError.forbidden(NO_LONGER_A_RIDER_MESSAGE)
    return _trip_out(result.request)


async def list_trip_join_requests(
    context: LeaderDep,
    session: SessionDep,
    state: Annotated[
        Literal["pending", "blocked"],
        Query(
            description="Which requests to list: 'pending' (the default) awaiting a decision, "
            "or 'blocked' so a leader can unblock them. Any other value is a 422."
        ),
    ] = "pending",
) -> list[TripJoinRequestOut]:
    """The trip's join requests in ``state``, oldest first."""
    records = await join_repo.list_for_trip(session, context.trip.id, state)
    return [_trip_out(record) for record in records]


router.add_api_route(
    "/trips/{tripId}/join-requests",
    list_trip_join_requests,
    methods=["GET"],
    dependencies=[Depends(limit_public_read)],
    summary="List a trip's join requests",
    response_description="The trip's requests in the asked-for state, oldest `createdAt` first.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: SESSION_401 + " Checked **before** the trip is looked up, "
            "so the answer is the same for a public trip, a private trip and a trip id that "
            "doesn't exist.",
            HTTPStatus.FORBIDDEN: LEADER_403.removesuffix(" Nothing was changed."),
            HTTPStatus.NOT_FOUND: V2_TRIP_404,
            HTTPStatus.UNPROCESSABLE_ENTITY: "`state` is neither `pending` nor `blocked`.",
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** Where a trip's leaders review who is asking to ride, and find the
people they blocked (decision-log Entry 29).

**How it works.** `limit_public_read` runs first, then `require_trip_leader`: a
valid session (401, for every trip id alike), then the trip (404 if it doesn't
exist, or if it is private and the caller has no membership row on it), then an
active **leader** membership (403). Lists the trip's requests in `state`
(`pending` by default, or `blocked`), oldest `createdAt` first. Each shows the
requester's user id and display name, never a username, plus `message` and
`via`. No matching requests is `200` with `[]`.

**Related APIs.** `POST /api/v2/trips/{tripId}/join-requests/{requestId}/decision`
decides one; `POST /api/v2/trips/{tripId}/join-requests/{requestId}/unblock`
lifts a block.
""",
)
# HEAD sibling: the same handler, schema-excluded (decision-log Entry 11).
router.add_api_route(
    "/trips/{tripId}/join-requests",
    list_trip_join_requests,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)


@router.post(
    "/trips/{tripId}/join-requests/{requestId}/decision",
    dependencies=[Depends(limit_writes)],
    summary="Decide a join request",
    response_description="The request after the decision.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
            HTTPStatus.FORBIDDEN: LEADER_403,
            HTTPStatus.NOT_FOUND: LEADER_REQUEST_404,
            HTTPStatus.CONFLICT: "The request was already decided another way, or withdrawn "
            "by its requester. Nothing was changed; it fails the same way on retry.",
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed validation: `action` is not "
            "one of `approve`, `reject`, `reject_and_block`.",
            HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
        }
    ),
    description="""
**Context.** How a trip's leaders let someone ride, or refuse them
(decision-log Entry 29). Bulk approve and reject is the UI looping over this.

**How it works.** `limit_writes` runs first, then `require_trip_leader`: a
valid session (401, for every trip id alike), then the trip (404 if it doesn't
exist, or if it is private and the caller has no membership row on it), then an
active **leader** membership (403). Then, under a lock on the trip row and then
the request: a request not on this trip is 404. A pending request moves to
`approved`, `rejected` or `blocked`, recording when and by whom; `approve` adds
the requester as an active rider in the same transaction (nothing more if they
already are one). Repeating the decision that produced the current state is a
`200` with nothing changed; any other decision on a decided or withdrawn
request is `409`. A rejection keeps the requester out for 7 days; a block,
until a leader unblocks.

**Related APIs.** `GET /api/v2/trips/{tripId}/join-requests` lists the requests;
`POST /api/v2/trips/{tripId}/join-requests/{requestId}/unblock` lifts a block.
""",
)
async def decide_join_request(
    context: LeaderDep,
    session: SessionDep,
    request_id: RequestIdPath,
    body: JoinDecisionCreate,
) -> TripJoinRequestOut:
    """Approve, reject or block a request on the trip; idempotent for the same decision."""
    result = await join_repo.decide(
        session,
        trip_id=context.trip.id,
        request_id=request_id,
        caller_id=context.user.user_id,
        action=body.action.value,
        now=_utcnow(),
    )
    return _leader_result(result, DECIDED_OTHERWISE_MESSAGE)


@router.post(
    "/trips/{tripId}/join-requests/{requestId}/unblock",
    dependencies=[Depends(limit_writes)],
    summary="Unblock a join request",
    response_description="The request after the change, now `rejected`.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
            HTTPStatus.FORBIDDEN: LEADER_403,
            HTTPStatus.NOT_FOUND: LEADER_REQUEST_404,
            HTTPStatus.CONFLICT: "The request isn't blocked. Nothing was changed; it fails "
            "the same way on retry.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
        }
    ),
    description="""
**Context.** How a trip's leaders lift a block, so the person may ask to join
again (decision-log Entry 29).

**How it works.** `limit_writes` runs first, then `require_trip_leader` (401,
then 404, then 403, as on the decision). Then, under a lock on the trip row and
then the request: a request not on this trip is 404; one that isn't `blocked`
is 409. A blocked request becomes `rejected`, keeping the original decision
time, so the 7-day wait from that decision still applies before the person may
ask again.

**Related APIs.** `GET /api/v2/trips/{tripId}/join-requests?state=blocked`
lists the blocked requests;
`POST /api/v2/trips/{tripId}/join-requests/{requestId}/decision` blocks one.
""",
)
async def unblock_join_request(
    context: LeaderDep, session: SessionDep, request_id: RequestIdPath
) -> TripJoinRequestOut:
    """Turn a blocked request on the trip into a rejected one."""
    result = await join_repo.unblock(
        session,
        trip_id=context.trip.id,
        request_id=request_id,
        caller_id=context.user.user_id,
    )
    return _leader_result(result, NOT_BLOCKED_MESSAGE)
