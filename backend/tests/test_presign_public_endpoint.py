"""
Presigned photo URLs are signed for the host the *browser* reaches storage at.

Inside docker compose the API talks to storage at ``http://minio:9000``, a name
no browser resolves. ``S3_PUBLIC_ENDPOINT_URL`` names the host the browser uses,
and presigning must happen against a client configured for it: SigV4 signs the
``Host`` header, so rewriting the host of a URL signed for another endpoint
leaves a signature that no longer matches.

**The local moto server does not verify signatures** -- a tampered or
host-swapped URL still returns 200 -- so fetching the URL proves only that the
host is reachable and the key is right. Signature validity for the public host
is therefore checked by recomputing SigV4 independently here (``_sigv4_valid``),
rather than trusting the storage server to reject a bad one.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
import pytest
from conftest import SeededTrip
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.data import tables
from app.data.repositories import photos as photos_repo
from app.storage import s3_client
from app.storage.s3_client import BUCKET_NAME, get_presign_client, get_s3_client

PUBLIC_ENDPOINT = "http://localhost:9000"


def _sigv4_valid(url: str, host: str, secret_key: str) -> bool:
    """
    True when the presigned GET ``url`` carries a valid SigV4 query signature
    for a request whose Host header is ``host``. Written from the SigV4 spec,
    not by calling botocore, so it cannot share a bug with the code under test.
    """
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    signature = dict(params)["X-Amz-Signature"]
    unsigned = sorted((k, v) for k, v in params if k != "X-Amz-Signature")
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in unsigned
    )
    assert dict(params)["X-Amz-SignedHeaders"] == "host"
    canonical_request = "\n".join(
        ["GET", parts.path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"]
    )
    amz_date = dict(params)["X-Amz-Date"]
    scope = dict(params)["X-Amz-Credential"].split("/", 1)[1]
    date_stamp, region, service, terminator = scope.split("/")
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = f"AWS4{secret_key}".encode()
    for part in (date_stamp, region, service, terminator):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@pytest.fixture
def public_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """``S3_PUBLIC_ENDPOINT_URL`` set, as ``s3_client`` sees it (it snapshots settings)."""
    monkeypatch.setattr(
        s3_client,
        "_settings",
        s3_client._settings.model_copy(update={"s3_public_endpoint_url": PUBLIC_ENDPOINT}),
    )
    yield PUBLIC_ENDPOINT


@pytest.fixture
def no_public_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        s3_client,
        "_settings",
        s3_client._settings.model_copy(update={"s3_public_endpoint_url": None}),
    )


@pytest.fixture
def stored_object(s3_bucket: None) -> Iterator[tuple[str, bytes]]:
    key = f"test-presign/{secrets.token_urlsafe(8)}"
    body = secrets.token_bytes(64)
    s3 = get_s3_client()
    s3.put_object(Bucket=BUCKET_NAME, Key=key, Body=body)
    try:
        yield key, body
    finally:
        s3.delete_object(Bucket=BUCKET_NAME, Key=key)


def _presign(key: str) -> str:
    return get_presign_client().generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET_NAME, "Key": key}, ExpiresIn=300
    )


def test_presigned_url_uses_public_host_and_serves_the_bytes(
    public_endpoint: str, stored_object: tuple[str, bytes]
) -> None:
    key, body = stored_object
    url = _presign(key)

    assert urlsplit(url).netloc == "localhost:9000"
    response = httpx.get(url)
    assert response.status_code == 200
    assert response.content == body


def test_presigned_signature_is_valid_for_the_public_host_only(
    public_endpoint: str, stored_object: tuple[str, bytes]
) -> None:
    """Signed for the public Host, so a host-rewritten URL would not verify."""
    key, _ = stored_object
    url = _presign(key)
    secret = s3_client._settings.s3_secret_access_key

    assert _sigv4_valid(url, "localhost:9000", secret)
    assert not _sigv4_valid(url, urlsplit(s3_client._settings.s3_endpoint_url).netloc, secret)


def test_without_public_endpoint_presigns_against_the_normal_endpoint(
    no_public_endpoint: None, stored_object: tuple[str, bytes]
) -> None:
    key, body = stored_object
    url = _presign(key)
    endpoint_host = urlsplit(s3_client._settings.s3_endpoint_url).netloc

    assert urlsplit(url).netloc == endpoint_host
    assert _sigv4_valid(url, endpoint_host, s3_client._settings.s3_secret_access_key)
    assert httpx.get(url).content == body


def test_upload_client_ignores_the_public_endpoint(public_endpoint: str) -> None:
    """Uploads and reads keep going to S3_ENDPOINT_URL."""
    assert get_s3_client().meta.endpoint_url == s3_client._settings.s3_endpoint_url
    assert get_presign_client().meta.endpoint_url == PUBLIC_ENDPOINT


async def test_photo_repository_returns_urls_on_the_public_host(
    public_endpoint: str,
    stored_object: tuple[str, bytes],
    seeded_trips: list[SeededTrip],
    migrated_engine: AsyncEngine,
) -> None:
    """The repository's ``PhotoOut.url`` -- what the API serves -- uses the public host."""
    key, body = stored_object
    stop_id = f"test-stop-{secrets.token_urlsafe(8)}"
    photo_id = f"test-photo-{secrets.token_urlsafe(8)}"
    now = datetime.now(UTC)
    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=seeded_trips[0].id,
                name="Presign stop",
                lat=0.0,
                lng=0.0,
                location_source="gps",
                arrived_at=now,
            )
        )
        await conn.execute(
            tables.photos.insert().values(
                id=photo_id,
                stop_id=stop_id,
                object_key=key,
                uploaded_by="rider",
                taken_at=now,
            )
        )

    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        [photo] = await photos_repo.list_by_stop(session, stop_id)

    assert urlsplit(photo.url).netloc == "localhost:9000"
    # Fetches the presigned URL from the object store, not our app: no app-client factory.
    async with httpx.AsyncClient() as http:
        assert (await http.get(photo.url)).content == body
