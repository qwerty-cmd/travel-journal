"""
SQLAlchemy Core table metadata for the Postgres schema.

This is the Python-side mirror of ``migrations/*.sql`` (0001 through 0004) — the
SQL files are what actually create the schema, this module is what the
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
  arrive from the client (docs/api-contract.md, "Idempotency"). The
  ``server_default`` values below are the migrations' own column defaults, mirrored
  so the metadata describes the same table; they are not Python-side defaults.
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
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)

# One shared MetaData for the whole data layer. Reflection in tests and any
# future tooling binds to this object, so every table must be registered on it.
metadata = MetaData()

# User — an account (migrations/0003_accounts_membership.sql, decision-log
# Entry 29). id is a server-generated UUID4 string, text like every other id.
# username is stored lowercased and is the private login handle; display_name
# is the public one. Users are never deleted by the app.
users = Table(
    "users",
    metadata,
    Column("id", Text, primary_key=True, nullable=False),
    Column("username", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    # argon2id PHC string.
    Column("password_hash", Text, nullable=False),
    # sha256 hex of the current one-time recovery code.
    Column("recovery_code_hash", Text, nullable=True),
    Column("failed_logins", Integer, nullable=False, server_default=text("0")),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column(
        "password_changed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint("username", name="users_username_key"),
    CheckConstraint(
        "username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'",
        name="users_username_format_check",
    ),
    CheckConstraint(
        "char_length(display_name) BETWEEN 1 AND 40",
        name="users_display_name_length_check",
    ),
    CheckConstraint("failed_logins >= 0", name="users_failed_logins_check"),
)

# Session — only the SHA-256 of the cookie token is stored, never the token.
# Rows cascade with their user. failed_confirmations (0004, Entry 33) counts
# wrong password confirmations on this session; the threshold lives in code.
sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", LargeBinary, primary_key=True, nullable=False),
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("last_used_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("absolute_expires_at", DateTime(timezone=True), nullable=False),
    Column("failed_confirmations", Integer, nullable=False, server_default=text("0")),
    CheckConstraint("octet_length(token_hash) = 32", name="sessions_token_hash_length_check"),
    CheckConstraint("absolute_expires_at > created_at", name="sessions_expiry_check"),
    CheckConstraint("failed_confirmations >= 0", name="sessions_failed_confirmations_check"),
    Index("ix_sessions_user_id", "user_id"),
)

# Trip — the journal itself. Since 0003 (Entry 29) access comes from
# visibility and trip_members, not from the slugs: rider_slug is a legacy
# locator that grants nothing, viewer_slug a legacy read link for pre-0003
# trips. Both are NULL on app-created trips, stay unique where present, and are
# set together or not at all (trips_slugs_paired_check).
trips = Table(
    "trips",
    metadata,
    Column("id", Text, primary_key=True, nullable=False),
    Column("name", Text, nullable=False),
    Column("rider_slug", Text, nullable=True, unique=True),
    Column("viewer_slug", Text, nullable=True, unique=True),
    Column("start_date", Date, nullable=False),
    # Pre-0003 trips were backfilled to 'private' / 24 by these defaults.
    Column("visibility", Text, nullable=False, server_default=text("'private'")),
    Column("public_delay_hours", Integer, nullable=False, server_default=text("24")),
    Column("created_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    # NULL = a pre-0003 trip whose creation time is unknown; new rows get now().
    Column("created_at", DateTime(timezone=True), nullable=True, server_default=text("now()")),
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
    CheckConstraint("visibility IN ('public', 'private')", name="trips_visibility_check"),
    CheckConstraint(
        "public_delay_hours BETWEEN 0 AND 168",
        name="trips_public_delay_hours_check",
    ),
    # Both slugs or neither: a half-slugged trip has no meaning under either model.
    CheckConstraint(
        "(rider_slug IS NULL) = (viewer_slug IS NULL)",
        name="trips_slugs_paired_check",
    ),
    Index("ix_trips_created_by", "created_by"),
    Index("ix_trips_public", "id", postgresql_where=text("visibility = 'public'")),
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
    # Authorship since 0003. NULL for pre-0003 rows and for writes by the
    # pre-0003 image; SET NULL so a stop is never lost with an account.
    Column("created_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    CheckConstraint(
        "location_source IN ('gps', 'manual')",
        name="stops_location_source_check",
    ),
    Index("ix_stops_trip_id", "trip_id"),
    # Delay filter and lastPublicStopAt: max(arrived_at) per trip under a bound.
    Index("ix_stops_trip_id_arrived_at", "trip_id", "arrived_at"),
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
    # Display name at upload time: the account's display_name since 0003, free
    # text before it.
    Column("uploaded_by", Text, nullable=False),
    Column("taken_at", DateTime(timezone=True), nullable=False),
    # Authorship since 0003; see stops.created_by.
    Column("created_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
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
    # Authorship since 0003; see stops.created_by.
    Column("created_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Index("ix_bikes_trip_id", "trip_id"),
)

# Trip membership. A surrogate id so revoked rows are kept as history; at most
# one ACTIVE (revoked_at IS NULL) row per (trip, user). user_id is RESTRICT so
# an operator's manual user delete fails loudly instead of dropping
# memberships. revoked_by NULL with revoked_at set = revoked by the operator CLI.
trip_members = Table(
    "trip_members",
    metadata,
    Column("id", Text, primary_key=True, nullable=False),
    Column("trip_id", Text, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", Text, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("role", Text, nullable=False),
    Column("joined_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("revoked_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    CheckConstraint("role IN ('rider', 'leader')", name="trip_members_role_check"),
    CheckConstraint(
        "revoked_by IS NULL OR revoked_at IS NOT NULL",
        name="trip_members_revoked_by_check",
    ),
    Index(
        "ux_trip_members_active",
        "trip_id",
        "user_id",
        unique=True,
        postgresql_where=text("revoked_at IS NULL"),
    ),
    Index("ix_trip_members_user_id", "user_id"),
)

# Join request — per person. At most one PENDING row per (trip, user);
# decided rows are kept. decided_at is set on every exit from pending,
# including cancel (join_requests_decided_check).
join_requests = Table(
    "join_requests",
    metadata,
    # Server-generated UUID4.
    Column("id", Text, primary_key=True, nullable=False),
    Column("trip_id", Text, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", Text, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("via", Text, nullable=False, server_default=text("'direct'")),
    Column("message", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("decided_by", Text, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    CheckConstraint(
        "state IN ('pending', 'approved', 'rejected', 'cancelled', 'blocked')",
        name="join_requests_state_check",
    ),
    CheckConstraint("via IN ('direct', 'legacy_rider_link')", name="join_requests_via_check"),
    CheckConstraint(
        "message IS NULL OR char_length(message) <= 280",
        name="join_requests_message_length_check",
    ),
    CheckConstraint(
        "(state = 'pending') = (decided_at IS NULL)",
        name="join_requests_decided_check",
    ),
    Index(
        "ux_join_requests_one_pending",
        "trip_id",
        "user_id",
        unique=True,
        postgresql_where=text("state = 'pending'"),
    ),
    Index("ix_join_requests_trip_state", "trip_id", "state"),
    Index("ix_join_requests_user_state", "user_id", "state"),
)

__all__ = [
    "bikes",
    "join_requests",
    "metadata",
    "photos",
    "sessions",
    "stops",
    "trip_members",
    "trips",
    "users",
]
