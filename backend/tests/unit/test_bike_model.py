"""
``BikeCreate`` / ``BikePatch`` — the description ratchet for the two write models.

Modelled on ``test_every_stop_create_field_has_a_description`` in
``test_stop_model.py`` and on the ``TripOut``/``BikeOut`` pair in
``test_trip_metadata_endpoint.py``: a sweep over ``model_fields`` rather than a
list of field names, so the test catches the *next* field added without a
description instead of only the ones missing today.

Both models are referenced by live routes (``POST`` and ``PATCH
/trips/{slug}/bikes``), so unlike ``StopCreate`` they do reach
``app.openapi()["components"]["schemas"]`` — the spec-level half of the ratchet
is assertable here and is asserted separately from the model-level half. A
description that exists on the field but never reaches the document is the
failure that matters: the document is the only thing Kubb reads.

``BikeOut`` is not covered here — it already has its own ratchet in
``test_trip_metadata_endpoint.py`` and a second copy would just be two places to
update.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.main
from app.models.bike import BikeCreate, BikePatch

WRITE_MODELS = [BikeCreate, BikePatch]


def _openapi() -> dict[str, Any]:
    """The served OpenAPI document, as ``/openapi.json`` renders it."""
    return app.main.app.openapi()


@pytest.mark.parametrize("model", WRITE_MODELS)
def test_every_write_model_field_has_a_real_description(model: type) -> None:
    """
    Every field of both write models carries a description, not just a type.

    The length floor is the house one (``BikeOut``, ``StopCreate``): a
    ``description="make"`` satisfies a presence check while telling a reader
    nothing they could not already see from the field name.
    """
    for name, field in model.model_fields.items():
        assert field.description, f"{model.__name__}.{name} has no description"
        assert len(field.description) > 25, f"{model.__name__}.{name}: description is a stub"


@pytest.mark.parametrize("model", WRITE_MODELS)
def test_descriptions_survive_into_the_openapi_document(model: type) -> None:
    """
    ...and they reach the emitted spec, which is the only place they are consumed.

    Asserted against ``app.openapi()`` rather than ``model_json_schema()``
    because the document is what Kubb generates from. For ``BikePatch`` the
    property is an ``anyOf`` of the type and ``null``; the description has to sit
    on the property itself to be visible there, and a description nested inside
    one of the ``anyOf`` branches would not be.
    """
    properties = _openapi()["components"]["schemas"][model.__name__]["properties"]

    for name in model.model_fields:
        assert properties[name].get("description"), f"{model.__name__}.{name} lost its description"


@pytest.mark.parametrize("field_name", list(BikePatch.model_fields))
def test_every_bike_patch_description_states_the_partial_update_semantics(field_name: str) -> None:
    """
    Each ``BikePatch`` description explains *omission*, and that omitted is not null.

    This is the substance of the model's contract rather than a style rule.
    ``docs/api-contract.md`` (``PATCH /trips/{slug}/bikes/{id}``): "only fields
    actually present in the request body change. Absent fields are left alone,
    and are not the same as a field explicitly set to null." A generated client
    sees ``str | None`` and cannot infer any of that — the description is the
    only carrier, which is why a per-field assertion exists instead of trusting
    the presence check above.

    Word-level and case-insensitive, not a fixed sentence: the wording is free to
    change, the two ideas are not.
    """
    description = BikePatch.model_fields[field_name].description or ""
    text = description.lower()

    assert "omit" in text, f"BikePatch.{field_name}: says nothing about omitting the field"
    assert "null" in text, f"BikePatch.{field_name}: does not distinguish omitted from null"


def test_bike_write_models_are_both_published_in_the_document() -> None:
    """
    Both models are actually in ``components.schemas`` — the premise of the ratchet.

    Without this, a route that stopped referencing ``BikePatch`` would drop it
    from the document and the spec-level test above would fail with a ``KeyError``
    that reads like a missing description rather than a missing schema.
    """
    assert {"BikeCreate", "BikePatch"} <= set(_openapi()["components"]["schemas"])
