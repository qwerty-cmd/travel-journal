from typing import Any

from pydantic import BaseModel, Field


class BikeCreate(BaseModel):
    """POST /trips/{slug}/bikes body. Rider-slug only."""

    id: str = Field(
        description="Client-generated UUID4. Replaying the same id returns the "
        "existing bike (200) instead of creating a duplicate — see docs/api-contract.md "
        "'Idempotency'."
    )
    riderName: str = Field(
        description="Whose bike this is — free text, not tied to a user account."
    )
    make: str = Field(description="Manufacturer, e.g. 'Honda'. Free text — not a fixed list.")
    model: str = Field(description="Model name, e.g. 'Africa Twin'. Free text — not a fixed list.")
    year: int = Field(description="Model year of the bike, as a four-digit year, e.g. 2019.")
    specs: str = Field(default="", description="Free text: engine, suspension, tyres, etc.")


def _omit_default(schema: dict[str, Any]) -> None:
    """Drop ``default: null`` from a ``BikePatch`` field's schema -- null is not a valid value."""
    schema.pop("default", None)


_NULL_REJECTED = (
    " Omit the field to leave the stored value alone. Explicit null is rejected with 422 / "
    "VALIDATION_ERROR: the column holds no null, and omitting is how 'no change' is spelled."
)


# Each BikePatch field is typed non-optional with a ``None`` default that is never
# validated: omitting a field leaves it unset (the repository's
# ``model_dump(exclude_unset=True)`` drops it), while an explicit ``null`` is
# validated against ``str`` / ``int`` and fails as a 422 instead of reaching a
# NOT NULL column as a 500. ``_omit_default`` keeps ``default: null`` out of the
# schema, so the generated client types each field as optional, never nullable.
class BikePatch(BaseModel):
    """
    PATCH /trips/{slug}/bikes/{id} body. Rider-slug only. Every field may be omitted --
    only fields present in the body change -- but none may be null.
    """

    riderName: str = Field(
        default=None,
        json_schema_extra=_omit_default,
        description="Whose bike this is — free text, not tied to a user account." + _NULL_REJECTED,
    )
    make: str = Field(
        default=None,
        json_schema_extra=_omit_default,
        description="Manufacturer, e.g. 'Honda'. Free text — not a fixed list." + _NULL_REJECTED,
    )
    model: str = Field(
        default=None,
        json_schema_extra=_omit_default,
        description="Model name, e.g. 'Africa Twin'. Free text — not a fixed list."
        + _NULL_REJECTED,
    )
    year: int = Field(
        default=None,
        json_schema_extra=_omit_default,
        description="Model year of the bike, as a four-digit year, e.g. 2019." + _NULL_REJECTED,
    )
    specs: str = Field(
        default=None,
        json_schema_extra=_omit_default,
        description="Free text: engine, suspension, tyres, etc. To clear specs send an empty "
        "string — that is how 'nothing written yet' is spelled, never null." + _NULL_REJECTED,
    )


class BikeOut(BaseModel):
    """One bike as returned. Embedded in `TripOut.bikes`, and the response to both bike writes."""

    id: str = Field(
        description="The bike's id — the same client-generated UUID4 that was sent on "
        "create, so the device's local copy and the server's record share one identifier. "
        "Match bikes by this, never by their position in a list."
    )
    riderName: str = Field(
        description="Whose bike this is — free text, not tied to a user account. Stored as "
        "`bikes.rider_name`; the camelCase spelling here is the wire name."
    )
    make: str = Field(description="Manufacturer, e.g. 'Honda'. Free text — not a fixed list.")
    model: str = Field(description="Model name, e.g. 'Africa Twin'. Free text — not a fixed list.")
    year: int = Field(description="Model year of the bike, as a four-digit year, e.g. 2019.")
    specs: str = Field(
        description="Free text: engine, suspension, tyres, etc. Empty string when nothing has "
        "been written yet — never null, so 'not filled in' has one representation."
    )
