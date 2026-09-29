"""Per-person join requests and their leader decisions (decision-log Entry 29 §5)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

JOIN_MESSAGE_MAX_LENGTH = 280  # mirrors `join_requests_message_length_check`


class JoinRequestState(StrEnum):
    """Mirrors `join_requests_state_check`. Leaves `pending` exactly once."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"  # withdrawn by the requester
    BLOCKED = "blocked"  # rejected, and further requests refused until a leader unblocks


class JoinRequestVia(StrEnum):
    """How the request was made. Mirrors `join_requests_via_check`."""

    DIRECT = "direct"
    LEGACY_RIDER_LINK = "legacy_rider_link"  # redeemed a pre-accounts rider slug via /claim


class JoinDecisionAction(StrEnum):
    """What a leader does with a pending request."""

    APPROVE = "approve"
    REJECT = "reject"
    REJECT_AND_BLOCK = "reject_and_block"


def _empty_to_none(value: object) -> object:
    """A message that is empty after trimming is stored as null, like an omitted one."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


JoinMessage = Annotated[
    Annotated[str, StringConstraints(strip_whitespace=True, max_length=JOIN_MESSAGE_MAX_LENGTH)]
    | None,
    BeforeValidator(_empty_to_none),
]


class JoinRequestCreate(BaseModel):
    """POST /api/v2/trips/{tripId}/join-requests body."""

    message: JoinMessage = Field(
        default=None,
        description="An optional note to the trip's leaders, e.g. who else is riding with you. "
        "Trimmed, then at most 280 characters, or the request is rejected with 422 / "
        "VALIDATION_ERROR. Omitted, null or empty after trimming are all stored as null. "
        "Ignored when a pending request already exists: that request keeps its message.",
    )


class JoinClaimCreate(BaseModel):
    """POST /api/v2/trips/claim body. The slug travels in the body, never the URL."""

    riderSlug: str = Field(
        description="A legacy rider link's slug. Redeeming it creates a **pending** join "
        "request on that trip and never grants membership. A viewer slug or an unknown slug "
        "is a 404. Never logged and never echoed back."
    )


class JoinDecisionCreate(BaseModel):
    """POST /api/v2/trips/{tripId}/join-requests/{requestId}/decision body."""

    action: JoinDecisionAction = Field(
        description="'approve' adds the requester as a rider; 'reject' refuses them, with a "
        "7-day cooldown before they may ask again; 'reject_and_block' refuses them until a "
        "leader unblocks. Repeating the decision that produced the current state is a 200; "
        "any other decision on an already-decided request is a 409."
    )


class PersonOut(BaseModel):
    """Someone as a trip's leaders see them: an id to act on and a public name. No username."""

    userId: str = Field(
        description="The person's account id. Only ever returned to the trip's leaders and "
        "members, never to a non-member."
    )
    displayName: str = Field(description="The person's public name. Never their username.")


class MyJoinRequestOut(BaseModel):
    """One of the caller's own join requests, as the requester sees it."""

    id: str = Field(description="The join request's id, server-generated. Used to cancel it.")
    tripId: str = Field(description="The id of the trip this request is to join.")
    tripName: str = Field(description="The display name of the trip this request is to join.")
    state: JoinRequestState = Field(
        description="Where the request stands. A request a leader blocked is reported to the "
        "requester as 'rejected', so this is never 'blocked' here."
    )
    message: str | None = Field(
        description="The note sent with the request, or null if none was sent."
    )
    createdAt: datetime = Field(
        description="When the request was made, as a timezone-aware ISO 8601 instant. Lists "
        "are ordered by it, newest first."
    )


class TripJoinRequestOut(BaseModel):
    """One join request on a trip, as that trip's leaders see it."""

    id: str = Field(
        description="The join request's id, server-generated. Used to decide or unblock it."
    )
    requester: PersonOut = Field(
        description="Who is asking to join: an id and a public name, never a username."
    )
    state: JoinRequestState = Field(
        description="Where the request stands: 'pending' awaiting a decision, or the outcome."
    )
    via: JoinRequestVia = Field(
        description="'direct' for a request made on the trip, 'legacy_rider_link' for one made "
        "by redeeming a pre-accounts rider link."
    )
    message: str | None = Field(
        description="The note the requester sent, or null if none was sent."
    )
    createdAt: datetime = Field(
        description="When the request was made, as a timezone-aware ISO 8601 instant."
    )
