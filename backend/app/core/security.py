"""
Slug-based access control — the first thing every endpoint does.

There are no accounts, no passwords and no sessions. A trip has two unguessable
random tokens (spec Section 4): a **rider slug** granting read + write, and a
**viewer slug** granting read only. The slug in the URL *is* the authorisation
(``docs/api-contract.md``, "Access: two slugs, no accounts"). Whoever was given
which link is the entire access model, which is why this module is spec
Section 12's top-priority area — there is no second layer behind it.

Two dependencies, one shape:

- ``require_trip_access`` — read endpoints. Either slug is acceptable.
- ``require_rider_access`` — write endpoints. The rider slug only.

Both return a ``TripContext``, so a read handler and a write handler consume an
identical value and the only difference between them is which dependency they
declared.

**Why 403 and 404 must not collapse into each other.** The contract makes this
distinction load-bearing and both directions of the collapse are real bugs, not
cosmetic ones:

- Answering 404 to a viewer-slug *write* tells a read-only guest their link is
  broken, when in fact it works perfectly for the thing it was issued for.
- Answering 403 to a slug that resolves to nothing tells anyone guessing URLs
  that they can distinguish "wrong slug" from "right slug, wrong permission" —
  which turns the slug space into an oracle. The unguessable-slug model depends
  on exactly that signal not leaking, because it is the only thing standing
  between a stranger and a trip.

So: "nothing resolved" is always a 404, on both dependencies, and a 403 is only
ever reachable *after* a real trip has been found.

Errors leave here as ``ApiError`` through its classmethod constructors, never as
a hand-built response — ``app/core/errors.py`` is what turns them into the one
envelope shape the generated client and the offline queue can parse.

Nothing in this module ever puts a slug into a message or a response body. A
slug is the credential; echoing it back is the same class of mistake as echoing
a password, and error messages are the part of a response most likely to end up
in a log, a screenshot or a bug report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.data.db import SessionDep
from app.data.repositories.trips import TripRecord, get_by_slug
from app.models.trip import Access

# Shown to whoever followed a link that resolves to nothing. Deliberately says
# nothing about the slug itself — not its value, not its length, not whether it
# "looks like" one half of a real pair. The message a stranger sees and the
# message a rider with a stale link sees have to be identical, or the difference
# is itself the leak.
UNKNOWN_TRIP_MESSAGE = (
    "We couldn't find a trip for this link. Check that you have the whole link you were sent."
)

# Shown on a write attempted with a viewer slug. Says plainly that the link
# works and is read-only, because the reader is a legitimate guest who needs to
# know their link is not broken — that is the whole reason this is not a 404.
READ_ONLY_LINK_MESSAGE = (
    "This link is read-only. Ask the rider for their editing link if you need to make changes."
)


@dataclass(frozen=True, slots=True)
class TripContext:
    """
    The resolved answer to "which trip, and rider or viewer?".

    Handed to every endpoint by one of the two dependencies below, so a handler
    never repeats the lookup or re-derives the permission. Frozen: a handler
    that could reassign ``access`` mid-request could grant itself write access
    after the guard had already run.

    ``trip`` carries both slugs (see ``TripRecord``) — they are here so the
    dependency can derive ``access``, and must not be serialised into a
    response. ``TripOut`` has no slug fields for that reason.
    """

    trip: TripRecord
    access: Access


def access_for_slug(slug: str, trip: TripRecord) -> Access:
    """
    Which kind of link ``slug`` is, judged against the trip it resolved to.

    Compared against the matched record's own columns rather than inferred from
    which half of the repository's ``OR`` fired. Two reasons. It keeps
    *derivation* separately testable from *enforcement* — a bug in one cannot be
    hidden by the other passing. And it stays self-consistent in the case the
    lookup cannot rule out on its own: a string that is one trip's rider slug
    and a different trip's viewer slug. Random tokens make that unreachable in
    practice, but the answer here is a property of the row returned, so
    whichever row that is, the access reported is true of *that* trip.

    Anything that is not the rider slug is a viewer, so the *fallthrough* is the
    lesser permission and a future third slug column cannot silently grant
    writes by being added. That is a property of the ``else`` branch only, and
    it is worth being precise about what it does **not** cover: when a slug
    matches more than one column of the same row, this comparison resolves the
    tie to the **greater** permission, because ``rider_slug`` is tested first.
    The one row where that could happen is ``rider_slug == viewer_slug``, on
    which a link issued as read-only would return ``RIDER``.

    So the guarantee holds *because the database excludes that row*, not on the
    strength of this expression: ``trips_slugs_differ_check``
    (``migrations/0002_trips_slugs_differ_check.sql``) makes an equal pair
    unstorable. If that constraint is ever dropped, this function starts
    granting writes through viewer links and nothing here will notice.
    """
    return Access.RIDER if slug == trip.rider_slug else Access.VIEWER


async def _resolve_trip(slug: str, session: AsyncSession) -> TripContext:
    """
    Look the slug up and derive its access, or raise 404 if nothing matches.

    The single place "no such trip" becomes an HTTP answer. Both dependencies
    funnel through it so the two cannot drift apart and start giving different
    statuses for the same unresolvable slug — which would leak by comparison
    even though each answer alone looked reasonable.
    """
    trip = await get_by_slug(session, slug)
    if trip is None:
        # 404 and not 403, always. A 403 here would confirm to a caller working
        # through guesses that some other slug *does* exist, and that is the one
        # fact this access model cannot afford to give away.
        raise ApiError.not_found(UNKNOWN_TRIP_MESSAGE)

    return TripContext(trip=trip, access=access_for_slug(slug, trip))


async def require_trip_access(
    slug: Annotated[
        str,
        Path(
            description="The trip's rider or viewer slug — the unguessable link the "
            "trip was shared with. Read endpoints accept either one."
        ),
    ],
    session: SessionDep,
) -> TripContext:
    """
    Read-endpoint guard: resolve ``{slug}`` to a trip, either slug accepted.

    Returns the trip together with the access the caller is acting under, which
    handlers pass on as ``TripOut.access`` so the frontend knows whether to
    render write UI. That flag is a hint for rendering only — the API's actual
    enforcement point is ``require_rider_access``, on the write endpoints
    themselves, regardless of what the UI chose to show.

    Raises 404 if no trip has this slug.
    """
    return await _resolve_trip(slug, session)


async def require_rider_access(
    slug: Annotated[
        str,
        Path(
            description="The trip's **rider** slug. Write endpoints reject the "
            "viewer slug with a 403, and an unknown slug with a 404."
        ),
    ],
    session: SessionDep,
) -> TripContext:
    """
    Write-endpoint guard: resolve ``{slug}`` and require that it is the rider's.

    Returns the same ``TripContext`` as ``require_trip_access`` — its ``access``
    is always ``Access.RIDER`` on success — so a write handler and a read
    handler take the same value.

    Raises 404 if no trip has this slug, and 403 if it resolves but is the
    viewer slug. The order matters: an unknown slug must never reach the
    permission check, or the two answers become distinguishable to someone
    guessing links (see the module docstring).

    FastAPI parses the request body before it solves dependencies, so a body
    that isn't valid JSON gets a 422 before this guard runs. That 422 is
    byte-identical for rider, viewer and unknown slugs, so it reveals nothing
    about the slug. Don't add a second slug check ahead of body parsing to
    "fix" the order: it would be a second copy of this guard that can drift
    from it. Decision-log Entry 23 (won't-fix).
    """
    context = await _resolve_trip(slug, session)
    if context.access is not Access.RIDER:
        # 403 and not 404. The caller is looking at a real trip through a link
        # that genuinely works — telling them it does not exist would send a
        # read-only guest chasing a broken link that isn't broken.
        raise ApiError.forbidden(READ_ONLY_LINK_MESSAGE)

    return context
