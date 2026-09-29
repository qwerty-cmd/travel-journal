"""
The write-only OneDrive archive sync -- ``app/storage/onedrive_sync.py``.

**Written from the acceptance criteria and ``docs/api-contract.md``, not from
the implementation.** This is spec Section 12's data-integrity tier: the thing
that must never happen is a photo that silently stops being owed. OneDrive is
never in a read path (``docs/api-contract.md``, "Photo serving" -- ``PhotoOut.url``
"is never a OneDrive URL", and ``archived`` is "status reporting, not a read
dependency"), so nothing user-facing ever goes red when this job quietly drops
one. The only detector is a test that checks the row.

**Everything is real except one HTTP hop.** Postgres and MinIO are the live
docker-compose services, the photo rows are real rows, the bytes are really in
S3, and where a criterion is about the database the ``record`` callback is the
real ``partial(mark_archived, session)`` and the answer is read back out of the
table. Only Microsoft Graph is faked, and it is faked at the *wire* with
``httpx.MockTransport``: the assertions are on the ``httpx.Request`` the
production code actually constructed and on how it handles real status codes.
No internal function is ever stubbed -- ``_fetch_token``, ``_upload`` and
``archive_photos`` all run for real, which is what makes "the URL carries
``conflictBehavior=replace``" and "a 401 triggers exactly one refresh" provable
rather than asserted against a mock's memory of itself.

The secrets in this module are sentinels chosen to be unmistakable in a log
dump. Criterion 10 is asserted by searching captured log output for them: any
path that logs a credential, an access token or an ``Authorization`` header
fails, including the error paths, which are the ones that usually do it.
"""

from __future__ import annotations

import io
import logging
import secrets
import subprocess
import sys
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import ClientError
from conftest import BACKEND_DIR, SeededTrip
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.data import tables
from app.data.repositories.photos import list_pending_archive, mark_archived
from app.storage import onedrive_sync
from app.storage.onedrive_sync import archive_photos, main

# Sentinels. Distinctive enough that a substring search over a log dump cannot
# match them by accident, and obvious enough in a failure message.
CLIENT_ID = "client-id-SENTINEL-a1b2c3"
CLIENT_SECRET = "client-secret-SENTINEL-must-never-be-logged"
REFRESH_TOKEN = "refresh-token-SENTINEL-must-never-be-logged"
FOLDER = "/BikeTripTest"

# The access tokens the fake login endpoint hands out, in order. Distinct so
# "the refreshed token is reused for the photos after the retry" is observable.
ACCESS_TOKENS = [
    "access-token-SENTINEL-first-must-never-be-logged",
    "access-token-SENTINEL-second-must-never-be-logged",
]
# What Graph returns alongside a fresh access token. This job persists no
# config, so it must go nowhere -- and certainly not into a log.
ROTATED_REFRESH_TOKEN = "rotated-refresh-SENTINEL-must-never-be-logged"

SECRETS = [CLIENT_SECRET, REFRESH_TOKEN, ROTATED_REFRESH_TOKEN, *ACCESS_TOKENS]

# Wide enough that an absent photo is absent because it was archived, not
# because a cap cut it off.
WIDE_LIMIT = 200

# Real magic bytes, so the extension sniffer is fed what a camera produces.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"jpeg-payload" * 4
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"png-payload" * 4
HEIC_BYTES = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"heic-payload" * 4
UNKNOWN_BYTES = b"this is not an image at all, not even close"


# --------------------------------------------------------------------------
# The fake wire
# --------------------------------------------------------------------------


def uploaded_filename(request: httpx.Request) -> str:
    """The filename out of a Graph upload URL (``/root:/{folder}/{name}:/content``)."""
    path = urllib.parse.unquote(str(request.url).split("/root:/", 1)[1].split(":/content", 1)[0])
    return path.rsplit("/", 1)[-1]


@dataclass
class FakeGraph:
    """
    Microsoft Graph and the Microsoft login endpoint, at the transport layer.

    Handed to ``httpx.MockTransport``, so every request recorded here is one the
    production code built itself -- URL, headers and body included. ``upload``
    is the per-request policy under test: it receives the real ``httpx.Request``
    and this upload's 0-based index within the run, and either returns an
    ``httpx.Response`` or *raises*, which is how a transport failure is staged.
    """

    upload: Callable[[httpx.Request, int], httpx.Response] | None = None
    token: Callable[[httpx.Request, int], httpx.Response] | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    # Interleaved timeline of wire calls and `record` calls, in the order they
    # happened. This is how "recorded per photo, immediately" is asserted.
    events: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == onedrive_sync.TOKEN_URL:
            call = len(self.token_requests)
            self.events.append(f"token:{call}")
            if self.token is not None:
                return self.token(request, call)
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKENS[min(call - 1, len(ACCESS_TOKENS) - 1)],
                    "refresh_token": ROTATED_REFRESH_TOKEN,
                    "expires_in": 3600,
                },
            )

        count = len(self.uploads) - 1  # 0-based: this request is already recorded
        name = uploaded_filename(request)
        self.events.append(f"upload:{name}")
        if self.upload is not None:
            return self.upload(request, count)
        return httpx.Response(201, json={"id": f"graph-file-for-{name}"})

    @property
    def token_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == onedrive_sync.TOKEN_URL]

    @property
    def uploads(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PUT"]

    @property
    def uploaded_names(self) -> list[str]:
        return [uploaded_filename(r) for r in self.uploads]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class Recorder:
    """
    A ``record`` callback that only remembers, for the criteria that are not
    about the database. Where the criterion *is* about the database the tests
    pass the real ``partial(mark_archived, session)`` instead.
    """

    def __init__(self, events: list[str], rows: int = 1) -> None:
        self.calls: list[tuple[str, str]] = []
        self.events = events
        self.rows = rows

    async def __call__(self, photo_id: str, one_drive_file_id: str) -> int:
        self.calls.append((photo_id, one_drive_file_id))
        self.events.append(f"record:{photo_id}")
        return self.rows


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def graph_config(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """
    ``GRAPH_*`` set to the sentinels, for the duration of one test.

    ``get_settings`` is ``@lru_cache`` and both ``_fetch_token`` and ``_upload``
    call it at call time, so clearing the cache around the test is enough and
    nothing has to be injected. ``S3_*`` is deliberately left alone --
    ``app.storage.s3_client`` snapshots settings at import and would not see a
    change anyway.
    """
    monkeypatch.setenv("GRAPH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("GRAPH_REFRESH_TOKEN", REFRESH_TOKEN)
    monkeypatch.setenv("GRAPH_ONEDRIVE_FOLDER", FOLDER)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def captured_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Everything this module logs, at every level."""
    caplog.set_level(logging.DEBUG, logger="app.storage.onedrive_sync")
    return caplog


@pytest.fixture
async def session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """
    A session on the *test* engine.

    Never ``app.data.db.async_session``: that engine's asyncpg connections bind
    to whichever loop first checks one out, and pytest gives each test its own
    loop (``app/data/db.py``, the long comment above the engine).
    """
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with sessionmaker() as open_session:
        yield open_session


@dataclass(frozen=True, slots=True)
class SeededPhoto:
    """A photo row this fixture put in Postgres, with its bytes really in MinIO."""

    id: str
    stop_id: str
    object_key: str
    body: bytes
    extension: str


@dataclass(frozen=True, slots=True)
class ArchiveFixture:
    """Four pending photos, one per extension the sniffer knows."""

    stop_id: str
    jpeg: SeededPhoto
    png: SeededPhoto
    heic: SeededPhoto
    unknown: SeededPhoto

    @property
    def all(self) -> list[SeededPhoto]:
        return [self.jpeg, self.png, self.heic, self.unknown]

    @property
    def three(self) -> list[SeededPhoto]:
        """Three photos -- enough for "the middle one failed" to have two sides."""
        return [self.jpeg, self.png, self.heic]


@pytest.fixture
async def pending(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip], s3_bucket: None
) -> AsyncIterator[ArchiveFixture]:
    """
    Four unarchived photos with real bytes in MinIO, removed again on teardown.

    ``one_drive_file_id`` is NULL on all four, which is the only thing that
    makes a photo pending -- there is no queue and no stored flag, so a test can
    assert "still owed" simply by re-running the sweep.

    Bodies differ per photo, and differ in their leading magic bytes, so an
    upload that sent the wrong object or a filename with the wrong extension
    cannot produce a passing request.
    """
    from app.storage.s3_client import BUCKET_NAME, get_s3_client

    s3 = get_s3_client()

    prefix = secrets.token_urlsafe(8)
    stop_id = f"test-sync-stop-{prefix}"
    base = datetime(1990, 1, 1, tzinfo=UTC)

    def seed(suffix: str, body: bytes, extension: str) -> SeededPhoto:
        return SeededPhoto(
            id=f"test-sync-photo-{prefix}-{suffix}",
            stop_id=stop_id,
            object_key=f"photos/{prefix}/{suffix}",
            body=body,
            extension=extension,
        )

    fixture = ArchiveFixture(
        stop_id=stop_id,
        jpeg=seed("01", JPEG_BYTES, ".jpg"),
        png=seed("02", PNG_BYTES, ".png"),
        heic=seed("03", HEIC_BYTES, ".heic"),
        unknown=seed("04", UNKNOWN_BYTES, ".bin"),
    )

    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop_id,
                trip_id=seeded_trips[0].id,
                name="Archive sync stop",
                lat=-25.0,
                lng=133.0,
                location_source="gps",
                arrived_at=base,
                notes=None,
            )
        )
        for index, photo in enumerate(fixture.all):
            s3.put_object(Bucket=BUCKET_NAME, Key=photo.object_key, Body=photo.body)
            await conn.execute(
                tables.photos.insert().values(
                    id=photo.id,
                    stop_id=photo.stop_id,
                    object_key=photo.object_key,
                    one_drive_file_id=None,
                    uploaded_by="Zoe",
                    taken_at=base + timedelta(minutes=index),
                )
            )

    try:
        yield fixture
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.photos.delete().where(tables.photos.c.id.in_([p.id for p in fixture.all]))
            )
            await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop_id))
        for photo in fixture.all:
            s3.delete_object(Bucket=BUCKET_NAME, Key=photo.object_key)


async def stored_file_id(engine: AsyncEngine, photo_id: str) -> str | None:
    """One photo's ``one_drive_file_id``, read straight from the table."""
    async with engine.connect() as conn:
        return await conn.scalar(
            select(tables.photos.c.one_drive_file_id).where(tables.photos.c.id == photo_id)
        )


async def still_pending(session: AsyncSession, photo_id: str) -> bool:
    """True when the sweep still owes this photo."""
    pending_ids = {p.id for p in await list_pending_archive(session, WIDE_LIMIT)}
    return photo_id in pending_ids


# --------------------------------------------------------------------------
# Criterion 2 + 1: unconfigured is a clean no-op
# --------------------------------------------------------------------------


class TestUnconfigured:
    """
    Empty ``GRAPH_REFRESH_TOKEN``: log it, exit 0, touch nothing.

    This is the state of every dev machine and of CI, so it is also the only
    path most people will ever run. A version that tried anyway would fail the
    build on a laptop that has no Graph credentials and no business having any.
    """

    async def test_returns_zero_without_touching_network_or_database(
        self, monkeypatch: pytest.MonkeyPatch, captured_logs: pytest.LogCaptureFixture
    ) -> None:
        """
        Exit 0, and neither the wire nor ``app.data`` is reached.

        Both halves are sabotaged rather than counted: the real async transport
        is replaced with something that fails the test if any request escapes,
        and the sessionmaker ``main()`` imports is replaced with something that
        fails the test if it is called. S3 comes after the database in the only
        path that reaches it, so a run that never opened a session never read an
        object either.
        """
        monkeypatch.setenv("GRAPH_REFRESH_TOKEN", "")
        get_settings.cache_clear()

        async def no_network(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unconfigured run made a request to {request.url}")

        def no_database(*args, **kwargs):
            raise AssertionError("unconfigured run opened a database session")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
        monkeypatch.setattr("app.data.db.async_session", no_database)

        try:
            assert await main() == 0
        finally:
            get_settings.cache_clear()

        assert "not configured" in captured_logs.text.lower()

    def test_module_entry_point_runs_one_pass_and_exits_zero(self) -> None:
        """
        ``python -m app.storage.onedrive_sync`` really exits, and exits 0.

        A subprocess, because the thing under test is the ``__main__`` block:
        one ``asyncio.run``, one pass, and a process that terminates on its own
        rather than sitting in a scheduling loop. If this hangs, the job is not
        a cron tick and the test suite will say so by timing out.
        """
        env = {
            **_subprocess_env(),
            "GRAPH_REFRESH_TOKEN": "",
        }
        result = subprocess.run(
            [sys.executable, "-m", "app.storage.onedrive_sync"],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,  # the exit code is the assertion
        )

        assert result.returncode == 0, result.stderr
        assert "not configured" in result.stderr.lower()


def _subprocess_env() -> dict[str, str]:
    """The current environment, with ``backend/`` importable."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR) + (
        ";" + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


# --------------------------------------------------------------------------
# main(), configured: the wiring the entry point owns (t-onedrive-main-untested)
# --------------------------------------------------------------------------


@dataclass
class MainHarness:
    """What a configured ``main()`` run was wired to, for the assertions."""

    graph: FakeGraph
    limits: list[int] = field(default_factory=list)


@pytest.fixture
def main_harness(
    monkeypatch: pytest.MonkeyPatch, migrated_engine: AsyncEngine
) -> Callable[[set[str]], MainHarness]:
    """
    Point ``main()``'s three outside dependencies at test doubles, then hand back the record.

    ``main()`` builds its own session, repository calls and HTTP client, so each
    is replaced where ``main()`` looks it up, and nothing else is:

    - ``app.data.db.async_session`` becomes a sessionmaker on the *test* engine
      (the production one binds to whichever loop first used it —
      ``app/data/db.py``). The session, ``mark_archived`` and its commit are real.
    - ``list_pending_archive`` is wrapped, not replaced: the real query runs with
      the limit ``main()`` passes, and its result is narrowed to the given ids so
      a pending photo left by some other test cannot join the run (it would have
      no bytes in MinIO). The limit is recorded, so passing the wrong one shows.
    - The *real* ``httpx.AsyncClient`` ``main()`` constructs is kept; only its
      transport's send is routed into a ``FakeGraph``. No request reaches Graph.
    """
    import app.data.db
    import app.data.repositories.photos as photos_repository

    real_list_pending = photos_repository.list_pending_archive

    def install(photo_ids: set[str]) -> MainHarness:
        harness = MainHarness(graph=FakeGraph())

        async def list_pending_spy(session: AsyncSession, limit: int):
            harness.limits.append(limit)
            return [p for p in await real_list_pending(session, limit) if p.id in photo_ids]

        async def to_fake_graph(self, request: httpx.Request) -> httpx.Response:
            return harness.graph.handler(request)

        monkeypatch.setattr(
            app.data.db,
            "async_session",
            async_sessionmaker(migrated_engine, expire_on_commit=False),
        )
        monkeypatch.setattr(photos_repository, "list_pending_archive", list_pending_spy)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", to_fake_graph)
        return harness

    return install


class TestMainConfigured:
    """
    ``main()`` with a refresh token set: real session, real repositories, faked wire.

    The ``archive_photos`` tests above inject their own client and ``record``;
    this is the only place the entry point's own wiring runs — which sessionmaker
    it opens, which limit it selects with, that it records through
    ``mark_archived`` on the session it opened (and so commits), and that the
    exit code it returns is the one the run earned.
    """

    async def test_archives_every_pending_photo_and_exits_zero(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        main_harness: Callable[[set[str]], MainHarness],
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        Every pending photo is uploaded once and its Graph id is *committed* to its row.

        Read back on a separate connection, so an update left uncommitted in
        ``main()``'s session — lost when the process exits — reads as ``None``.
        """
        harness = main_harness({p.id for p in pending.all})

        assert await main() == 0

        assert harness.limits == [onedrive_sync.BATCH_LIMIT]
        assert len(harness.graph.token_requests) == 1
        assert sorted(harness.graph.uploaded_names) == sorted(
            f"{p.id}{p.extension}" for p in pending.all
        )
        for photo in pending.all:
            assert await stored_file_id(migrated_engine, photo.id) == (
                f"graph-file-for-{photo.id}{photo.extension}"
            ), f"{photo.id} was uploaded but its row does not say so"
        TestNoSecretIsLogged.assert_clean(captured_logs)

    async def test_a_failed_upload_exits_nonzero_and_leaves_that_photo_pending(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        main_harness: Callable[[set[str]], MainHarness],
        migrated_engine: AsyncEngine,
    ) -> None:
        """
        The exit code is the run's, and a failed photo stays owed while the rest are recorded.

        A shell or scheduler sees only the exit code, so ``main()`` returning 0
        after a failed upload would make a partial archive indistinguishable
        from a complete one.
        """
        harness = main_harness({p.id for p in pending.three})
        failed = f"{pending.png.id}{pending.png.extension}"

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            if uploaded_filename(request) == failed:
                return httpx.Response(500)
            return httpx.Response(201, json={"id": f"graph-file-for-{uploaded_filename(request)}"})

        harness.graph.upload = upload

        assert await main() == 1

        assert await stored_file_id(migrated_engine, pending.png.id) is None
        for photo in (pending.jpeg, pending.heic):
            assert await stored_file_id(migrated_engine, photo.id) == (
                f"graph-file-for-{photo.id}{photo.extension}"
            )

    async def test_nothing_pending_exits_zero_without_touching_graph(
        self,
        graph_config: None,
        main_harness: Callable[[set[str]], MainHarness],
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        An empty sweep is a clean 0 with no token request — not a failed login on every tick.

        Configured, so the query does run (the limit is recorded); nothing comes
        back, so the job has no reason to spend a refresh-token redemption.
        """
        harness = main_harness(set())

        assert await main() == 0

        assert harness.limits == [onedrive_sync.BATCH_LIMIT]
        assert harness.graph.requests == []
        assert "no photos pending" in captured_logs.text.lower()


# --------------------------------------------------------------------------
# Criterion 3: one access token per run
# --------------------------------------------------------------------------


class TestTokenRequest:
    """The refresh-token grant, fetched once and reused."""

    async def test_one_token_request_per_run_reused_for_every_photo(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """
        Three photos, one token request.

        A token per photo would work and would still be wrong: it is three times
        the login traffic, and a hundred-photo run would be a hundred redemptions
        where one does. (Redeeming does not revoke the configured refresh token;
        decision-log Entry 22.)
        """
        graph = FakeGraph()
        async with graph.client() as client:
            assert await archive_photos(client, pending.three, Recorder(graph.events)) == 0

        assert len(graph.token_requests) == 1
        assert len(graph.uploads) == 3
        assert graph.events[0] == "token:1", "the token comes before the first upload"

    async def test_token_request_is_the_refresh_grant_with_configured_credentials(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """
        The exact form post the criterion names, with values out of settings.

        Read off the request the production code built, not off a mock's
        expectation: the URL, the grant type, and all three credentials.
        """
        graph = FakeGraph()
        async with graph.client() as client:
            await archive_photos(client, [pending.jpeg], Recorder(graph.events))

        request = graph.token_requests[0]
        assert request.method == "POST"
        assert str(request.url) == "https://login.microsoftonline.com/common/oauth2/v2.0/token"

        form = urllib.parse.parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["client_id"] == [CLIENT_ID]
        assert form["client_secret"] == [CLIENT_SECRET]
        assert form["refresh_token"] == [REFRESH_TOKEN]

    async def test_rejected_token_aborts_the_run_and_archives_nothing(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        No token, no uploads, no rows touched, non-zero exit.

        The failure worth ruling out is an abort that has already half-run:
        uploading without a usable token, or marking photos archived that never
        left the building.
        """
        graph = FakeGraph(
            token=lambda request, call: httpx.Response(400, json={"error": "invalid_grant"})
        )
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code != 0
        assert graph.uploads == []
        for photo in pending.three:
            assert await stored_file_id(migrated_engine, photo.id) is None
        assert "400" in captured_logs.text


# --------------------------------------------------------------------------
# Criteria 4 + 5: the upload request
# --------------------------------------------------------------------------


class TestUploadRequest:
    """What actually goes on the wire, per photo."""

    async def test_filename_is_derived_from_the_photo_id_with_a_sniffed_extension(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """
        ``{photo id}{extension}``, with the extension sniffed from the bytes.

        The id is the deterministic half (criterion 4: same photo, same name,
        every run) and the extension follows from content, not from a filename
        nobody stored. All four cases in one pass: JPEG, PNG, HEIC, and
        something that is none of them.
        """
        graph = FakeGraph()
        async with graph.client() as client:
            assert await archive_photos(client, pending.all, Recorder(graph.events)) == 0

        assert graph.uploaded_names == [f"{p.id}{p.extension}" for p in pending.all]

    async def test_the_same_photo_gets_the_same_name_on_every_run(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """
        Two runs, one name.

        This is what makes a re-upload after a crash land on top of the first
        copy instead of beside it. A name with a timestamp or a random component
        in it would pass every other test in this file and quietly accumulate
        duplicates in the archive forever.
        """
        names = []
        for _ in range(2):
            graph = FakeGraph()
            async with graph.client() as client:
                await archive_photos(client, [pending.jpeg], Recorder(graph.events))
            names.append(graph.uploaded_names)

        assert names[0] == names[1] == [f"{pending.jpeg.id}.jpg"]

    async def test_upload_targets_the_configured_folder(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """The path under the drive root is ``settings.graph_onedrive_folder``."""
        graph = FakeGraph()
        async with graph.client() as client:
            await archive_photos(client, [pending.jpeg], Recorder(graph.events))

        url = urllib.parse.unquote(str(graph.uploads[0].url))
        assert "https://graph.microsoft.com/v1.0/me/drive/root:/" in url
        assert f"/root:/{FOLDER.strip('/')}/{pending.jpeg.id}.jpg:/content" in url

    async def test_upload_body_is_the_bytes_from_s3(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """
        Each photo's own object, read out of MinIO by ``object_key``.

        Distinct bodies per photo, so a loop that uploaded the first object
        three times, or held a stale buffer between iterations, cannot pass.
        """
        graph = FakeGraph()
        async with graph.client() as client:
            await archive_photos(client, pending.three, Recorder(graph.events))

        assert [r.content for r in graph.uploads] == [p.body for p in pending.three]

    async def test_upload_carries_a_bearer_authorization_header(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """
        Presence and scheme only -- the token value is never spelled into an
        assertion message here.
        """
        graph = FakeGraph()
        async with graph.client() as client:
            await archive_photos(client, pending.three, Recorder(graph.events))

        for request in graph.uploads:
            assert request.headers.get("Authorization", "").startswith("Bearer ")
            assert request.headers.get("Content-Type") == "application/octet-stream"

    async def test_conflict_behavior_replace_is_on_the_url(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """The directive itself, spelled as Graph documents it."""
        graph = FakeGraph()
        async with graph.client() as client:
            await archive_photos(client, [pending.jpeg], Recorder(graph.events))

        url = urllib.parse.unquote(str(graph.uploads[0].url))
        assert "@microsoft.graph.conflictBehavior=replace" in url

    async def test_a_graph_that_rejects_anything_but_replace_still_succeeds(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
    ) -> None:
        """
        Criterion 5, in the form that survives someone deleting the directive.

        Graph's default is ``fail``, so this fake answers 409
        ``nameAlreadyExists`` to any upload that does not carry ``replace`` --
        the same answer real Graph gives on the second attempt at a name that
        already exists. That is not hypothetical: the crash window between a
        successful upload and the ``UPDATE`` committing leaves exactly that
        state, and without ``replace`` the photo 409s forever and is stuck
        pending permanently.

        Asserting only that the string is in the URL would pass just as well if
        the value were ``rename``; this one does not.
        """

        def strict(request: httpx.Request, count: int) -> httpx.Response:
            url = urllib.parse.unquote(str(request.url))
            if "@microsoft.graph.conflictBehavior=replace" not in url:
                return httpx.Response(
                    409,
                    json={"error": {"code": "nameAlreadyExists", "message": "already exists"}},
                )
            return httpx.Response(200, json={"id": f"graph-{uploaded_filename(request)}"})

        graph = FakeGraph(upload=strict)
        async with graph.client() as client:
            code = await archive_photos(client, [pending.jpeg], partial(mark_archived, session))

        assert code == 0
        assert await stored_file_id(migrated_engine, pending.jpeg.id) is not None


# --------------------------------------------------------------------------
# Criterion 6: recorded per photo, immediately
# --------------------------------------------------------------------------


class TestRecordingArchivedPhotos:
    """Turning a 2xx into a row that no longer asks to be archived."""

    async def test_graph_file_id_from_the_response_body_lands_in_the_row(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
    ) -> None:
        """
        The stored value is Graph's ``id``, read back out of Postgres.

        Through the real ``mark_archived``, because the column is the whole
        point: it is what makes ``PhotoOut.archived`` true and what takes the
        photo out of the next sweep.
        """

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            return httpx.Response(201, json={"id": f"01GRAPH{count}ITEMID"})

        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code == 0
        stored = [await stored_file_id(migrated_engine, p.id) for p in pending.three]
        assert stored == ["01GRAPH0ITEMID", "01GRAPH1ITEMID", "01GRAPH2ITEMID"]
        for photo in pending.three:
            assert not await still_pending(session, photo.id)

    async def test_each_photo_is_recorded_before_the_next_one_uploads(
        self, graph_config: None, pending: ArchiveFixture
    ) -> None:
        """
        Interleaved, not batched at the end of the run.

        A run that collected results and wrote them all at the end loses every
        archive it already performed when the process dies mid-pass -- and
        because the bytes are in OneDrive but the row says pending, the next run
        re-uploads them all. The timeline is the assertion.
        """
        graph = FakeGraph()
        recorder = Recorder(graph.events)
        async with graph.client() as client:
            await archive_photos(client, pending.three, recorder)

        assert graph.events == [
            "token:1",
            f"upload:{pending.jpeg.id}.jpg",
            f"record:{pending.jpeg.id}",
            f"upload:{pending.png.id}.png",
            f"record:{pending.png.id}",
            f"upload:{pending.heic.id}.heic",
            f"record:{pending.heic.id}",
        ]

    async def test_a_2xx_that_is_not_200_still_counts_as_archived(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
    ) -> None:
        """
        201 and 202 are successes.

        Graph answers 201 for a newly created item and 200 when ``replace``
        overwrote one, so a check for ``== 200`` would treat every first upload
        as a failure and re-upload it on every run forever.
        """

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            return httpx.Response(
                (201, 202)[count % 2], json={"id": f"graph-{uploaded_filename(request)}"}
            )

        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            code = await archive_photos(
                client, [pending.jpeg, pending.png], partial(mark_archived, session)
            )

        assert code == 0
        assert await stored_file_id(migrated_engine, pending.jpeg.id) is not None
        assert await stored_file_id(migrated_engine, pending.png.id) is not None

    async def test_crash_window_replay_ends_archived(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
    ) -> None:
        """
        Uploaded, then the process died before the ``UPDATE`` -- replayed safely.

        Run one succeeds on the wire but its record never reaches the database
        (the callback here is the one that only remembers, which is exactly what
        a crash between the PUT and the commit leaves behind). The row is
        therefore still pending, the next sweep re-selects it, and run two
        re-uploads *the same name* and finishes the job.

        This is the scenario criterion 5 exists for, driven end to end: same
        filename both runs, 200 on the overwrite, and a non-null
        ``one_drive_file_id`` at the end.
        """
        first = FakeGraph()
        async with first.client() as client:
            await archive_photos(client, [pending.jpeg], Recorder(first.events))

        assert await stored_file_id(migrated_engine, pending.jpeg.id) is None
        assert await still_pending(session, pending.jpeg.id), "a crashed run leaves it owed"

        second = FakeGraph(
            upload=lambda request, count: httpx.Response(200, json={"id": "graph-replaced-item"})
        )
        async with second.client() as client:
            code = await archive_photos(client, [pending.jpeg], partial(mark_archived, session))

        assert code == 0
        assert first.uploaded_names == second.uploaded_names
        assert await stored_file_id(migrated_engine, pending.jpeg.id) == "graph-replaced-item"
        assert not await still_pending(session, pending.jpeg.id)


# --------------------------------------------------------------------------
# Criterion 7: per-photo failure
# --------------------------------------------------------------------------


class TestPerPhotoFailure:
    """One bad photo does not take the run, and does not lose the photo."""

    async def test_a_500_on_one_photo_leaves_it_pending_and_the_run_continues(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        Photo 2 fails; 1 and 3 archive; the run reports failure.

        The middle photo is the one that fails so both sides are observable: a
        run that stopped early would leave photo 3 unattempted, and a run that
        swallowed the failure would exit 0 and nobody would ever look again.
        The row is checked, not just the exit code -- untouched means the next
        run picks it up, which is the only reason a permanent failure is not a
        permanent loss.
        """

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            if count == 1:
                return httpx.Response(500, json={"error": {"code": "generalException"}})
            return httpx.Response(201, json={"id": f"graph-{uploaded_filename(request)}"})

        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code != 0, "a failed photo is a failed run"
        assert len(graph.uploads) == 3, "the run continued to photo 3"
        assert await stored_file_id(migrated_engine, pending.jpeg.id) is not None
        assert await stored_file_id(migrated_engine, pending.png.id) is None
        assert await stored_file_id(migrated_engine, pending.heic.id) is not None
        assert await still_pending(session, pending.png.id), "the failed photo is still owed"
        assert pending.png.id in captured_logs.text
        assert "500" in captured_logs.text

    async def test_a_transport_failure_on_one_photo_is_logged_and_skipped(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        A connection that never completes is the same class of event as a 500.

        Raised from the transport, so the exception really comes up through
        httpx the way a dropped connection does. An uncaught one would end the
        pass with a traceback and leave every later photo unattempted.
        """

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            if count == 1:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(201, json={"id": f"graph-{uploaded_filename(request)}"})

        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code != 0
        assert len(graph.uploads) == 3
        assert await stored_file_id(migrated_engine, pending.png.id) is None
        assert await stored_file_id(migrated_engine, pending.heic.id) is not None
        assert pending.png.id in captured_logs.text

    async def test_nothing_is_deleted_by_a_failed_run(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        migrated_engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """
        Every row and every object survives a run in which everything failed.

        The archive is a copy, never a move. A "cleanup" that removed the S3
        object after archiving -- or removed a row it could not archive -- would
        destroy the source of truth the app actually reads from
        (``docs/api-contract.md``, "Photo serving").
        """
        from app.storage.s3_client import BUCKET_NAME, get_s3_client

        graph = FakeGraph(upload=lambda request, count: httpx.Response(500))
        async with graph.client() as client:
            await archive_photos(client, pending.all, partial(mark_archived, session))

        s3 = get_s3_client()
        for photo in pending.all:
            assert await stored_file_id(migrated_engine, photo.id) is None
            body = s3.get_object(Bucket=BUCKET_NAME, Key=photo.object_key)["Body"].read()
            assert body == photo.body


# --------------------------------------------------------------------------
# Criterion 8: per-run failure
# --------------------------------------------------------------------------


class TestExpiredAccessToken:
    """A 401 mid-run: exactly one refresh, exactly one retry."""

    async def test_one_refresh_one_retry_and_the_new_token_is_reused(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
    ) -> None:
        """
        The access token expires on photo 2; the run finishes all three.

        Three things at once, because they are one behaviour: the refresh
        happens once (not once per photo), the retry is of *that* photo (not of
        the whole batch, which would re-upload photo 1), and photo 3 goes out
        under the refreshed token rather than triggering its own refresh.

        The ``Authorization`` values compared here are this test's own fake
        tokens, handed out by the fixture's login endpoint -- no real credential
        is named.
        """
        first_upload_of_photo_two = 1

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            if count == first_upload_of_photo_two:
                return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
            return httpx.Response(201, json={"id": f"graph-{uploaded_filename(request)}"})

        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code == 0
        assert len(graph.token_requests) == 2, "exactly one refresh"
        assert graph.uploaded_names == [
            f"{pending.jpeg.id}.jpg",
            f"{pending.png.id}.png",
            f"{pending.png.id}.png",
            f"{pending.heic.id}.heic",
        ], "the 401'd photo is retried once, and only that photo"

        authorizations = [r.headers["Authorization"] for r in graph.uploads]
        assert authorizations[0] == authorizations[1] == f"Bearer {ACCESS_TOKENS[0]}"
        assert authorizations[2] == authorizations[3] == f"Bearer {ACCESS_TOKENS[1]}", (
            "the refreshed token is reused for the rest of the pass"
        )

        for photo in pending.three:
            assert await stored_file_id(migrated_engine, photo.id) is not None

    async def test_a_second_401_aborts_the_run_with_zero_rows_updated(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        A freshly issued token that is also rejected means the refresh token has
        lapsed: stop, do not retry, do not touch a row.

        Retrying per photo against a dead credential is a login storm that ends
        in the account being throttled. The abort must also be *clean* -- every
        photo still pending, so a human re-consent and the next scheduled run
        pick up exactly where this one stopped.
        """
        graph = FakeGraph(
            upload=lambda request, count: httpx.Response(
                401, json={"error": {"code": "InvalidAuthenticationToken"}}
            )
        )
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code != 0
        assert len(graph.token_requests) == 2, "one initial token, one refresh, and no more"
        assert len(graph.uploads) == 2, "the first photo twice, and then nothing"
        for photo in pending.three:
            assert await stored_file_id(migrated_engine, photo.id) is None
            assert await still_pending(session, photo.id)
        assert "lapsed" in captured_logs.text.lower() or "401" in captured_logs.text


class TestThrottlingAndUnavailable:
    """429 and 503 end the pass. The backoff is the next scheduled run."""

    @pytest.mark.parametrize("status", [429, 503])
    async def test_run_aborts_leaving_the_remaining_photos_pending(
        self,
        status: int,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        Photo 1 archived and stays archived; photo 2 hits it; photo 3 untouched.

        Pushing on through a 429 is how a throttle becomes a block. The photo
        already archived before the throttle must keep its ``one_drive_file_id``
        -- an abort that rolled the pass back would re-upload it next run for no
        reason.
        """

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            if count == 1:
                return httpx.Response(status, headers={"Retry-After": "120"})
            return httpx.Response(201, json={"id": f"graph-{uploaded_filename(request)}"})

        graph = FakeGraph(upload=upload)
        started = time.monotonic()
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))
        elapsed = time.monotonic() - started

        assert code != 0
        assert len(graph.uploads) == 2, "the run stopped at the throttled photo"
        assert await stored_file_id(migrated_engine, pending.jpeg.id) is not None
        assert await stored_file_id(migrated_engine, pending.png.id) is None
        assert await still_pending(session, pending.heic.id), "never attempted, still owed"

        assert elapsed < 30, (
            f"the run took {elapsed:.1f}s -- Retry-After was 120, so anything that waited "
            "for it slept inside the process instead of leaving backoff to the next run"
        )
        assert str(status) in captured_logs.text
        assert "120" in captured_logs.text, "Retry-After is logged when it is sent"

    async def test_a_429_without_retry_after_still_aborts_cleanly(
        self, graph_config: None, pending: ArchiveFixture, session: AsyncSession
    ) -> None:
        """
        The header is optional. Logging it must not be what makes the abort work.
        """
        graph = FakeGraph(upload=lambda request, count: httpx.Response(429))
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code != 0
        assert len(graph.uploads) == 1
        assert await still_pending(session, pending.png.id)


# --------------------------------------------------------------------------
# Criterion 9: the batch limit
# --------------------------------------------------------------------------


class TestBatchLimit:
    """How much one pass takes is a module constant, not a knob."""

    def test_batch_limit_is_a_module_constant_and_not_configuration(self) -> None:
        """
        A positive int on the module, and nothing in ``Settings`` shadowing it.

        The only thing tuning it changes is how much a single cron tick chews
        through; the next tick takes the rest either way. A config key would be
        an operational lever that does nothing and an extra thing to get wrong
        in a deployment.
        """
        assert isinstance(onedrive_sync.BATCH_LIMIT, int)
        assert not isinstance(onedrive_sync.BATCH_LIMIT, bool)
        assert onedrive_sync.BATCH_LIMIT > 0

        configurable = [
            name for name in Settings.model_fields if "limit" in name or "batch" in name
        ]
        assert configurable == [], f"batch size leaked into configuration: {configurable}"


# --------------------------------------------------------------------------
# Criterion 10: no secret is ever logged
# --------------------------------------------------------------------------


class TestNoSecretIsLogged:
    """
    Every path this job can take, checked against every sentinel.

    Logs from a background job are the place credentials leak: nobody is
    watching them, they get shipped to a log service, and the paths that log the
    most are the error paths, which is exactly where an "include the request for
    debugging" line gets added. So the failing runs are checked as hard as the
    successful one.
    """

    @staticmethod
    def assert_clean(captured: pytest.LogCaptureFixture) -> None:
        text = captured.text
        for value in SECRETS:
            assert value not in text, "a credential or token reached the log"
        assert "Bearer " not in text, "an Authorization header reached the log"

    async def test_successful_run_logs_no_secret(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        graph = FakeGraph()
        async with graph.client() as client:
            await archive_photos(client, pending.three, Recorder(graph.events))

        self.assert_clean(captured_logs)

    @pytest.mark.parametrize(
        "upload",
        [
            pytest.param(lambda request, count: httpx.Response(500), id="server-error"),
            pytest.param(lambda request, count: httpx.Response(401), id="two-401s"),
            pytest.param(
                lambda request, count: httpx.Response(429, headers={"Retry-After": "5"}),
                id="throttled",
            ),
            pytest.param(lambda request, count: httpx.Response(403), id="forbidden"),
        ],
    )
    async def test_failing_upload_logs_no_secret(
        self,
        upload: Callable[[httpx.Request, int], httpx.Response],
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            await archive_photos(client, pending.three, Recorder(graph.events))

        assert captured_logs.text.strip() != "", "the failure was logged at all"
        self.assert_clean(captured_logs)

    async def test_transport_failure_logs_no_secret(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        The riskiest line in the module: ``httpx`` exceptions carry the request,
        and ``logger.error("...: %s", exc)`` on a ``RequestError`` is one
        ``repr`` away from printing the URL and, if anyone widens it, the
        headers.
        """

        def boom(request: httpx.Request, count: int) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        graph = FakeGraph(upload=boom)
        async with graph.client() as client:
            await archive_photos(client, pending.three, Recorder(graph.events))

        self.assert_clean(captured_logs)

    async def test_token_failure_logs_no_secret(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        The token request is the one that *carries* the client secret and the
        refresh token in its body, so a log line that echoed the failed request
        would dump both.
        """
        graph = FakeGraph(
            token=lambda request, call: httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "token expired"}
            )
        )
        async with graph.client() as client:
            await archive_photos(client, pending.three, Recorder(graph.events))

        self.assert_clean(captured_logs)

    async def test_token_transport_failure_logs_no_secret(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        def boom(request: httpx.Request, call: int) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        graph = FakeGraph(token=boom)
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, Recorder(graph.events))

        assert code != 0
        self.assert_clean(captured_logs)


# --------------------------------------------------------------------------
# Criterion 11 + the contract: write-only, never a read path
# --------------------------------------------------------------------------


class TestModuleIsWriteOnlyAndDocumented:
    """The property the contract states, asserted structurally."""

    def test_no_route_imports_the_sync(self) -> None:
        """
        Nothing under ``app/api/`` reaches this module.

        ``docs/api-contract.md`` ("Photo serving"): a photo's URL "is never a
        OneDrive URL. OneDrive is a write-only archive reached by a background
        sync job. It is never in a read path, and nothing user-facing waits on
        it." The moment a handler imports this module, a request can end up
        waiting on Graph -- and an unhealthy archive, which "degrades nothing a
        viewer can see", starts degrading the app.
        """
        api_dir = Path(onedrive_sync.__file__).resolve().parents[1] / "api"
        offenders = [
            str(path)
            for path in api_dir.rglob("*.py")
            if "onedrive" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"a route reached into the archive sync: {offenders}"

    def test_module_docstring_is_in_the_project_api_format(self) -> None:
        """
        Context -> How it works -> Related APIs, and it says write-only out loud.

        The format is not decoration: this docstring is what the next person
        reads before deciding whether a handler may call in here.
        """
        doc = onedrive_sync.__doc__ or ""
        for heading in ("Context", "How it works", "Related APIs"):
            assert heading in doc, f"missing the '{heading}' section"
        assert "write-only" in doc.lower()
        assert "read" in doc.lower()


# --------------------------------------------------------------------------
# Debt sweep: per-photo isolation, URL encoding, HEIF brands, token error
# codes, and the mark_archived == 0 warning
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoosePhoto:
    """A ``PendingPhoto`` with its bytes in MinIO and no row -- for ``Recorder`` runs."""

    id: str
    object_key: str


@pytest.fixture
def s3_photos(s3_bucket: None) -> AsyncIterator[Callable[[str, bytes], LoosePhoto]]:
    """Put an object in MinIO under a fresh key and hand back a photo pointing at it."""
    from app.storage.s3_client import BUCKET_NAME, get_s3_client

    s3 = get_s3_client()
    keys: list[str] = []

    def make(photo_id: str, body: bytes) -> LoosePhoto:
        key = f"photos/loose/{secrets.token_urlsafe(8)}"
        s3.put_object(Bucket=BUCKET_NAME, Key=key, Body=body)
        keys.append(key)
        return LoosePhoto(id=photo_id, object_key=key)

    yield make
    for key in keys:
        s3.delete_object(Bucket=BUCKET_NAME, Key=key)


class TestPerPhotoIsolation:
    """
    A photo that fails *before* or *after* the HTTP call is still one photo.

    t-onedrive-per-photo-isolation: the S3 read and the read of Graph's ``id``
    used to sit outside the per-photo ``try``, so either one failing ended the
    whole pass with a traceback and every later photo went unattempted.
    """

    async def test_a_missing_s3_object_mid_batch_does_not_block_later_photos(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """Photo 2's object is gone: 1 and 3 archive, 2 stays owed, and the run exits non-zero."""
        from app.storage.s3_client import BUCKET_NAME, get_s3_client

        get_s3_client().delete_object(Bucket=BUCKET_NAME, Key=pending.png.object_key)

        graph = FakeGraph()
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code != 0, "a photo that could not be read is a failed run"
        assert graph.uploaded_names == [f"{pending.jpeg.id}.jpg", f"{pending.heic.id}.heic"]
        assert await stored_file_id(migrated_engine, pending.jpeg.id) is not None
        assert await stored_file_id(migrated_engine, pending.png.id) is None
        assert await stored_file_id(migrated_engine, pending.heic.id) is not None
        assert await still_pending(session, pending.png.id), "the unreadable photo is still owed"
        assert pending.png.id in captured_logs.text
        TestNoSecretIsLogged.assert_clean(captured_logs)

    async def test_a_2xx_without_a_graph_id_does_not_block_later_photos(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """A 201 with no ``id`` in its body records nothing for that photo, and the run goes on."""

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            if count == 1:
                return httpx.Response(201, json={"name": "no id here"})
            return httpx.Response(201, json={"id": f"graph-{uploaded_filename(request)}"})

        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, partial(mark_archived, session))

        assert code != 0
        assert len(graph.uploads) == 3
        assert await stored_file_id(migrated_engine, pending.png.id) is None
        assert await stored_file_id(migrated_engine, pending.heic.id) is not None
        assert await still_pending(session, pending.png.id)
        assert pending.png.id in captured_logs.text

    @pytest.mark.parametrize(
        "graph_id",
        [
            pytest.param(None, id="null"),
            pytest.param("", id="empty"),
            pytest.param(123, id="int"),
        ],
    )
    async def test_a_2xx_with_an_unusable_graph_id_leaves_the_photo_pending(
        self,
        graph_id: object,
        graph_config: None,
        pending: ArchiveFixture,
        session: AsyncSession,
        migrated_engine: AsyncEngine,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """
        ``id`` present but null, empty or not a string: same as missing.

        Recording it would stamp the row with a value that points at nothing in
        OneDrive -- or, for null, silently re-record NULL and exit 0, so nobody
        ever hears that the archive did not happen. (Missing key: the test above.)
        """

        def upload(request: httpx.Request, count: int) -> httpx.Response:
            if count == 1:
                return httpx.Response(201, json={"id": graph_id})
            return httpx.Response(201, json={"id": f"graph-{uploaded_filename(request)}"})

        recorded: list[str] = []

        async def record(photo_id: str, one_drive_file_id: str) -> int:
            recorded.append(photo_id)
            return await mark_archived(session, photo_id, one_drive_file_id)

        graph = FakeGraph(upload=upload)
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, record)

        assert code != 0, "an unusable id is a failed photo"
        assert pending.png.id not in recorded, "record was called with an unusable id"
        assert recorded == [pending.jpeg.id, pending.heic.id], "the run went on to photo 3"
        assert await stored_file_id(migrated_engine, pending.png.id) is None
        assert await still_pending(session, pending.png.id)
        failures = [r.getMessage() for r in captured_logs.records if r.levelno == logging.ERROR]
        assert any(pending.png.id in m and "201" in m for m in failures), failures


class TestFailureLogsCarryNoPayload:
    """
    The S3-read and missing-id failure logs name the photo and the class/status only.

    Not the exception text (a botocore message can carry the bucket, key and
    request ids), not the object key, not the response body. A fake S3 client is
    injected where the module looks it up, so this needs no MinIO and the
    ``ClientError`` it raises carries a sentinel in its message.
    """

    OBJECT_KEY = "photos/key-SENTINEL-must-never-be-logged"
    S3_MESSAGE = "s3-message-SENTINEL-must-never-be-logged"
    BODY_TEXT = "graph-body-SENTINEL-must-never-be-logged"

    class FakeS3:
        def __init__(self, error: Exception | None) -> None:
            self.error = error

        def get_object(self, Bucket: str, Key: str) -> dict:
            if self.error is not None:
                raise self.error
            return {"Body": io.BytesIO(JPEG_BYTES)}

    def assert_no_payload(self, captured: pytest.LogCaptureFixture, photo_id: str) -> str:
        text = captured.text
        assert photo_id in text, "the failure was logged against its photo"
        for sentinel in (self.OBJECT_KEY, self.S3_MESSAGE, self.BODY_TEXT):
            assert sentinel not in text, f"{sentinel!r} reached the log"
        TestNoSecretIsLogged.assert_clean(captured)
        return text

    async def test_s3_read_failure_logs_the_class_not_the_message_or_key(
        self,
        graph_config: None,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        error = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": f"{self.S3_MESSAGE} {self.OBJECT_KEY}"}},
            "GetObject",
        )
        assert self.S3_MESSAGE in str(error), "the sentinel really is in the exception text"
        monkeypatch.setattr(onedrive_sync, "get_s3_client", lambda: self.FakeS3(error))
        photo = LoosePhoto(id="log-hygiene-s3", object_key=self.OBJECT_KEY)

        graph = FakeGraph()
        async with graph.client() as client:
            code = await archive_photos(client, [photo], Recorder(graph.events))

        assert code != 0
        assert graph.uploads == []
        text = self.assert_no_payload(captured_logs, photo.id)
        assert "ClientError" in text

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"name": BODY_TEXT}, id="missing"),
            pytest.param({"id": None, "name": BODY_TEXT}, id="null"),
        ],
    )
    async def test_missing_id_failure_logs_the_status_not_the_body_or_key(
        self,
        body: dict,
        graph_config: None,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(onedrive_sync, "get_s3_client", lambda: self.FakeS3(None))
        photo = LoosePhoto(id="log-hygiene-id", object_key=self.OBJECT_KEY)

        graph = FakeGraph(upload=lambda request, count: httpx.Response(201, json=body))
        recorder = Recorder(graph.events)
        async with graph.client() as client:
            code = await archive_photos(client, [photo], recorder)

        assert code != 0
        assert recorder.calls == []
        text = self.assert_no_payload(captured_logs, photo.id)
        assert "201" in text


class TestFilenameEncoding:
    """t-onedrive-filename-url-encoding: the id is data, not URL syntax."""

    @pytest.mark.parametrize("photo_id", ["has#hash", "has?query", "both#and?x"])
    async def test_a_reserved_character_in_the_id_keeps_content_and_replace(
        self,
        photo_id: str,
        graph_config: None,
        s3_photos: Callable[[str, bytes], LoosePhoto],
    ) -> None:
        """
        The request still targets ``:/content`` with ``conflictBehavior=replace``.

        Unencoded, a ``#`` turns everything after it into a fragment (never
        sent) and a ``?`` starts the query early -- either way Graph's default
        ``fail`` applies and a crash-window replay strands the photo.
        """
        photo = s3_photos(photo_id, JPEG_BYTES)

        graph = FakeGraph()
        async with graph.client() as client:
            assert await archive_photos(client, [photo], Recorder(graph.events)) == 0

        request = graph.uploads[0]
        assert request.url.fragment == ""
        assert (
            request.url.path == f"/v1.0/me/drive/root:/{FOLDER.strip('/')}/{photo_id}.jpg:/content"
        )
        assert urllib.parse.unquote(request.url.query.decode()) == (
            "@microsoft.graph.conflictBehavior=replace"
        )


class TestHeifBrands:
    """t-onedrive-heic-brand-list: iPhone HEIF brands archive as ``.heic``, not ``.bin``."""

    @pytest.mark.parametrize("brand", [b"heic", b"heif", b"heix", b"hevc", b"mif1"])
    async def test_heif_brand_archives_as_heic(
        self,
        brand: bytes,
        graph_config: None,
        s3_photos: Callable[[str, bytes], LoosePhoto],
    ) -> None:
        photo = s3_photos(
            f"heif-{brand.decode()}", b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 4 + b"x" * 32
        )

        graph = FakeGraph()
        async with graph.client() as client:
            await archive_photos(client, [photo], Recorder(graph.events))

        assert graph.uploaded_names == [f"{photo.id}.heic"]


class TestTokenErrorCodeLogging:
    """
    t-graph-token-error-code-logging: a rejected token says *why*, safely.

    The owner has to tell an expired or revoked refresh token (re-mint) from a
    Microsoft outage. The OAuth ``error`` code and the AADSTS numbers do that;
    ``error_description`` is free text that can echo request data, so it never
    reaches the log.
    """

    DESCRIPTION = "AADSTS700082: expired -- description-SENTINEL-must-never-be-logged"

    async def test_error_and_aadsts_codes_are_logged_but_nothing_sensitive(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        graph = FakeGraph(
            token=lambda request, call: httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": self.DESCRIPTION,
                    "error_codes": [700082],
                },
            )
        )
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, Recorder(graph.events))

        assert code != 0
        text = captured_logs.text
        assert "400" in text
        assert "invalid_grant" in text
        assert "AADSTS700082" in text
        assert "description-SENTINEL" not in text, "error_description reached the log"
        TestNoSecretIsLogged.assert_clean(captured_logs)

    async def test_a_non_json_rejection_is_logged_without_its_body(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        """An HTML error page (a proxy, an outage) aborts cleanly and says no code was given."""
        graph = FakeGraph(
            token=lambda request, call: httpx.Response(
                502, text="<html>Bad Gateway body-SENTINEL-must-never-be-logged</html>"
            )
        )
        async with graph.client() as client:
            code = await archive_photos(client, pending.three, Recorder(graph.events))

        assert code != 0
        assert graph.uploads == []
        text = captured_logs.text
        assert "502" in text
        assert "error: none given" in text
        assert "body-SENTINEL" not in text
        TestNoSecretIsLogged.assert_clean(captured_logs)


class TestRecordReturnedZero:
    """
    t-onedrive-archived-log-wording: ``mark_archived`` returning 0 has two causes.

    Its ``UPDATE`` carries ``AND one_drive_file_id IS NULL``, so 0 means either
    the row was deleted (orphaned copy) or an overlapping sweep already archived
    it (same path, overwritten -- nothing orphaned). The warning must not claim
    only the first.
    """

    async def test_warning_names_both_causes_and_the_exit_code_is_unchanged(
        self,
        graph_config: None,
        pending: ArchiveFixture,
        captured_logs: pytest.LogCaptureFixture,
    ) -> None:
        graph = FakeGraph()
        async with graph.client() as client:
            code = await archive_photos(client, [pending.jpeg], Recorder(graph.events, rows=0))

        assert code == 0, "a 0-row record is a warning, not a failure"
        warnings = [r for r in captured_logs.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert pending.jpeg.id in message
        assert "deleted" in message
        assert "already archived" in message
