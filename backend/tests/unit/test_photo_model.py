"""
``PhotoOut`` — the description ratchet for the photo read model.

Modelled on ``test_bike_model.py``: a sweep over ``model_fields`` rather than a
list of field names, so the test catches the *next* field added without a
description instead of only the four that were bare before this patch.

``PhotoOut`` is the response model of ``GET`` and ``POST
/trips/{slug}/stops/{stop_id}/photos``, so it reaches
``app.openapi()["components"]["schemas"]`` and the spec-level half of the ratchet
is assertable. That half is what matters: a description that exists on the field
but never reaches the document is invisible to Kubb, which reads the document and
nothing else.

``PhotoCreateForm`` is deliberately not covered — scope, not impossibility. Its
own undescribed ``takenAt`` is tracked separately as
``t-photocreateform-field-descriptions``, whose trigger has not fired. Note for
whoever picks that up: the model is absent from ``components.schemas`` because
the route declares its fields as inline ``Annotated[..., Form(...)]``
parameters, but FastAPI still publishes a synthesised body schema
(``Body_upload_photo_api_trips__slug__stops__stop_id__photos_post``), so a
spec-level ratchet is available there under that name.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.main
from app.models.photo import PhotoOut


def _openapi() -> dict[str, Any]:
    """The served OpenAPI document, as ``/openapi.json`` renders it."""
    return app.main.app.openapi()


@pytest.mark.parametrize("field_name", list(PhotoOut.model_fields))
def test_every_photo_out_field_has_a_real_description(field_name: str) -> None:
    """
    Every field of ``PhotoOut`` carries a description, not just a type.

    The length floor is the house one (``BikeOut``, ``BikeCreate``,
    ``StopCreate``): a ``description="id"`` satisfies a presence check while
    telling a reader nothing they could not already see from the field name.
    """
    description = PhotoOut.model_fields[field_name].description

    assert description, f"PhotoOut.{field_name} has no description"
    assert len(description) > 25, f"PhotoOut.{field_name}: description is a stub"


def test_photo_out_is_published_in_the_document() -> None:
    """
    ``PhotoOut`` is actually in ``components.schemas`` — the premise of the ratchet.

    Without this, a route that stopped referencing ``PhotoOut`` would drop it from
    the document and the spec-level test below would fail with a ``KeyError`` that
    reads like a missing description rather than a missing schema. It is also the
    promotion trigger this task was filed against: an undescribed field in an
    unpublished model reaches no client, a published one reaches every client.
    """
    assert "PhotoOut" in _openapi()["components"]["schemas"]


def test_descriptions_survive_into_the_openapi_document() -> None:
    """
    ...and they reach the emitted spec, which is the only place they are consumed.

    Asserted against ``app.openapi()`` rather than ``model_json_schema()`` because
    the document is what Kubb generates from and what every agent reads.
    """
    properties = _openapi()["components"]["schemas"]["PhotoOut"]["properties"]

    for name in PhotoOut.model_fields:
        assert properties[name].get("description"), f"PhotoOut.{name} lost its description"


def test_taken_at_description_makes_no_timezone_claim() -> None:
    """
    ``PhotoOut.takenAt``'s description does not settle the open timezone question.

    Whether a capture time must carry a UTC offset is recorded as *unruled* at
    ``docs/api-contract.md:305`` and tracked as ``t-takenat-tz-question``. A
    description is contract text that Kubb ships to the client, so stating a rule
    here would answer the question by the back door — a client author reading
    "must include an offset" has no way to tell a ruling from a guess.

    Word-level and case-insensitive, so the wording stays free to change; what is
    pinned is that none of these words appear until the ruling lands.
    """
    text = (PhotoOut.model_fields["takenAt"].description or "").lower()

    for claim in ("timezone", "time zone", "utc", "offset", "aware", "naive", "iso 8601"):
        assert claim not in text, f"PhotoOut.takenAt's description makes a timezone claim: {claim!r}"
