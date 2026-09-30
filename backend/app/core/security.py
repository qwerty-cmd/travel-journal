"""
Access control — the gates every ``/api`` route declares (decision-log Entry 29).

**Context.** Access answers two separate questions (``docs/api-contract.md``,
"Access: public trips, members and leaders"):

- **Who is asking?** The ``__Host-btj_session`` cookie answers it
  (``require_session``).
- **What may they do on this trip?** Their row in ``trip_members`` answers it,
  read from Postgres on every request with no cache, so a revocation takes
  effect on the next request (``app/data/repositories/memberships.py``).

A slug is **no longer a credential for anything**. Before Entry 29 the rider
slug *was* the write authorisation and the viewer slug the read-only one. Now
either slug only *locates* a trip on the legacy ``/api/trips/{slug}`` routes,
and writes go through the membership gate (contract default 21: "legacy writes
accept either slug as the locator: the membership gate is the only
authorisation"). The ``access`` a legacy read reports is derived from
membership too, never from which slug was followed.

The gates built here:

- ``require_session`` — a valid session, else ``401``. Account routes.
- ``require_trip_access`` — legacy reads. Either slug, else ``404``. The session
  is optional and only decides ``access``; it never refuses a read.
- ``require_trip_writer`` — legacy writes, with a slug locator. Slug (``404``) →
  session (``401``) → active membership (``403``). A ``tripId`` locator for the
  v2 routes arrives with ``t-am-v2-rider-writes``, and ``require_trip_leader``
  with its first consumer, ``t-am-trip-create``.

**Why the legacy write order is slug → session → membership.** This is the ADR's
order unchanged (Entry 29; contract, "Where the session check sits"). A trip
found by slug is never hidden — the slug is proof the caller was sent the link —
so answering ``404`` for an unknown slug before asking who the caller is reveals
nothing a ``401`` would have withheld. The v2 routes check the session *first*
(contract default 1) because there the trip id is public and a private trip must
answer a signed-out caller with ``401``, not a never-retry ``404`` that would
permanently fail a queued write whose only problem is an expired session.

**Why 401, 403 and 404 are three different answers** (contract, "Access
control: 401, 403 and 404 are three different answers"). Collapsing any two is a
real bug:

- ``404`` — nothing you may see exists here. An unknown slug is always ``404``,
  never ``403``: a ``403`` would tell someone trying links that this one named
  a real trip. **On private trips (the v2 gates) a non-member's ``404`` is
  byte-identical to a nonexistent trip's**, so a trip id cannot be used to test
  whether a private trip exists. That is why a private trip answers ``404`` and
  not ``403`` to strangers; the legacy slug gate never needs it, because a slug
  already located the trip.
- ``401`` — we don't know who you are. The offline queue *pauses* on it and
  resumes after sign-in; a ``403`` or ``404`` would fail the item for good.
- ``403`` — we know who you are, the trip is located, and you may not write. A
  revoked member is told "You're no longer a rider on this trip."; anyone else
  "You're not a rider on this trip." Both are never-retry.

Errors leave here as ``ApiError`` through its classmethod constructors, never as
a hand-built response — ``app/core/errors.py`` turns them into the one envelope
shape the generated client and the offline queue parse. Nothing here ever puts a
slug into a message or a response body: a slug is still a locator for a
possibly private trip, and error messages are the part of a response most
likely to end up in a log, a screenshot or a bug report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Path, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.sessions import (
    SESSION_COOKIE_NAME,
    SessionUser,
    cleared_session_cookie_header,
    resolve_session,
    set_session_cookie,
)
from app.data.db import SessionDep
from app.data.repositories.memberships import MembershipRecord, get_for_user
from app.data.repositories.trips import TripRecord, get_by_slug
from app.models.member import MemberRole
from app.models.trip import Access

# Shown to whoever followed a link that resolves to nothing. Deliberately says
# nothing about the slug itself — not its value, not its length, not whether it
# "looks like" one half of a real pair. The message a stranger sees and the
# message a rider with a stale link sees have to be identical, or the difference
# is itself the leak.
UNKNOWN_TRIP_MESSAGE = (
    "We couldn't find a trip for this link. Check that you have the whole link you were sent."
)

# The one message for every 401 from the session gate: no cookie, garbage,
# expired, over the 365-day cap, deleted, or a disabled account. One string so
# the answers can't be told apart.
SIGN_IN_REQUIRED_MESSAGE = "You need to sign in to do this."

# The two 403s from the writer gate (contract, "Offline-queue classification").
# Distinct on purpose: a rider whose membership was revoked needs to know that is
# what happened, rather than wonder whether they are signed in to the wrong
# account.
NOT_A_RIDER_MESSAGE = "You're not a rider on this trip."
NO_LONGER_A_RIDER_MESSAGE = "You're no longer a rider on this trip."


@dataclass(frozen=True, slots=True)
class TripContext:
    """
    The resolved answer to "which trip, and may the caller write to it?".

    Handed to every legacy read by ``require_trip_access``, so a handler never
    repeats the lookup or re-derives the answer. Frozen: a handler that could
    reassign ``access`` mid-request could grant itself write UI after the gate
    had already run.

    ``trip`` carries both slugs (see ``TripRecord``). They must not be
    serialised into a response; ``TripOut`` has no slug fields for that reason.
    """

    trip: TripRecord
    access: Access


@dataclass(frozen=True, slots=True)
class TripWriterContext(TripContext):
    """
    What ``require_trip_writer`` hands a write handler: the trip, and who is writing.

    ``access`` is always ``Access.RIDER`` — the gate raised otherwise. ``user``
    is the signed-in account, the source of ``created_by`` and of a photo's
    ``uploaded_by`` display name. ``role`` is the caller's active membership role.
    """

    user: SessionUser
    role: MemberRole


def access_for_membership(membership: MembershipRecord | None) -> Access:
    """
    ``TripOut.access`` for a caller with this membership: ``rider`` iff it is active.

    Deprecated field, kept for clients from before the upgrade (contract, "TripOut.
    viewer.role and TripOut.access only tell the UI what to show"): ``rider`` when
    the caller is an active rider or leader, ``viewer`` otherwise — anonymous,
    signed-in stranger, pending, or revoked. Which slug was followed plays no part.

    A pure function so the *flag* stays testable apart from the *gate*: an
    ``access`` stuck at ``rider`` shows a stranger buttons that always fail, with
    enforcement still perfectly correct, and only a separate test catches that.
    """
    return Access.RIDER if membership is not None and membership.active else Access.VIEWER


async def _locate_trip(slug: str, db: AsyncSession) -> TripRecord:
    """
    The trip either slug names, or ``404``.

    The single place "no such trip" becomes an HTTP answer. Both legacy gates
    funnel through it so they cannot drift apart and give different statuses for
    the same unresolvable slug — which would leak by comparison even though each
    answer alone looked reasonable.
    """
    trip = await get_by_slug(db, slug)
    if trip is None:
        # 404 and not 403, always, and before the session is even looked at. A
        # 403 here would confirm to a caller working through guesses that the
        # slug names a real trip.
        raise ApiError.not_found(UNKNOWN_TRIP_MESSAGE)
    return trip


async def _signed_in_user(request: Request, response: Response, db: AsyncSession) -> SessionUser:
    """The session gate's body, shared by ``require_session`` and ``require_trip_writer``."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    resolved = await resolve_session(db, token) if token else None
    if resolved is None:
        # `is not None`, not truthiness: an empty cookie value was still sent,
        # and is still cleared.
        raise ApiError.unauthenticated(
            SIGN_IN_REQUIRED_MESSAGE,
            set_cookie=cleared_session_cookie_header() if token is not None else None,
        )

    if resolved.refreshed:
        set_session_cookie(response, token)

    return resolved.user


async def _optional_user(
    request: Request, response: Response, db: AsyncSession
) -> SessionUser | None:
    """
    The signed-in account if a valid session was sent, else ``None``. Never raises.

    For reads, where the session only decides what the UI is told. An invalid
    cookie is treated as no cookie: a read never answers ``401``, so there is no
    ``401`` to carry a clearing ``Set-Cookie`` either. A valid session due its
    daily refresh is re-issued on ``response``, as ``require_session`` does.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    resolved = await resolve_session(db, token)
    if resolved is None:
        return None
    if resolved.refreshed:
        set_session_cookie(response, token)
    return resolved.user


async def require_trip_access(
    slug: Annotated[
        str,
        Path(
            description="The trip's rider or viewer slug — the link the trip was shared "
            "with. Either one locates the trip; neither grants anything. Legacy reads "
            "accept both and give the full, undelayed trip."
        ),
    ],
    request: Request,
    response: Response,
    session: SessionDep,
) -> TripContext:
    """
    Legacy read gate: resolve ``{slug}`` to a trip, either slug accepted.

    ``access`` is ``rider`` iff the session user is an active member
    (``access_for_membership``), and ``viewer`` for everyone else, including
    anonymous callers. The session is optional: it never turns a read into a
    ``401``. ``access`` is a hint for rendering only — the enforcement point is
    ``require_trip_writer``, on the write routes, regardless of what the UI
    chose to show.

    Raises ``404`` if no trip has this slug.
    """
    trip = await _locate_trip(slug, session)
    user = await _optional_user(request, response, session)
    membership = await get_for_user(session, trip.id, user.user_id) if user else None
    return TripContext(trip=trip, access=access_for_membership(membership))


async def require_trip_writer(
    slug: Annotated[
        str,
        Path(
            description="The trip's rider or viewer slug. It only locates the trip: the "
            "write itself needs a signed-in account with an active membership on it. An "
            "unknown slug is a 404, no session a 401, and a non-member or revoked member "
            "a 403."
        ),
    ],
    request: Request,
    response: Response,
    session: SessionDep,
) -> TripWriterContext:
    """
    Legacy write gate: slug (``404``) → session (``401``) → active membership (``403``).

    The order is the ADR's, and is the reason this gate does not declare
    ``require_session`` as a sub-dependency: FastAPI solves sub-dependencies
    before the function body, which would put the ``401`` ahead of the ``404``.
    It calls the same session code in order instead (see the module docstring
    for why slug-first is safe here and session-first is right on v2).

    The membership is read fresh on every request. A revoked member gets
    ``NO_LONGER_A_RIDER_MESSAGE``; a signed-in caller with no membership row at
    all (including one with only a pending join request) gets
    ``NOT_A_RIDER_MESSAGE``. Both are ``403``: the trip was located, so ``404``
    would be a lie that sends a rider chasing a link that isn't broken.

    Because a gate runs before the handler, a replay of an already-stored id
    from a revoked rider is refused here too, before any replay lookup — a
    replay is not an exception to authorisation (contract, "Idempotency:
    additions").

    FastAPI validates the request body alongside dependencies, so a body that
    isn't valid JSON gets a 422 before this gate runs; a schema-invalid body
    still gets this gate's answer first. Neither reveals anything about the slug
    (decision-log Entry 23, won't-fix).
    """
    trip = await _locate_trip(slug, session)
    user = await _signed_in_user(request, response, session)
    membership = await get_for_user(session, trip.id, user.user_id)

    if membership is None:
        raise ApiError.forbidden(NOT_A_RIDER_MESSAGE)
    if not membership.active:
        raise ApiError.forbidden(NO_LONGER_A_RIDER_MESSAGE)

    return TripWriterContext(trip=trip, access=Access.RIDER, user=user, role=membership.role)


async def require_session(request: Request, response: Response, db: SessionDep) -> SessionUser:
    """
    Session gate: the signed-in account behind the ``__Host-btj_session`` cookie.

    Returns a ``SessionUser``. Raises ``401 UNAUTHENTICATED`` (with
    ``WWW-Authenticate``) for a missing, garbage, idle-expired, over-cap or
    deleted token, and for a disabled account, all with the same message.

    **Two cookie side effects, delivered two ways:**

    - **Refresh.** When resolving the session bumped ``last_used_at`` (at most
      once every 24 h), the cookie is re-issued with a fresh ``Max-Age`` on
      ``response``, FastAPI's per-request ``Response`` parameter. FastAPI copies
      its headers onto the response the route returns. It does **not** copy them
      when a route returns a ``Response`` object itself, or when the request ends
      in an error: then the refresh is skipped, and the next request after 24 h
      re-issues it. The same holds for ``require_trip_writer`` and
      ``require_trip_access``, so every gated route returns a model (or ``None``
      with a decorator status), never its own ``Response``.
    - **Clear.** On a 401 from a request that sent the cookie, the clearing
      ``Set-Cookie`` (``Max-Age=0``) travels on the ``ApiError``, because the
      exception handler builds a new response and ``response`` is discarded. A
      request with no cookie gets no ``Set-Cookie``.

    The cookie is read from ``request.cookies`` rather than declared as a
    ``Cookie()`` parameter, so it is not listed as an operation parameter in the
    OpenAPI document and Kubb generates no client field for a value JavaScript
    can't read.
    """
    return await _signed_in_user(request, response, db)
