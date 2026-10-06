"""
Validation boundaries of the Entry 29 contract models (task `t-am-contract-models`).

No routes use these yet, so they are exercised at the model level. Every
boundary the contract names is tested on both sides: username regex after
lowercasing, password 15-128 code points after NFKC with no `Cc`, displayName
1-40 after trimming, join message <= 280, canonical-UUID `TripCreate.id`, and
`TripPatch`'s explicit-null rejection.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.models import account, join_request, member, trip
from app.models.account import AccountCreate, AccountRecover, PasswordChange
from app.models.join_request import JoinRequestCreate
from app.models.trip import TripCreate, TripPatch, Visibility

GOOD_PASSWORD = "correct horse battery"  # 21 chars
GOOD_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _account(**overrides: str) -> AccountCreate:
    body = {"username": "rider_one", "displayName": "Ann", "password": GOOD_PASSWORD}
    body.update(overrides)
    return AccountCreate.model_validate(body)


# -- descriptions ------------------------------------------------------------

NEW_MODELS = [
    obj
    for module in (account, join_request, member)
    for obj in vars(module).values()
    if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == module.__name__
] + [
    trip.ViewerOut,
    trip.TripCreate,
    trip.TripPatch,
    trip.TripSummaryOut,
    trip.TripPageOut,
    trip.MyTripOut,
]


@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.__name__)
def test_every_new_model_field_has_a_real_description(model: type[BaseModel]) -> None:
    for name, field in model.model_fields.items():
        assert field.description, f"{model.__name__}.{name} has no description"
        assert len(field.description) > 25, f"{model.__name__}.{name}: description is a stub"


# -- username ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("abc", "abc"), ("Rider_One", "rider_one"), ("0.a-b", "0.a-b"), ("a" * 32, "a" * 32)],
)
def test_username_is_lowercased_and_accepted(raw: str, stored: str) -> None:
    assert _account(username=raw).username == stored


@pytest.mark.parametrize(
    "raw",
    ["ab", "a" * 33, "_abc", ".abc", "-abc", "ab c", "abc!", "", " abc", "Kelvin", "émile"],
)
def test_username_outside_the_pattern_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        _account(username=raw)


# -- password ----------------------------------------------------------------


def test_password_length_boundaries() -> None:
    assert _account(password="x" * 15).password == "x" * 15
    assert _account(password="x" * 128).password == "x" * 128
    for bad in ("x" * 14, "x" * 129):
        with pytest.raises(ValidationError):
            _account(password=bad)


def test_password_length_is_counted_after_nfkc() -> None:
    # U+FB01 LATIN SMALL LIGATURE FI is one code point raw, two after NFKC.
    fourteen_raw = "ﬁ" + "x" * 13  # 14 raw -> 15 normalised
    assert _account(password=fourteen_raw).password == "fi" + "x" * 13
    at_limit_raw = "ﬁ" + "x" * 127  # 128 raw -> 129 normalised
    with pytest.raises(ValidationError):
        _account(password=at_limit_raw)


def test_password_is_not_trimmed() -> None:
    assert _account(password="  " + "x" * 13).password == "  " + "x" * 13


@pytest.mark.parametrize("control", ["\x00", "\n", "\t", "\x7f", "\x85"])
def test_password_with_a_cc_character_is_rejected(control: str) -> None:
    with pytest.raises(ValidationError):
        _account(password="x" * 20 + control)


def test_new_password_rule_applies_to_recover_and_change() -> None:
    with pytest.raises(ValidationError):
        AccountRecover(username="a", recoveryCode="c", newPassword="short")
    with pytest.raises(ValidationError):
        PasswordChange(currentPassword="anything", newPassword="short")
    # The presented credential is not format-checked.
    assert PasswordChange(currentPassword="x", newPassword=GOOD_PASSWORD).currentPassword == "x"


# -- displayName -------------------------------------------------------------


def test_display_name_is_trimmed_and_bounded() -> None:
    assert _account(displayName="  Ann  ").displayName == "Ann"
    assert _account(displayName="A").displayName == "A"
    assert _account(displayName="a" * 40).displayName == "a" * 40
    for bad in ("", "   ", "a" * 41, "Ann\x00", "Ann\u0007Bee"):
        with pytest.raises(ValidationError):
            _account(displayName=bad)


# -- join message -------------------------------------------------------------


def test_join_message_boundaries() -> None:
    assert JoinRequestCreate().message is None
    assert JoinRequestCreate(message=None).message is None
    assert JoinRequestCreate(message="   ").message is None
    assert JoinRequestCreate(message="  hi  ").message == "hi"
    assert JoinRequestCreate(message="m" * 280).message == "m" * 280
    assert JoinRequestCreate(message=" " + "m" * 280 + " ").message == "m" * 280
    with pytest.raises(ValidationError):
        JoinRequestCreate(message="m" * 281)


# -- TripCreate ---------------------------------------------------------------


def test_trip_create_accepts_a_canonical_uuid_and_defaults_public() -> None:
    body = TripCreate(id=GOOD_ID, name=" Outback ", startDate="2026-10-01")
    assert body.id == GOOD_ID
    assert body.name == "Outback"
    assert body.visibility is Visibility.PUBLIC


@pytest.mark.parametrize(
    "bad_id",
    [
        GOOD_ID.upper(),
        "{" + GOOD_ID + "}",
        GOOD_ID.replace("-", ""),
        GOOD_ID + "\n",
        " " + GOOD_ID,
        GOOD_ID[:-1],
        "not-a-uuid",
    ],
)
def test_trip_create_rejects_a_non_canonical_id(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        TripCreate(id=bad_id, name="Outback", startDate="2026-10-01")


def test_trip_name_boundaries() -> None:
    assert TripCreate(id=GOOD_ID, name="n" * 100, startDate="2026-10-01").name == "n" * 100
    for bad in ("", "  ", "n" * 101):
        with pytest.raises(ValidationError):
            TripCreate(id=GOOD_ID, name=bad, startDate="2026-10-01")


# -- TripPatch ----------------------------------------------------------------


def test_trip_patch_empty_body_sets_nothing() -> None:
    assert TripPatch.model_validate({}).model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize("field", ["name", "visibility", "publicDelayHours"])
def test_trip_patch_explicit_null_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        TripPatch.model_validate({field: None})


def test_trip_patch_delay_boundaries() -> None:
    assert TripPatch(publicDelayHours=0).publicDelayHours == 0
    assert TripPatch(publicDelayHours=168).publicDelayHours == 168
    for bad in (-1, 169):
        with pytest.raises(ValidationError):
            TripPatch(publicDelayHours=bad)


def test_trip_patch_schema_has_no_null_default() -> None:
    properties = TripPatch.model_json_schema()["properties"]
    for name, schema in properties.items():
        assert "default" not in schema, name
        assert "anyOf" not in schema, name
