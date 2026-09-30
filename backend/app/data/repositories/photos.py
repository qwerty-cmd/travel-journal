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
from dataclasses import dataclass
from functools import partial

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.repositories.stops import public_visibility
from app.data.tables import photos, stops
from app.models.photo import PhotoOut
from app.storage.s3_client import BUCKET_NAME, get_presign_client


async def stop_belongs_to_trip(
    session: AsyncSession, trip_id: str, stop_id: str, *, public_delay_hours: int | None = None
) -> bool:
    """
    True when this stop exists and is on this trip -- and, given ``public_delay_hours``,
    is visible to the public (``stops.public_visibility``).

    A stop hidden by the delay is ``False`` exactly like one that doesn't exist,
    so its photos are unreachable to a non-member and the route's ``404`` is the
    same in both cases.

    A NUL byte is ``False`` without a query: Postgres ``text`` cannot hold one,
    so the driver would raise and the read would be a ``500``, not the unknown
    stop's ``404`` (the same guard as ``trips.get_by_slug`` / ``get_by_id``).
    ``%00`` in the path reaches here decoded.
    """
    if "\x00" in stop_id:
        return False
    return bool(
        await session.scalar(
            select(
                exists().where(
                    stops.c.id == stop_id,
                    stops.c.trip_id == trip_id,
                    public_visibility(public_delay_hours),
                )
            )
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

    s3 = get_presign_client()
    results: list[PhotoOut] = []
    for row in rows:
        url = await _presign_async(s3, row.object_key)
        results.append(_row_to_photo_sync(row, url))
    return results


async def find_existing(
    session: AsyncSession,
    stop_id: str,
    photo_id: str,
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

    s3 = get_presign_client()
    url = await _presign_async(s3, stored.object_key)
    return _row_to_photo_sync(stored, url)


async def check_id_conflict(session: AsyncSession, photo_id: str) -> bool:
    """True if this photo id exists anywhere (any stop)."""
    return bool(await session.scalar(select(exists().where(photos.c.id == photo_id))))


async def insert(
    session: AsyncSession,
    stop_id: str,
    photo_id: str,
    uploaded_by: str,
    taken_at,
    object_key: str,
    *,
    created_by: str | None = None,
) -> PhotoOut:
    """
    Insert a new photo row. Caller must have already checked for replay/conflict.

    The ``PhotoOut`` is built from the row as stored (``RETURNING``), not from
    the arguments: ``taken_at`` goes in with the device's offset and comes back
    from ``timestamptz`` as UTC, so echoing the argument made a ``201`` spell
    the same instant differently from its own ``200`` replay.

    ``uploaded_by`` is the uploading account's ``display_name`` at upload time
    and ``created_by`` its id (decision-log Entry 29, contract default 23). The
    upload route always passes both. ``created_by`` defaults to ``None`` only
    because the column is nullable and a photo with no account behind it (a
    pre-0003 row, a repository test) is a legitimate row.
    """
    stored = (
        await session.execute(
            photos.insert()
            .values(
                id=photo_id,
                stop_id=stop_id,
                object_key=object_key,
                uploaded_by=uploaded_by,
                taken_at=taken_at,
                created_by=created_by,
            )
            .returning(
                photos.c.id,
                photos.c.stop_id,
                photos.c.object_key,
                photos.c.one_drive_file_id,
                photos.c.uploaded_by,
                photos.c.taken_at,
            )
        )
    ).one()
    await session.commit()

    s3 = get_presign_client()
    url = await _presign_async(s3, stored.object_key)
    return _row_to_photo_sync(stored, url)


@dataclass(frozen=True, slots=True)
class PendingArchivePhoto:
    """
    One photo the OneDrive sync still has to archive.

    Deliberately not ``PhotoOut``: the sync needs the object key to read the
    bytes back out of S3, and needs no presigned URL at all.
    """

    id: str
    object_key: str


async def list_pending_archive(
    session: AsyncSession,
    limit: int,
) -> list[PendingArchivePhoto]:
    """
    Photos that have never been archived (``one_drive_file_id IS NULL``), across
    every trip and stop -- this is a maintenance sweep, not a slug-scoped read.

    Ordered ``(taken_at, id)`` like ``list_by_stop`` so the sweep is
    deterministic, and capped at ``limit``. There is no queue, cursor or lease:
    "still needs archiving" is a pure query over current state, so a run that
    crashes leaves the row untouched and the next run re-selects it.

    Presigns nothing -- the sync reads bytes from S3 directly.

    ``limit`` must be at least 1: a negative value would otherwise surface as a
    raw driver error, and ``0`` would be a sweep that can never make progress.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit!r}")
    statement = (
        select(photos.c.id, photos.c.object_key)
        .where(photos.c.one_drive_file_id.is_(None))
        .order_by(photos.c.taken_at, photos.c.id)
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return [PendingArchivePhoto(id=row.id, object_key=row.object_key) for row in rows]


async def mark_archived(
    session: AsyncSession,
    photo_id: str,
    one_drive_file_id: str,
) -> int:
    """
    Record that this photo now exists in OneDrive, by setting its
    ``one_drive_file_id`` -- the only column touched, on the only row matched.
    ``PhotoOut.archived`` is derived from it, never stored.

    Returns the number of rows updated: 0 means nothing was written -- either
    the photo is gone (its stop was cascade-deleted mid-run) or it already has
    a ``one_drive_file_id``. The caller can tell from a write without a second
    query.

    The update only matches a row that is still unarchived, so a second write
    (two overlapping sweeps selecting the same row) never replaces a file id
    that is already recorded and leaves the first OneDrive copy unreferenced.
    """
    result = await session.execute(
        photos.update()
        .where(photos.c.id == photo_id, photos.c.one_drive_file_id.is_(None))
        .values(one_drive_file_id=one_drive_file_id)
    )
    await session.commit()
    return result.rowcount
