"""
``StopCreate`` — the create model's field contract, asserted against the ruling.

Written **contract-first** from ``docs/api-contract.md`` §"``StopCreate.arrivedAt``
must be timezone-aware" and ``docs/decision-log.md`` Entry 15, not from
``backend/app/models/stop.py``. A stop filed at the wrong hour is a data-integrity
failure (spec Section 12's top tier), and a test derived from the model would
encode whatever the model does as the expected answer — including the thing the
ruling exists to forbid.

What the contract promises here:

1. ``arrivedAt`` is a timezone-aware instant. A **naive** datetime is rejected
   with ``422`` / ``VALIDATION_ERROR``. Not defaulted to UTC, not assumed to be
   the server's local time, not assumed to be a fixed trip offset.
2. **Any** offset is acceptable, not only UTC — the rider crosses timezone
   boundaries mid-trip and syncs later, so the offset that arrives is whatever
   the device was on at capture.
3. Every field carries a real ``description=`` (CLAUDE.md, "Conventions"), which
   is what feeds the OpenAPI document and Kubb's generated types.

The ``422`` of the contract is what a Pydantic ``ValidationError`` becomes once a
route parses a body into this model. There is no such route yet, so these tests
assert at the model layer — the layer that actually does the rejecting either way,
since the value fails schema validation *before* any handler runs.

**Two things deliberately not asserted here**, both because they cannot be
asserted honestly rather than because they were forgotten:

- **The OpenAPI document.** ``StopCreate`` is referenced by no route today, so it
  is absent from ``app.openapi()["components"]["schemas"]`` — the spec-level half
  of the description ratchet (as in
  ``test_every_stop_out_field_has_a_description_that_reaches_the_spec``) is not
  reachable and belongs to ``t-stops-create-endpoint``, which lands the route.
  ``model_json_schema()`` is used instead: it proves the description survives
  schema generation, which is the part that is true today.
- **Timezone-awareness via the schema.** JSON Schema has no vocabulary for it.
  ``AwareDatetime`` and a hand-written field validator emit the *identical*
  ``{"type": "string", "format": "date-time"}``. A test asserting the schema
  proves the constraint would pass against a relaxed bare ``datetime`` — i.e. it
  would be green against the exact regression this file exists to catch. The
  enforcement is server-side only (Entry 15), so only a validation call can
  observe it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models.stop import StopCreate

# Kathmandu, +05:45 — a real, non-whole-hour, non-UTC offset. A whole-hour
# offset would still catch a UTC-only special case, but this also catches an
# implementation that normalised through an hours-only conversion.
NEPAL = timezone(timedelta(hours=5, minutes=45))


def _payload(arrived_at: datetime | str) -> dict[str, object]:
    """
    A valid create body in its **wire** spelling, varying only ``arrivedAt``.

    camelCase keys and ``model_validate`` rather than keyword construction: the
    contract governs what arrives over HTTP, and this is the form the route will
    hand the model. Everything but ``arrivedAt`` is held constant so a failure
    can only be about the timestamp.
    """
    return {
        "id": "stop-01JCONTRACT0000000000000",
        "name": "Col du Galibier",
        "lat": 45.0637,
        "lng": 6.4075,
        "locationSource": "gps",
        "arrivedAt": arrived_at,
        "notes": "Cold at the top.",
    }


def test_arrived_at_with_a_utc_offset_validates() -> None:
    """An offset-carrying ``arrivedAt`` is accepted and keeps its offset."""
    arrived = datetime(2026, 6, 14, 9, 30, tzinfo=UTC)

    stop = StopCreate.model_validate(_payload(arrived))

    assert stop.arrivedAt.utcoffset() is not None
    assert stop.arrivedAt == arrived


def test_naive_arrived_at_is_rejected() -> None:
    """
    A naive ``arrivedAt`` — no offset — is rejected, and rejected *for that*.

    ``ValidationError`` precisely, not "it raised": a ``TypeError`` from a broken
    helper or a ``ValueError`` from somewhere else would satisfy a bare
    ``pytest.raises(Exception)`` while proving nothing. And the error is pinned to
    ``arrivedAt`` alone — a model that started rejecting this payload for an
    unrelated reason (a renamed field, a tightened ``id``) would otherwise look
    like a passing timezone guard.

    This is the ``422`` / ``VALIDATION_ERROR`` of the endpoint table, seen at the
    layer that produces it. Entry 15: ``VALIDATION_ERROR`` is never-retry, so the
    offline queue *drops* such a stop and surfaces it — accepted deliberately,
    because a surfaced error is recoverable and a stop silently filed at the
    wrong hour is not.
    """
    naive = datetime(2026, 6, 14, 9, 30)  # noqa: DTZ001 - a naive value is the input under test
    assert naive.tzinfo is None  # the premise of the test, not an assumption

    with pytest.raises(ValidationError) as caught:
        StopCreate.model_validate(_payload(naive))

    errors = caught.value.errors()
    assert len(errors) == 1, errors
    assert errors[0]["loc"] == ("arrivedAt",), errors
    # Spelling-agnostic: ``AwareDatetime`` reports type ``timezone_aware`` /
    # "Input should have timezone info"; a hand-written validator is free to word
    # it differently, and the contract allows either. What is pinned is that the
    # complaint is about the missing offset and not about some other property of
    # the same field.
    complaint = f"{errors[0]['type']} {errors[0]['msg']}".lower()
    assert any(word in complaint for word in ("timezone", "aware", "offset")), errors


def test_arrived_at_with_a_non_utc_offset_validates() -> None:
    """
    ``+05:45`` is accepted — the rule is "has an offset", not "is UTC".

    The rider crosses timezone boundaries mid-trip and syncs later, so the offset
    that arrives is the device's at capture. An implementation that special-cased
    UTC would pass every test above this one and reject real traffic from most of
    the planet.
    """
    arrived = datetime(2026, 6, 14, 15, 15, tzinfo=NEPAL)

    stop = StopCreate.model_validate(_payload(arrived))

    assert stop.arrivedAt.utcoffset() == timedelta(hours=5, minutes=45)
    # Same instant either way — asserted as an instant rather than as a wall
    # clock, so normalising to UTC on the way in would also be correct.
    assert stop.arrivedAt == datetime(2026, 6, 14, 9, 30, tzinfo=UTC)


def test_arrived_at_as_an_iso_string_with_an_offset_validates() -> None:
    """
    The same value as JSON actually carries it: a string with an offset.

    Every ``datetime`` above is a Python object a route will never receive. This
    is the wire form, and it is the form the naive rejection has to survive
    without also rejecting — a guard that only inspected ``datetime`` instances
    would be invisible here.
    """
    stop = StopCreate.model_validate(_payload("2026-06-14T15:15:00+05:45"))

    assert stop.arrivedAt == datetime(2026, 6, 14, 9, 30, tzinfo=UTC)


def test_every_stop_create_field_has_a_description() -> None:
    """
    Every ``StopCreate`` field carries a real description, surviving into schema.

    A ratchet over ``model_fields`` rather than a list of field names, mirroring
    ``test_every_stop_out_field_has_a_description_that_reaches_the_spec``: the
    point is to catch the *next* field added without one. ``arrivedAt`` losing its
    description is how this model came to disagree with its own contract in the
    first place (Entry 15) — an enumerated test would have been green throughout.

    A length floor because ``description="id"`` satisfies a presence check while
    saying nothing.

    The spec-level half of the sibling test is absent on purpose: no route
    references ``StopCreate``, so it is not in the OpenAPI document at all and
    ``app.openapi()[...]["StopCreate"]`` raises ``KeyError``. That assertion is
    ``t-stops-create-endpoint``'s to make, once the route puts it there.
    ``model_json_schema()`` covers what is observable now.
    """
    properties = StopCreate.model_json_schema()["properties"]

    for name, field in StopCreate.model_fields.items():
        assert field.description, f"StopCreate.{name} has no description"
        assert len(field.description) > 25, f"StopCreate.{name}: description is a stub"
        key = field.alias or name
        assert properties[key].get("description"), f"StopCreate.{name} lost its description"
