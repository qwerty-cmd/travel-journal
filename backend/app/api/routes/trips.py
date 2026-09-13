"""
Trip routes — ``GET /api/trips/{slug}``, the first request the app ever makes.

The handler below is deliberately thin: the slug dependency has already resolved
the trip and derived the access level, and the repository layer owns every
column name. What is left here is the mapping from those two values onto the
contract's ``TripOut``, written out field by field (see the handler's own note
on why it is spelled out rather than spread).
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends

# `AsyncSession` is imported for the dependency annotation only — the same way
# `app/core/security.py` declares it. No statement is built and no column is
# named in this module: the session is taken here purely so it can be handed to
# `data/repositories/`, which is the only layer allowed to know the schema
# (spec Section 4, "Portability principle").
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TripContext, require_trip_access
from app.data.db import get_session
from app.data.repositories.bikes import list_by_trip
from app.models.common import ErrorEnvelope
from app.models.trip import TripOut

router = APIRouter(prefix="/trips", tags=["trips"])


@router.get(
    "/{slug}",
    summary="Get a trip's metadata and its bikes",
    response_description="The trip, the bikes on it, and which kind of link was used.",
    responses={
        HTTPStatus.NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No trip has this slug. Deliberately the same answer for a "
            "mistyped link, a revoked one and a guess — see `docs/api-contract.md`, "
            "'Access control: 403 and 404 are different answers'.",
        }
    },
    description="""
**Context.** The rider shares one trip through two unguessable links: a rider
link that can write and a viewer link that can only read. This is the endpoint
both of them open first, and for a viewer it is the whole journal header. It
answers three questions in one round trip — which trip is this, which bikes are
on it, and may I write to it — because the app is used on the Stuart Hwy where a
second request is a second chance to be offline. Task `t-trip-metadata-endpoint`.

**How it works.** `{slug}` is resolved by the shared `require_trip_access`
dependency, which accepts **either** slug and raises a 404 if neither matches;
a viewer slug is a perfectly ordinary success here, not a 403, because this is a
read. The dependency also derives `access` from the matched row, and that value
is passed straight through to `TripOut.access` — the handler never recomputes
it. The trip's bikes are fetched by `data/repositories/bikes.list_by_trip`,
filtered to this trip; a trip with no bikes returns `"bikes": []`, which is a
normal trip and not a 404. Neither slug is in the response: `TripOut` has no
slug field, and the slug is the credential.

`access` is a **UI hint**, not the enforcement point. It tells the frontend
whether to render Add stop / upload photo / edit bikes. The write endpoints
reject a viewer slug themselves regardless of what the UI chose to show.

**Related APIs.** `GET /api/trips/{slug}/stops` for the stops this header sits
above, `GET /api/trips/{slug}/map` for the same trip as GeoJSON, and
`POST /api/trips/{slug}/bikes` / `PATCH /api/trips/{slug}/bikes/{id}` for the
rider-only writes behind the `bikes` list returned here.
""",
)
async def get_trip(
    context: Annotated[TripContext, Depends(require_trip_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TripOut:
    """
    The trip behind this slug, with its bikes and the caller's access level.

    Built by explicit keyword, not by spreading the record. Three reasons, and
    they are the reason the next seven handlers should look like this one:

    - The wire names are camelCase and the columns are snake_case
      (``tables.py``). ``startDate=context.trip.start_date`` is the mapping,
      stated once, where both halves are visible together.
    - ``TripRecord`` carries **both slugs**. A spread — ``TripOut(**asdict(...))``
      or ``model_validate(record)`` — is a construct that reaches for every
      attribute of a record whose extra attributes are the app's only
      credentials. Naming four fields cannot pick up a fifth by accident.
    - ``access`` is not on the record at all. It is a property of the *request*
      (which of the two links was followed), so it can only come from the
      dependency's ``TripContext``, never from the row.
    """
    return TripOut(
        id=context.trip.id,
        name=context.trip.name,
        startDate=context.trip.start_date,
        bikes=await list_by_trip(session, context.trip.id),
        access=context.access,
    )
