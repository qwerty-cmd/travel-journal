"""
``photos`` repository -- ``list_pending_archive`` and ``mark_archived``.

**Written from the contract and the acceptance criteria, not from the
implementation.** These are the two database calls the OneDrive archive sync
is built on, which puts them in spec Section 12's data-integrity tier: a photo
that falls out of the pending sweep without ever reaching the archive is
silently lost, and nothing user-facing ever notices, because OneDrive is never
in a read path.

What is under assertion:

1. **Pending means ``one_drive_file_id IS NULL``.** A row that has been
   archived is excluded from the sweep; one that has not is included. There is
   no stored ``archived`` column (``app/data/tables.py``) -- "still needs
   archiving" is a pure query over current state, which is what makes a
   crashed run safe to repeat: the row was never mutated, so the next sweep
   re-selects it.
2. **``limit`` is respected**, so one sweep cannot pull the whole table.
3. **``mark_archived`` touches one row.** A sibling photo on the *same stop* is
   left exactly as it was.
4. **``mark_archived`` on an unknown id returns 0 and writes nothing** -- no
   row is created for an id that was never uploaded.
5. **``PhotoOut.archived`` is derived, not stored.** After ``mark_archived``,
   the published contract field flips to ``true`` for that photo and stays
   ``false`` for its sibling -- read back through ``list_by_stop``, the same
   path ``GET /trips/{slug}/stops/{id}/photos`` serves.
6. **The sweep is not trip- or stop-scoped.** It returns photos from more than
   one trip in a single call. This is a maintenance sweep over the whole
   table, and the missing ``WHERE trip_id`` is deliberate -- a single-trip
   fixture would make an added filter look correct.

Timestamps here are in 1990 on purpose. The sweep is global, so any other
photo row in the test database competes for the same ``limit``; ordering by
``(taken_at, id)`` ascending puts these rows first whatever else is present,
which keeps the ``limit`` assertion deterministic without the test having to
own the table.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
from conftest import SeededTrip
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.repositories import photos as photos_repo

# A generous cap for the assertions that are not about `limit` itself: large
# enough that an excluded row's absence means the filter excluded it, not that
# the cap cut it off.
WIDE_LIMIT = 50


@dataclass(frozen=True, slots=True)
class SeededPhoto:
    """
    A photos row this fixture put in the database, in the table's own spelling.

    Independent of the repository's own types on purpose: comparing the code
    under test against a value the code under test produced asserts nothing.
    """

    id: str
    stop_id: str
    object_key: str
    one_drive_file_id: str | None
    uploaded_by: str
    taken_at: datetime


@dataclass(frozen=True, slots=True)
class ArchiveFixture:
    """The two stops and four photos the tests in this module work against."""

    stop_trip_one: str
    stop_trip_two: str
    pending_first: SeededPhoto
    pending_sibling: SeededPhoto
    already_archived: SeededPhoto
    pending_other_trip: SeededPhoto

    @property
    def pending_in_order(self) -> list[SeededPhoto]:
        """The three unarchived photos, in the order the sweep promises."""
        return [self.pending_first, self.pending_sibling, self.pending_other_trip]


@pytest.fixture
async def session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the test database, for calling repository functions directly."""
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with sessionmaker() as open_session:
        yield open_session


@pytest.fixture
async def archive_fixture(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[ArchiveFixture]:
    """
    Photos spread over two trips, one of them already archived.

    Two trips because the sweep is deliberately not trip-scoped, and two photos
    on one shared stop because "``mark_archived`` updated exactly one row" is
    only observable when there is a neighbour close enough for a missing
    ``WHERE`` to hit -- a sibling on the same stop, not a photo somewhere else.

    Ids are assigned to **contradict** ``taken_at`` -- the oldest photo carries
    the highest suffix. Both halves of the promised ``(taken_at, id)`` ordering
    then have to be right for the order assertions to hold: sorting by id alone
    reverses the answer, so an order-by regression cannot hide behind a fixture
    whose two orderings happen to agree. Two pending photos deliberately share
    a ``taken_at``, so the ``id`` tiebreak is load-bearing rather than
    decorative, and the resulting order is total.

    Suffixes are fixed-width with a shared random prefix, so their relative
    order is the same under any Postgres collation.
    """
    first_trip, second_trip = seeded_trips
    prefix = secrets.token_urlsafe(8)
    stop_one = f"test-archive-stop-{prefix}-1"
    stop_two = f"test-archive-stop-{prefix}-2"
    base = datetime(1990, 1, 1, 0, 0, tzinfo=UTC)

    # Oldest photo, highest id suffix: id-only ordering puts it last.
    pending_first = SeededPhoto(
        id=f"test-archive-photo-{prefix}-40",
        stop_id=stop_one,
        object_key=f"photos/{prefix}/40.jpg",
        one_drive_file_id=None,
        uploaded_by="Zoe",
        taken_at=base,
    )
    # Shares its taken_at with pending_other_trip, and wins the tie on id.
    pending_sibling = SeededPhoto(
        id=f"test-archive-photo-{prefix}-20",
        stop_id=stop_one,
        object_key=f"photos/{prefix}/20.jpg",
        one_drive_file_id=None,
        uploaded_by="Alex",
        taken_at=base + timedelta(minutes=1),
    )
    pending_other_trip = SeededPhoto(
        id=f"test-archive-photo-{prefix}-30",
        stop_id=stop_two,
        object_key=f"photos/{prefix}/30.jpg",
        one_drive_file_id=None,
        uploaded_by="Sam",
        taken_at=base + timedelta(minutes=1),
    )
    already_archived = SeededPhoto(
        id=f"test-archive-photo-{prefix}-10",
        stop_id=stop_one,
        object_key=f"photos/{prefix}/10.jpg",
        # Non-null: this photo is done, and the sweep must not hand it back.
        one_drive_file_id=f"onedrive-{prefix}-10",
        uploaded_by="Alex",
        taken_at=base + timedelta(minutes=2),
    )

    # Inserted in an order that is neither the promised ordering nor its
    # reverse, and specifically with the *loser* of the taken_at tie inserted
    # first: a query that dropped the `id` tiebreak would fall back to whatever
    # order Postgres scans rows in, which is this one.
    seeded_photos = [pending_other_trip, already_archived, pending_first, pending_sibling]

    async with migrated_engine.begin() as conn:
        for stop_id, trip_id, name in (
            (stop_one, first_trip.id, "Archive stop one"),
            (stop_two, second_trip.id, "Archive stop two"),
        ):
            await conn.execute(
                tables.stops.insert().values(
                    id=stop_id,
                    trip_id=trip_id,
                    name=name,
                    lat=-25.0,
                    lng=133.0,
                    location_source="gps",
                    arrived_at=base,
                    notes=None,
                )
            )
        for photo in seeded_photos:
            await conn.execute(
                tables.photos.insert().values(
                    id=photo.id,
                    stop_id=photo.stop_id,
                    object_key=photo.object_key,
                    one_drive_file_id=photo.one_drive_file_id,
                    uploaded_by=photo.uploaded_by,
                    taken_at=photo.taken_at,
                )
            )

    try:
        yield ArchiveFixture(
            stop_trip_one=stop_one,
            stop_trip_two=stop_two,
            pending_first=pending_first,
            pending_sibling=pending_sibling,
            already_archived=already_archived,
            pending_other_trip=pending_other_trip,
        )
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.photos.delete().where(tables.photos.c.id.in_([p.id for p in seeded_photos]))
            )
            await conn.execute(
                tables.stops.delete().where(tables.stops.c.id.in_([stop_one, stop_two]))
            )


async def stored_one_drive_file_id(engine: AsyncEngine, photo_id: str) -> str | None:
    """The ``one_drive_file_id`` column for one photo, read straight from the table."""
    async with engine.connect() as conn:
        return await conn.scalar(
            select(tables.photos.c.one_drive_file_id).where(tables.photos.c.id == photo_id)
        )


async def stored_photo_row(engine: AsyncEngine, photo_id: str) -> dict:
    """Every column of one photo, read straight from the table."""
    async with engine.connect() as conn:
        result = await conn.execute(tables.photos.select().where(tables.photos.c.id == photo_id))
        return dict(result.mappings().one())


async def photo_row_count(engine: AsyncEngine) -> int:
    """How many rows the photos table holds right now."""
    async with engine.connect() as conn:
        count = await conn.scalar(select(func.count()).select_from(tables.photos))
        return int(count or 0)


class TestListPendingArchive:
    """The sweep that decides which photos the archive job still owes."""

    async def test_archived_excluded_and_unarchived_included(
        self, session: AsyncSession, archive_fixture: ArchiveFixture
    ) -> None:
        """
        A non-null ``one_drive_file_id`` is done; a NULL one is still pending.

        Both rows sort into the same window, so the archived one's absence is
        the filter doing its job rather than the limit cutting it off.
        """
        pending = await photos_repo.list_pending_archive(session, WIDE_LIMIT)
        returned_ids = [photo.id for photo in pending]

        assert archive_fixture.pending_first.id in returned_ids
        assert archive_fixture.pending_sibling.id in returned_ids
        assert archive_fixture.already_archived.id not in returned_ids

    async def test_object_key_is_carried_through(
        self, session: AsyncSession, archive_fixture: ArchiveFixture
    ) -> None:
        """
        The sweep hands back the object key, which is what the sync reads bytes with.

        A pending photo whose key did not survive the query is a photo the sync
        cannot fetch and therefore cannot archive.
        """
        pending = await photos_repo.list_pending_archive(session, WIDE_LIMIT)
        by_id = {photo.id: photo for photo in pending}

        assert (
            by_id[archive_fixture.pending_first.id].object_key
            == archive_fixture.pending_first.object_key
        )

    async def test_limit_is_respected(
        self, session: AsyncSession, archive_fixture: ArchiveFixture
    ) -> None:
        """
        ``limit`` caps the sweep, and caps it at the *oldest* pending photos.

        The fixture's rows are the oldest in the table, so with ``limit=2`` the
        answer is exactly the first two of them in ``(taken_at, id)`` order --
        a cap that returned an arbitrary two, or ignored the cap entirely, both
        fail here. So does an ordering that dropped either sort key: the
        fixture's ids run opposite to its timestamps, and two of its photos
        share a ``taken_at`` and can only be separated by id.

        Which photos a capped sweep returns is not cosmetic. When more photos
        are pending than one batch can take, this ordering is what decides
        whether the backlog drains oldest-first or leaves a tail that is never
        reached.
        """
        pending = await photos_repo.list_pending_archive(session, 2)

        assert len(pending) == 2
        assert [photo.id for photo in pending] == [
            photo.id for photo in archive_fixture.pending_in_order[:2]
        ]

    async def test_sweep_crosses_trips(
        self, session: AsyncSession, archive_fixture: ArchiveFixture
    ) -> None:
        """
        One sweep returns photos from more than one trip.

        Deliberate, and the property most likely to be mistaken for a missing
        ``WHERE trip_id`` and 'fixed': the archive job runs for the whole
        database, not per trip, so a trip- or stop-scoped query would quietly
        leave every other trip's photos unarchived forever.
        """
        pending = await photos_repo.list_pending_archive(session, WIDE_LIMIT)
        returned_ids = {photo.id for photo in pending}

        assert archive_fixture.pending_first.id in returned_ids, "photo on the first trip"
        assert archive_fixture.pending_other_trip.id in returned_ids, "photo on the second trip"


class TestMarkArchived:
    """Recording that one photo reached the archive."""

    async def test_marks_one_photo_and_leaves_its_sibling_alone(
        self, session: AsyncSession, archive_fixture: ArchiveFixture, migrated_engine: AsyncEngine
    ) -> None:
        """
        One row, one column.

        An update that lost its ``WHERE`` would archive the sibling too -- and
        because ``archived`` is derived from this column, that photo would drop
        out of every future sweep while its bytes were never sent anywhere. So
        the sibling on the same stop is checked as well as the target.

        The target's *other* columns are checked too: ``one_drive_file_id`` is
        the only one this call is allowed to write, and a stray value in the
        same ``.values(...)`` would be invisible to an assertion that read back
        only the column it expected to change.

        The return is asserted to be an ``int`` and not a ``bool``. ``True == 1``
        in Python, so ``rows == 1`` alone cannot tell a row count from a
        success flag, and the count is the whole point -- it is how a caller
        learns the photo went away mid-run without a second query.
        """
        target = archive_fixture.pending_first
        sibling = archive_fixture.pending_sibling
        assert target.stop_id == sibling.stop_id, "fixture must put both on one stop"

        rows = await photos_repo.mark_archived(session, target.id, "onedrive-file-abc")

        assert rows == 1
        assert type(rows) is int, f"a row count, not a success flag: got {rows!r}"

        stored = await stored_photo_row(migrated_engine, target.id)
        assert stored["one_drive_file_id"] == "onedrive-file-abc"
        assert stored["stop_id"] == target.stop_id
        assert stored["object_key"] == target.object_key
        assert stored["uploaded_by"] == target.uploaded_by
        assert stored["taken_at"] == target.taken_at

        assert await stored_one_drive_file_id(migrated_engine, sibling.id) is None

    async def test_marked_photo_drops_out_of_the_next_sweep(
        self, session: AsyncSession, archive_fixture: ArchiveFixture
    ) -> None:
        """
        A photo the job finished is not offered again; its sibling still is.

        This is what stops the sweep re-uploading the same bytes forever, and
        it has to hold without any stored queue state -- the next sweep is just
        the same query over the new row state.
        """
        await photos_repo.mark_archived(session, archive_fixture.pending_first.id, "onedrive-xyz")

        pending = await photos_repo.list_pending_archive(session, WIDE_LIMIT)
        returned_ids = {photo.id for photo in pending}

        assert archive_fixture.pending_first.id not in returned_ids
        assert archive_fixture.pending_sibling.id in returned_ids

    async def test_unknown_id_returns_zero_and_writes_nothing(
        self, session: AsyncSession, archive_fixture: ArchiveFixture, migrated_engine: AsyncEngine
    ) -> None:
        """
        An id no photo has updates nothing -- and creates nothing.

        The 0 is how the caller learns the photo went away mid-run (its stop
        was cascade-deleted) without a second query. Asserting only on the
        return value would not catch an upsert quietly inventing a row for an
        id that was never uploaded, so the table is counted as well.
        """
        unknown_id = f"no-such-photo-{secrets.token_urlsafe(8)}"
        count_before = await photo_row_count(migrated_engine)

        rows = await photos_repo.mark_archived(session, unknown_id, "onedrive-nobody")

        assert rows == 0
        assert await stored_one_drive_file_id(migrated_engine, unknown_id) is None
        assert await photo_row_count(migrated_engine) == count_before


class TestArchivedIsDerivedNotStored:
    """The published ``PhotoOut.archived`` field, across both new functions."""

    async def test_list_by_stop_reports_archived_after_mark(
        self, session: AsyncSession, archive_fixture: ArchiveFixture
    ) -> None:
        """
        ``mark_archived`` flips ``PhotoOut.archived`` for that photo only.

        Read back through ``list_by_stop`` -- the same path
        ``GET /trips/{slug}/stops/{id}/photos`` serves -- because ``archived``
        is not a column: it is derived from ``one_drive_file_id IS NOT NULL``
        at read time (``docs/api-contract.md``, "Photo serving"). Writing one
        and reading the other is the assertion; a stored ``archived`` flag
        could disagree with the column the sweep filters on, and then a photo
        could report itself archived while the sweep still owed it, or the
        reverse.
        """
        target = archive_fixture.pending_first
        sibling = archive_fixture.pending_sibling

        before = {
            photo.id: photo.archived
            for photo in await photos_repo.list_by_stop(session, target.stop_id)
        }
        assert before[target.id] is False
        assert before[sibling.id] is False

        await photos_repo.mark_archived(session, target.id, "onedrive-file-derived")

        after = {
            photo.id: photo.archived
            for photo in await photos_repo.list_by_stop(session, target.stop_id)
        }
        assert after[target.id] is True, "the photo that was archived"
        assert after[sibling.id] is False, "the photo beside it, which was not"


class TestMarkArchivedNeverOverwrites:
    """``t-mark-archived-overwrite-guard``: a recorded file id is never replaced."""

    async def test_already_archived_row_is_not_overwritten(
        self, session: AsyncSession, archive_fixture: ArchiveFixture, migrated_engine: AsyncEngine
    ) -> None:
        """
        A second write against an archived row updates nothing and returns 0.

        Replacing the id would leave the first OneDrive copy unreferenced with no
        error -- the race two overlapping sweeps would produce.
        """
        archived = archive_fixture.already_archived

        rows = await photos_repo.mark_archived(session, archived.id, "onedrive-second-copy")

        assert rows == 0
        assert await stored_one_drive_file_id(migrated_engine, archived.id) == (
            archived.one_drive_file_id
        )

    async def test_second_sweep_writing_the_same_row_loses(
        self, session: AsyncSession, archive_fixture: ArchiveFixture, migrated_engine: AsyncEngine
    ) -> None:
        """Two sweeps that both selected one NULL row: the first write stands."""
        target = archive_fixture.pending_first

        first = await photos_repo.mark_archived(session, target.id, "onedrive-first")
        second = await photos_repo.mark_archived(session, target.id, "onedrive-second")

        assert (first, second) == (1, 0)
        assert await stored_one_drive_file_id(migrated_engine, target.id) == "onedrive-first"


class TestListPendingArchiveLimitValidation:
    """``t-pending-archive-limit-validation``: a non-positive limit is refused up front."""

    @pytest.mark.parametrize("limit", [0, -1, -50])
    async def test_non_positive_limit_raises_value_error(
        self, session: AsyncSession, limit: int
    ) -> None:
        """A ``ValueError`` from the repository, not a raw driver error from asyncpg."""
        with pytest.raises(ValueError, match="limit"):
            await photos_repo.list_pending_archive(session, limit)

    async def test_limit_of_one_is_accepted(
        self, session: AsyncSession, archive_fixture: ArchiveFixture
    ) -> None:
        """The boundary: 1 is the smallest sweep that makes progress."""
        pending = await photos_repo.list_pending_archive(session, 1)

        assert [photo.id for photo in pending] == [archive_fixture.pending_first.id]


class TestInsertReturnsTheStoredRow:
    """``t-photo-insert-echoes-argument``: ``insert`` reads the row back."""

    async def test_insert_spells_taken_at_like_its_own_replay(
        self, session: AsyncSession, archive_fixture: ArchiveFixture, migrated_engine: AsyncEngine
    ) -> None:
        """
        A ``+09:30`` instant goes in; the ``PhotoOut`` that comes back is the
        stored UTC spelling, identical to what ``find_existing`` (the replay path)
        returns for the same id -- field for field, bar the presigned URL.
        """
        stop_id = archive_fixture.stop_trip_one
        photo_id = f"test-insert-readback-{secrets.token_urlsafe(8)}"
        submitted = datetime(2026, 6, 14, 10, 0, tzinfo=timezone(timedelta(hours=9, minutes=30)))

        try:
            created = await photos_repo.insert(
                session, stop_id, photo_id, "Zoe", submitted, f"photos/{photo_id}.jpg"
            )
            replayed = await photos_repo.find_existing(session, stop_id, photo_id)

            assert replayed is not None
            assert created.takenAt == submitted
            assert created.takenAt.utcoffset() == timedelta(0)
            assert created.model_dump(mode="json", exclude={"url"}) == replayed.model_dump(
                mode="json", exclude={"url"}
            )
            assert created.archived is False
        finally:
            async with migrated_engine.begin() as conn:
                await conn.execute(tables.photos.delete().where(tables.photos.c.id == photo_id))
