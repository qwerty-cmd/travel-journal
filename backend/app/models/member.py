"""Trip membership (decision-log Entry 29). Member lists go to active members only."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MemberRole(StrEnum):
    """An active membership's role. Mirrors `trip_members_role_check` in migration 0003."""

    RIDER = "rider"  # stops, photos, bikes; can leave and list members
    LEADER = "leader"  # rider writes plus trip settings, requests, promote, revoke riders


class MemberOut(BaseModel):
    """One active member of a trip. Elements of `GET /api/v2/trips/{tripId}/members`."""

    userId: str = Field(
        description="The member's account id. The path parameter for promote and revoke. "
        "Only ever returned to active members of the trip."
    )
    displayName: str = Field(description="The member's public name. Never their username.")
    role: MemberRole = Field(description="The member's role on this trip: 'rider' or 'leader'.")
    joinedAt: datetime = Field(
        description="When this membership started, as a timezone-aware ISO 8601 instant. "
        "Member lists are ordered by it, oldest first."
    )
