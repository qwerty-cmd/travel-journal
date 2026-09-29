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


class BikePatch(BaseModel):
    """PATCH /trips/{slug}/bikes/{id} body. Rider-slug only. All fields optional — only sent fields change."""

    riderName: str | None = Field(
        default=None,
        description="Whose bike this is — free text, not tied to a user account. Omit the field "
        "to leave the stored name alone; omitting is not the same as sending it explicitly as "
        "null, which asks to write null into a column that holds none.",
    )
    make: str | None = Field(
        default=None,
        description="Manufacturer, e.g. 'Honda'. Free text — not a fixed list. Omit the field to "
        "leave the stored make alone; omitting is not the same as sending it explicitly as null, "
        "which asks to write null into a column that holds none.",
    )
    model: str | None = Field(
        default=None,
        description="Model name, e.g. 'Africa Twin'. Free text — not a fixed list. Omit the field "
        "to leave the stored model alone; omitting is not the same as sending it explicitly as "
        "null, which asks to write null into a column that holds none.",
    )
    year: int | None = Field(
        default=None,
        description="Model year of the bike, as a four-digit year, e.g. 2019. Omit the field to "
        "leave the stored year alone; omitting is not the same as sending it explicitly as null, "
        "which asks to write null into a column that holds none.",
    )
    specs: str | None = Field(
        default=None,
        description="Free text: engine, suspension, tyres, etc. Omit the field to leave the "
        "stored specs alone; omitting is not the same as sending it explicitly as null. To clear "
        "specs send an empty string — that is how 'nothing written yet' is spelled, never null.",
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
