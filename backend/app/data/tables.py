"""
SQLAlchemy Core table metadata for the Postgres schema.

This is the Python-side mirror of ``migrations/0001_initial_schema.sql`` — the
SQL file is what actually creates the schema, this module is what the
repositories in ``data/repositories/`` build queries against. The two must
agree; ``tests/test_schema.py`` reflects the live database and asserts that
they do, so a column added to one and forgotten in the other fails a test
rather than an endpoint.

Deliberate boundaries:

- **Core, not ORM.** No declarative models, no relationships, no lazy loading.
  Repositories issue explicit statements and map rows to the Pydantic models in
  ``app/models/`` themselves, which keeps the API contract the only definition
  of a response shape.
- **snake_case here, camelCase in the API.** ``arrived_at`` / ``location_source``
  / ``uploaded_by`` / ``rider_name`` are database names. The mapping to
  ``arrivedAt`` / ``locationSource`` / ``uploadedBy`` / ``riderName`` happens in
  the repository layer, not here.
- **No business logic.** No defaults that invent values, no id generation — ids
  arrive from the client (docs/api-contract.md, "Idempotency").
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)

# One shared MetaData for the whole data layer. Reflection in tests and any
# future tooling binds to this object, so every table must be registered on it.
metadata = MetaData()

# Trip — the journal itself. The two slug columns are the entire access model:
# rider_slug grants read+write, viewer_slug grants read-only, and both are
# unique because resolving a slug to a trip is the first step of every request.
trips = Table(
    "trips",
    metadata,
    Column("id", Text, primary_key=True, nullable=False),
    Column("name", Text, nullable=False),
    Column("rider_slug", Text, nullable=False, unique=True),
    Column("viewer_slug", Text, nullable=False, unique=True),
    Column("start_date", Date, nullable=False),
    # migrations/0002_trips_slugs_differ_check.sql. Per-column UNIQUE does not
    # stop one *row* from carrying the same string twice, and that row is a
    # privilege escalation: security.py derives access as "rider if the slug
    # equals rider_slug, else viewer", so on such a row a link issued as
    # read-only resolves to RIDER. The two links are indistinguishable because
    # there is only one link. Cheap to enforce — a CHECK runs on write, never
    # on the slug lookup every request performs.
    CheckConstraint(
        "rider_slug <> viewer_slug",
        name="trips_slugs_differ_check",
    ),
)

# Stop — captured on the road, possibly offline, with a client-generated id.
stops = Table(
    "stops",
    metadata,
    Column("id", Text, primary_key=True, nullable=False),
    Column("trip_id", Text, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    # Double -> DOUBLE PRECISION. Coordinates come from the browser geolocation
    # API as IEEE doubles and are only ever plotted, never accumulated.
    Column("lat", Double, nullable=False),
    Column("lng", Double, nullable=False),
    # Mirrors LocationSource in app/models/stop.py. There is no third "unknown"
    # value: both frontend capture paths know which one they are.
    Column("location_source", Text, nullable=False),
    Column("arrived_at", DateTime(timezone=True), nullable=False),
    Column("notes", Text, nullable=True),
    CheckConstraint(
        "location_source IN ('gps', 'manual')",
        name="stops_location_source_check",
    ),
    Index("ix_stops_trip_id", "trip_id"),
)

# Photo — one uploaded image on a stop.
#
# There is no `url` column and no `archived` column, and that is not an
# oversight: PhotoOut.url is presigned per request from object_key by the
# storage/ module, and PhotoOut.archived is derived as
# (one_drive_file_id IS NOT NULL). Storing either would create a second source
# of truth that can disagree with the first.
photos = Table(
    "photos",
    metadata,
    Column("id", Text, primary_key=True, nullable=False),
    Column("stop_id", Text, ForeignKey("stops.id", ondelete="CASCADE"), nullable=False),
    Column("object_key", Text, nullable=False),
    Column("one_drive_file_id", Text, nullable=True),
    Column("uploaded_by", Text, nullable=False),
    Column("taken_at", DateTime(timezone=True), nullable=False),
    Index("ix_photos_stop_id", "stop_id"),
)

# Bike — a motorcycle on the trip. Rider identity lives here and on
# photos.uploaded_by; spec Section 3 has no separate riders table.
bikes = Table(
    "bikes",
    metadata,
    Column("id", Text, primary_key=True, nullable=False),
    Column("trip_id", Text, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
    Column("rider_name", Text, nullable=False),
    Column("make", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("year", Integer, nullable=False),
    # Matches BikeCreate.specs, which defaults to "" rather than None, so
    # "nothing written yet" has exactly one representation in the database.
    Column("specs", Text, nullable=False, server_default=text("''")),
    Index("ix_bikes_trip_id", "trip_id"),
)

__all__ = ["bikes", "metadata", "photos", "stops", "trips"]
