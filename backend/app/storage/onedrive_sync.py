"""
Write-only OneDrive archive sync -- pushes photo bytes from S3 to OneDrive.

**Context.** S3-compatible storage is the source of truth for photo bytes (spec
Section 4, "Storage"); OneDrive is a second copy the rider can browse from a
phone. This module is *write-only and never a read dependency*: nothing under
``app/api/`` imports it, no request awaits it, and a photo serves fine from S3
whether or not it has ever been archived. It deletes nothing, from S3 or from
Postgres. A photo that fails every run stays pending forever and is never lost.

**How it works.** ``python -m app.storage.onedrive_sync`` runs exactly one pass
and exits -- 0 when everything selected archived (or nothing was pending),
non-zero when a photo failed or the run aborted. Backoff is the *next scheduled
run*, not a sleep loop: there is no queue, cursor or lease, so "still needs
archiving" is a pure query over current state
(``photos.one_drive_file_id IS NULL``) and a crashed run simply leaves the row
for the next one. That makes a whole-run retry free, which is why the whole
design leans on it.

One pass: one access token (refresh-token grant), then per photo -- read the
object from S3 by ``object_key``, PUT it to Graph under
``settings.graph_onedrive_folder`` as ``{photo id}{sniffed extension}``, and on
2xx record the returned Graph file id immediately. Per-photo failures are
logged and skipped; a lapsed refresh token (401 twice) or throttling (429/503)
aborts the whole run because continuing just burns requests.

Uploads carry ``@microsoft.graph.conflictBehavior=replace``. Graph's default is
``fail``: the crash window between a successful upload and the ``UPDATE``
committing would otherwise 409 ``nameAlreadyExists`` on every subsequent run
and strand the photo pending forever.

No secret is ever logged -- not the refresh token, not the access token, not an
``Authorization`` header, including on the paths that log a failed request.

**Related APIs.** Reads its work list through
``app.data.repositories.photos.list_pending_archive`` and records results
through ``mark_archived`` (both wired in ``main()``, not in library code here).
Reads bytes through ``app.storage.s3_client``. Writes to Microsoft Graph.
Consumed by no route. Whether real Graph accepts this exact request shape (URL
form, token scope, ``conflictBehavior`` placement) is **unverified** -- no
``GRAPH_*`` credentials exist yet; ``t-onedrive-preflight-check`` is the human
step that closes that.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.storage.s3_client import BUCKET_NAME, get_s3_client

logger = logging.getLogger(__name__)

# Photos per run. A module constant, not configuration: the only thing tuning it
# changes is how much a single cron tick chews through, and the next tick picks
# up the rest either way.
BATCH_LIMIT = 50

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_DRIVE_URL = "https://graph.microsoft.com/v1.0/me/drive"

# Graph's simple-upload endpoint. `ponytail: simple PUT, no upload session --
# swap to POST /createUploadSession if real photos exceed what this accepts.`
#
# conflictBehavior is spelled into the URL rather than passed as `params=`
# because httpx percent-encodes the leading `@` of a param *key* (`%40microsoft
# .graph.conflictBehavior`). That should be equivalent, but Graph's docs show
# the literal form and nothing here has been tried against real Graph yet, so
# this sends exactly what the docs describe.
UPLOAD_URL = GRAPH_DRIVE_URL + "/root:/{path}:/content?@microsoft.graph.conflictBehavior=replace"


class PendingPhoto(Protocol):
    """What this module needs of a pending row: an id and an S3 object key."""

    id: str
    object_key: str


def _extension(head: bytes) -> str:
    """
    File extension sniffed from the first bytes of the object.

    ``object_key`` has no extension and the upload endpoint stores no content
    type, so a name derived from the photo id alone archives extension-less
    blobs -- no thumbnails, no viewer. Deterministic per id and content, so the
    same photo gets the same name on every run.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heif"):
        return ".heic"
    return ".bin"


def _read_object(s3, key: str) -> bytes:
    """Whole object from S3. Sync (boto3) -- called via to_thread."""
    return s3.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()


async def _fetch_token(client: httpx.AsyncClient) -> str | None:
    """
    One access token from the refresh-token grant, or None (logged) on failure.

    Logs a status code, never a token or a credential.
    """
    settings = get_settings()
    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.graph_client_id,
                "client_secret": settings.graph_client_secret,
                "refresh_token": settings.graph_refresh_token,
            },
        )
    except httpx.HTTPError as exc:
        logger.error("Graph token request failed: %s", type(exc).__name__)
        return None
    if not response.is_success:
        logger.error("Graph token request rejected: HTTP %s", response.status_code)
        return None
    # Graph rotates the refresh token on every redemption and returns the new one
    # here. Deliberately unused and persisted nowhere -- this process writes no
    # config. The configured token stays valid until it lapses; that is a human
    # re-consent step, not something this job can fix.
    return response.json()["access_token"]


async def _upload(
    client: httpx.AsyncClient, token: str, filename: str, body: bytes
) -> httpx.Response:
    folder = get_settings().graph_onedrive_folder.strip("/")
    return await client.put(
        UPLOAD_URL.format(path=f"{folder}/{filename}"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
        content=body,
    )


async def archive_photos(
    client: httpx.AsyncClient,
    photos: list[PendingPhoto],
    record: Callable[[str, str], Awaitable[int]],
) -> int:
    """
    Archive each photo to OneDrive. Returns a process exit code (0 = all done).

    ``client`` is the only injection point -- the real one is built in
    ``main()``, a test supplies one over ``httpx.MockTransport`` so the request
    this code actually constructs is what gets asserted. ``record`` is called
    with ``(photo id, Graph file id)`` the moment an upload succeeds, never
    batched to the end of the run: a crash must not lose an archive that
    already happened. This function knows nothing about the database.
    """
    token = await _fetch_token(client)
    if token is None:
        logger.error("No Graph access token -- run aborted, nothing archived")
        return 1

    s3 = get_s3_client()
    failures = 0

    for photo in photos:
        body = await asyncio.to_thread(partial(_read_object, s3, photo.object_key))
        filename = f"{photo.id}{_extension(body[:12])}"
        try:
            response = await _upload(client, token, filename, body)
            if response.status_code == 401:
                # The access token expired mid-run. Exactly one refresh, one retry.
                token = await _fetch_token(client)
                if token is None:
                    return 1
                response = await _upload(client, token, filename, body)
                if response.status_code == 401:
                    logger.error(
                        "Graph rejected a freshly issued token on photo %s -- the refresh "
                        "token has lapsed; aborting run",
                        photo.id,
                    )
                    return 1
        except httpx.HTTPError as exc:
            logger.error(
                "Upload failed for photo %s: %s -- left pending", photo.id, type(exc).__name__
            )
            failures += 1
            continue

        if response.status_code in (429, 503):
            logger.error(
                "Graph is throttling or unavailable (HTTP %s, Retry-After: %s) on photo %s -- "
                "aborting run; the remaining photos stay pending for the next one",
                response.status_code,
                response.headers.get("Retry-After", "not sent"),
                photo.id,
            )
            return 1

        if not response.is_success:
            logger.error(
                "Upload failed for photo %s: HTTP %s -- left pending",
                photo.id,
                response.status_code,
            )
            failures += 1
            continue

        if await record(photo.id, response.json()["id"]) == 0:
            logger.warning(
                "Photo %s was gone by the time it archived -- its OneDrive copy is orphaned",
                photo.id,
            )

    return 1 if failures else 0


async def main() -> int:
    """One pass: select pending photos, archive them, exit code back to the shell."""
    if not get_settings().graph_refresh_token:
        logger.info(
            "OneDrive archiving is not configured (GRAPH_REFRESH_TOKEN is empty) -- nothing to do"
        )
        return 0

    # Imported here, not at module scope: everything above is library code that
    # never reaches into data/. The wiring lives in the entry point.
    from app.data.db import async_session
    from app.data.repositories.photos import list_pending_archive, mark_archived

    async with async_session() as session:
        photos = await list_pending_archive(session, BATCH_LIMIT)
        if not photos:
            logger.info("No photos pending archive")
            return 0

        logger.info("Archiving %d photo(s) to OneDrive", len(photos))
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await archive_photos(client, photos, partial(mark_archived, session))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Exactly one asyncio.run() in the process, and no second loop anywhere: the
    # engine's asyncpg connections bind to the loop that creates them
    # (app/data/db.py). A fresh `python -m` process per run is the whole scheduler.
    raise SystemExit(asyncio.run(main()))
