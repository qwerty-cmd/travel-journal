"""
Photo reads and creates -- photos belonging to one stop on one trip.

Same boundaries as ``stops.py``:

- **Only layer that names database columns.** What leaves is ``PhotoOut``.
- **Never raises ``ApiError``.** ``check_id_conflict`` returns a bool, and
  ``stop_belongs_to_trip`` returns a bool -- the route decides the HTTP status.

Presigned URLs are generated here because ``PhotoOut.url`` is derived from
``object_key`` at read time and the DB never stores a URL
(``docs/api-contract.md``, "Photo serving").
"""

from __future__ import annotations

import asyncio
from functools import partial

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.tables import photos, stops
from app.models.photo import PhotoOut
from app.storage.s3_client import BUCKET_NAME, get_s3_client


async def stop_belongs_to_trip(session: AsyncSession, trip_id: str, stop_id: str) -> bool:
    """True when this stop exists and is on this trip."""
    return bool(
        await session.scalar(
            select(exists().where(stops.c.id == stop_id, stops.c.trip_id == trip_id))
        )
    )


def _presign(s3, key: str) -> str:
    """Presigned GET URL for one object key. Sync -- called via to_thread."""
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=3600,
    )


async def _presign_async(s3, key: str) -> str:
    return await asyncio.to_thread(partial(_presign, s3, key))


def _row_to_photo_sync(row, url: str) -> PhotoOut:
    """Map a DB row + presigned URL to PhotoOut."""
    return PhotoOut(
        id=row.id,
        stopId=row.stop_id,
        url=url,
        uploadedBy=row.uploaded_by,
        takenAt=row.taken_at,
        archived=row.one_drive_file_id is not None,
    )


async def list_by_stop(session: AsyncSession, stop_id: str) -> list[PhotoOut]:
    """Every photo on this stop, with presigned URLs. Empty list if none."""
    statement = (
        select(
            photos.c.id,
            photos.c.stop_id,
            photos.c.object_key,
            photos.c.one_drive_file_id,
            photos.c.uploaded_by,
            photos.c.taken_at,
        )
        .where(photos.c.stop_id == stop_id)
        .order_by(photos.c.taken_at, photos.c.id)
    )

    rows = (await session.execute(statement)).all()
    if not rows:
        return []

    s3 = get_s3_client()
    results: list[PhotoOut] = []
    for row in rows:
        url = await _presign_async(s3, row.object_key)
        results.append(_row_to_photo_sync(row, url))
    return results


async def find_existing(
    session: AsyncSession, stop_id: str, photo_id: str,
) -> PhotoOut | None:
    """
    If this photo id already exists on this stop, return the stored PhotoOut.
    Returns None if not found on this stop. Does NOT check other stops.
    """
    stored = (
        await session.execute(
            select(
                photos.c.id,
                photos.c.stop_id,
                photos.c.object_key,
                photos.c.one_drive_file_id,
                photos.c.uploaded_by,
                photos.c.taken_at,
            ).where(photos.c.stop_id == stop_id, photos.c.id == photo_id)
        )
    ).first()

    if stored is None:
        return None

    s3 = get_s3_client()
    url = await _presign_async(s3, stored.object_key)
    return _row_to_photo_sync(stored, url)


async def check_id_conflict(session: AsyncSession, photo_id: str) -> bool:
    """True if this photo id exists anywhere (any stop)."""
    return bool(await session.scalar(select(exists().where(photos.c.id == photo_id))))


async def insert(
    session: AsyncSession, stop_id: str, photo_id: str, uploaded_by: str,
    taken_at, object_key: str,
) -> PhotoOut:
    """
    Insert a new photo row. Caller must have already checked for replay/conflict.
    """
    await session.execute(
        photos.insert().values(
            id=photo_id,
            stop_id=stop_id,
            object_key=object_key,
            uploaded_by=uploaded_by,
            taken_at=taken_at,
        )
    )
    await session.commit()

    s3 = get_s3_client()
    url = await _presign_async(s3, object_key)
    return PhotoOut(
        id=photo_id,
        stopId=stop_id,
        url=url,
        uploadedBy=uploaded_by,
        takenAt=taken_at,
        archived=False,
    )
