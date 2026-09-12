from pydantic import BaseModel, Field


class BikeCreate(BaseModel):
    """POST /trips/{slug}/bikes body. Rider-slug only."""

    id: str = Field(
        description="Client-generated UUID4. Replaying the same id returns the "
        "existing bike (200) instead of creating a duplicate — see docs/api-contract.md "
        "'Idempotency'."
    )
    riderName: str = Field(description="Whose bike this is — free text, not tied to a user account.")
    make: str
    model: str
    year: int
    specs: str = Field(default="", description="Free text: engine, suspension, tyres, etc.")


class BikePatch(BaseModel):
    """PATCH /trips/{slug}/bikes/{id} body. Rider-slug only. All fields optional — only sent fields change."""

    riderName: str | None = None
    make: str | None = None
    model: str | None = None
    year: int | None = None
    specs: str | None = None


class BikeOut(BaseModel):
    id: str
    riderName: str
    make: str
    model: str
    year: int
    specs: str
