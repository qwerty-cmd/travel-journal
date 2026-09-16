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

The **upload form fields** are covered here too, as of ``t-takenat-tz-question``.
There is no ``PhotoCreateForm`` model to sweep — it was deleted, because FastAPI
embeds every body field once a route has more than one, so a bound form model
could not coexist with the separate ``File()`` part without nesting the flat
multipart body the contract promises. The route declares ``id``, ``uploadedBy``
and ``takenAt`` as inline ``Annotated[..., Form(...)]`` parameters instead, and
FastAPI publishes them under a synthesised body schema name. The ratchet is
spec-level for the same reason as ``PhotoOut``'s: the document is what Kubb
reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import app.main
from app.api.routes import photos as photo_routes
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


def test_taken_at_description_carries_the_timezone_ruling() -> None:
    """
    ``PhotoOut.takenAt``'s description now states the ruling, rather than dodging it.

    The inversion of ``test_taken_at_description_makes_no_timezone_claim``. That
    test pinned *silence* while the question was open: a description is contract
    text Kubb ships to the client, so a rule stated there would have settled an
    unruled question by the back door. ``t-takenat-tz-question`` ruled — a capture
    time is an offset-aware instant and a naive value is rejected ``422`` /
    ``VALIDATION_ERROR`` — so the same reasoning now runs the other way: silence
    would leave a client author guessing about the one field where a client-side
    mistake permanently dequeues the upload.

    The ruling is cited by **task id** — ``t-takenat-tz-question``, whose entry in
    ``docs/progress-notes.md`` is the one reference that exists and stays put. Its
    ``docs/api-contract.md`` section is written by the ``docs`` agent when this
    task closes, so naming that section here would be a forward reference; the
    line number this test used to carry (``:305``) had already gone stale, which
    is how the pointer got here in the first place.

    Word-level and case-insensitive, so the wording stays free to change; what is
    pinned is that the claim is made at all.
    """
    text = (PhotoOut.model_fields["takenAt"].description or "").lower()

    for claim in ("timezone-aware", "offset", "utc", "422"):
        assert claim in text, f"PhotoOut.takenAt's description dropped the timezone claim: {claim!r}"


# --------------------------------------------------------------------------
# The upload form fields
# --------------------------------------------------------------------------

# FastAPI *synthesises* this schema name from the operation id when a route's
# body is a set of inline Form()/File() params rather than a model. Pinned in
# this one place: it changes if the handler's name, path or signature shape
# changes, and a single constant makes that a one-line fix instead of a hunt.
FORM_SCHEMA = "Body_upload_photo_api_trips__slug__stops__stop_id__photos_post"

FORM_FIELDS = ("id", "uploadedBy", "takenAt")


def _form_properties() -> dict[str, Any]:
    """The upload endpoint's multipart body properties, as the document publishes them."""
    schemas = _openapi()["components"]["schemas"]

    assert FORM_SCHEMA in schemas, (
        f"{FORM_SCHEMA} is not in components.schemas — FastAPI renamed the synthesised "
        "body schema, or the upload route's signature was restructured"
    )
    return schemas[FORM_SCHEMA]["properties"]


def test_the_form_body_is_flat() -> None:
    """
    The multipart body is ``{id, uploadedBy, takenAt, file}`` — four flat fields.

    The premise of every assertion below, and the thing the deleted
    ``PhotoCreateForm`` would have broken: binding a form *model* alongside the
    separate ``File()`` part makes FastAPI embed both, turning the body into
    ``{"form": {...}, "file": ...}`` and rejecting the flat multipart POST the
    contract promises with a ``422``. Pinned so a future refactor back to a bound
    model fails here, loudly, instead of on the wire.
    """
    assert set(_form_properties()) == {"id", "uploadedBy", "takenAt", "file"}


@pytest.mark.parametrize("field_name", FORM_FIELDS)
def test_every_upload_form_field_has_a_real_description(field_name: str) -> None:
    """
    Every upload form field carries a description in the document.

    Same house length floor as ``PhotoOut`` above. ``file`` is excluded only
    because FastAPI owns that part's schema, not because it is undescribed.
    """
    description = _form_properties()[field_name].get("description")

    assert description, f"upload form field {field_name} has no description"
    assert len(description) > 25, f"upload form field {field_name}: description is a stub"


def test_upload_taken_at_description_states_the_rejection_rule() -> None:
    """
    ``takenAt``'s form description tells a client author the offset is mandatory.

    This is the description that matters most on the whole surface. JSON Schema
    has no vocabulary for timezone-awareness, so ``AwareDatetime`` and a bare
    ``datetime`` emit the identical ``{"type": "string", "format": "date-time"}``
    — the generated client cannot catch a naive value, and prose is the *only*
    place the rule is visible before the server rejects it. Since
    ``VALIDATION_ERROR`` is a never-retry code, a client that does not know this
    loses the upload rather than retrying it.
    """
    text = _form_properties()["takenAt"].get("description", "").lower()

    for claim in ("offset", "422", "validation_error", "naive"):
        assert claim in text, f"upload takenAt description omits the rejection rule: {claim!r}"


def test_the_old_placeholder_description_is_gone() -> None:
    """
    The pre-ruling wording was *replaced*, not duplicated alongside the new one.

    "ISO 8601 timestamp when the photo was taken" settles nothing — ISO 8601
    admits both naive and offset-carrying forms — so leaving a second copy of it
    anywhere in the route module gives a reader a contradicting sentence to find
    first.
    """
    source = Path(photo_routes.__file__).read_text(encoding="utf-8")

    assert "ISO 8601 timestamp when the photo was taken" not in source
