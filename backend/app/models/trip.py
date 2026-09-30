from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from app.models.bike import BikeOut
from app.models.member import MemberRole

# `TripCreate.id` must be canonical: lowercase hex, hyphenated 8-4-4-4-12. The one
# create that validates id format, because a trip id becomes a public URL and the
# S3 key prefix — two spellings of one UUID must not be two trips
# (docs/api-contract.md, "Idempotency: additions").
CANONICAL_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

PUBLIC_DELAY_HOURS_MAX = 168  # mirrors `trips_public_delay_hours_check` (0-168)

TripName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Access(StrEnum):
    RIDER = "rider"
    VIEWER = "viewer"


class TripOut(BaseModel):
    """GET /trips/{slug} response. Works with either slug."""

    id: str = Field(
        description="The trip's id. Stable, and safe to show or log — it is not a "
        "credential and cannot be used to reach the trip. Neither slug appears "
        "anywhere in this response."
    )
    name: str = Field(description="The trip's display name, as the rider titled it.")
    startDate: date = Field(
        description="The day the trip starts, as an ISO 8601 date (YYYY-MM-DD). A date "
        "with no time and no timezone: it is the calendar day the journal opens on, not "
        "an instant, so it reads the same wherever the viewer is."
    )
    bikes: list[BikeOut] = Field(
        description="The bikes on **this** trip, embedded so the app's first load is one "
        "request rather than two. Empty list when no bikes have been added yet — that is a "
        "normal trip, not an error. Order is not part of the contract; match bikes by `id`."
    )
    access: Access = Field(
        description="Deprecated; kept for clients from before accounts. 'rider' when the "
        "caller is signed in with an active membership on this trip (rider or leader), "
        "'viewer' otherwise — anonymous, a signed-in non-member, a pending requester or a "
        "revoked member. Which slug was used plays no part. A UI hint for whether to show "
        "write UI (Add stop, upload photo, edit bikes); the server enforces access on every "
        "write regardless."
    )


class Visibility(StrEnum):
    """Who can read a trip. Mirrors `trips_visibility_check` in migration 0003."""

    PUBLIC = "public"  # anyone, with the public delay applied for non-members
    PRIVATE = "private"  # members only; a non-member gets the same 404 as a nonexistent trip


class ViewerRole(StrEnum):
    """
    The caller's relationship to one trip — a UI hint only; the server enforces access.

    Revoked, rejected, blocked and self-departed users are all `none`.
    """

    ANONYMOUS = "anonymous"  # no valid session
    NONE = "none"  # signed in, no active membership and no pending request
    PENDING = "pending"  # signed in, with a pending join request on this trip
    RIDER = "rider"  # active membership as rider
    LEADER = "leader"  # active membership as leader


class ViewerOut(BaseModel):
    """What the caller is to this trip. Tells the UI what to show; never what is allowed."""

    role: ViewerRole = Field(
        description="The caller's role on this trip, computed from the session on every "
        "request: 'anonymous' (no session), 'none' (signed in, no membership or pending "
        "request — including revoked, rejected and blocked users), 'pending', 'rider' or "
        "'leader'. A UI hint only: the server enforces access on every write regardless."
    )


def _omit_default(schema: dict[str, Any]) -> None:
    """Drop ``default: null`` from a ``TripPatch`` field's schema -- null is not a valid value."""
    schema.pop("default", None)


_NULL_REJECTED = (
    " Omit the field to leave the stored value alone. Explicit null is rejected with 422 / "
    "VALIDATION_ERROR: omitting is how 'no change' is spelled."
)


class TripCreate(BaseModel):
    """POST /api/v2/trips body. Needs a session; the caller becomes the trip's first leader."""

    id: str = Field(
        pattern=CANONICAL_UUID_PATTERN,
        description="Client-generated UUID4 in canonical form: lowercase hex, hyphenated "
        "8-4-4-4-12. Any other spelling (uppercase, braces, no hyphens) is rejected with 422 "
        "/ VALIDATION_ERROR, because the id becomes the trip's public URL. Replaying the same "
        "id as its creator, while still an active member, returns the existing trip (200); "
        "any other existing id is a 409.",
    )
    name: TripName = Field(
        description="The trip's display name. Trimmed, then 1-100 characters, or the request "
        "is rejected with 422 / VALIDATION_ERROR."
    )
    startDate: date = Field(
        description="The day the trip starts, as an ISO 8601 date (YYYY-MM-DD) with no time "
        "and no timezone."
    )
    visibility: Visibility = Field(
        default=Visibility.PUBLIC,
        description="Who can read the trip. Defaults to 'public': anyone can browse it, with "
        "stops newer than the public delay (24 hours) hidden from non-members. 'private' "
        "hides it from everyone but its members.",
    )


# Same shape as `BikePatch`: each field is typed non-optional with a ``None``
# default that is never validated, so omitting a field leaves it unset while an
# explicit ``null`` fails as a 422. ``_omit_default`` keeps ``default: null`` out
# of the schema, so the generated client types each field optional, never nullable.
class TripPatch(BaseModel):
    """
    PATCH /api/v2/trips/{tripId} body. Leaders only. Every field may be omitted -- only
    fields present in the body change, and an empty body changes nothing -- but none may
    be null.
    """

    name: TripName = Field(
        default=None,
        json_schema_extra=_omit_default,
        description="The trip's display name. Trimmed, then 1-100 characters, or the request "
        "is rejected with 422 / VALIDATION_ERROR." + _NULL_REJECTED,
    )
    visibility: Visibility = Field(
        default=None,
        json_schema_extra=_omit_default,
        description="Who can read the trip: 'public' or 'private'. There is no server-side "
        "publish confirmation; the app warns before publishing." + _NULL_REJECTED,
    )
    publicDelayHours: int = Field(
        default=None,
        ge=0,
        le=PUBLIC_DELAY_HOURS_MAX,
        json_schema_extra=_omit_default,
        description="How many hours a stop stays hidden from non-members after its "
        "`arrivedAt`, from 0 to 168 inclusive; anything else is rejected with 422 / "
        "VALIDATION_ERROR. 0 shows stops to the public as soon as they are sent." + _NULL_REJECTED,
    )


class TripSummaryOut(BaseModel):
    """One public trip in the Discover list. The same for every caller."""

    id: str = Field(
        description="The trip's id. Non-secret and immutable; the trip's public URL is built "
        "from it."
    )
    name: str = Field(description="The trip's display name, as its leaders titled it.")
    startDate: date = Field(
        description="The day the trip starts, as an ISO 8601 date (YYYY-MM-DD) with no time "
        "and no timezone."
    )
    riderCount: int = Field(
        description="How many people are on the trip: its active members, leaders included."
    )
    lastPublicStopAt: datetime | None = Field(
        description="The latest `arrivedAt` among the stops visible to the public, i.e. with "
        "the public delay applied, as a timezone-aware ISO 8601 instant. The same value for "
        "every caller, members included. Null when no stop is visible yet."
    )


class TripPageOut(BaseModel):
    """GET /api/v2/trips response: one page of public trips."""

    items: list[TripSummaryOut] = Field(
        description="Public trips, sorted by `lastPublicStopAt` descending with nulls last, "
        "then by `id` ascending. Private trips never appear. Empty when there are none."
    )
    nextCursor: str | None = Field(
        description="Opaque cursor for the next page: pass it back as `cursor` unchanged. Null "
        "on the last page."
    )


class MyTripOut(BaseModel):
    """One trip the caller is an active member of. Elements of `GET /api/v2/me/trips`."""

    id: str = Field(description="The trip's id; the trip's URL is built from it.")
    name: str = Field(description="The trip's display name, as its leaders titled it.")
    startDate: date = Field(
        description="The day the trip starts, as an ISO 8601 date (YYYY-MM-DD) with no time "
        "and no timezone."
    )
    role: MemberRole = Field(description="The caller's role on this trip: 'rider' or 'leader'.")
