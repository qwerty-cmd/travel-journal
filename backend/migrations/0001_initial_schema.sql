-- 0001_initial_schema
--
-- The Trip / Stop / Photo / Bike schema from spec Section 3, as the API
-- contract in docs/api-contract.md settled it (Session 1).
--
-- Conventions this file commits to, so later migrations don't have to re-decide:
--
--   * Column names are snake_case. The camelCase names in the API
--     (arrivedAt, locationSource, uploadedBy, riderName) exist only in the
--     Pydantic models; mapping between the two is the repository layer's job.
--   * Primary keys are `text`, not `uuid`. Ids are generated on the client at
--     capture time (docs/api-contract.md, "Idempotency"), and the contract
--     deliberately does not enforce UUID4 format at the model layer. A `uuid`
--     column would turn a malformed client id into a raw driver error deep in
--     the data layer instead of something a route can map to a 422.
--   * Every timestamp is `timestamptz`. The trip crosses time zones; a naive
--     timestamp column would make "when did we arrive" ambiguous the first
--     time the trip changes zone.
--   * Child rows are deleted with their parent (ON DELETE CASCADE). There is
--     no delete endpoint in v1, so this only matters for seeding/teardown.

-- Trip — one row per journal. The two slugs ARE the access model: there are no
-- accounts and no sessions, so whoever holds a link has exactly the rights that
-- link carries (rider = read+write, viewer = read-only).
CREATE TABLE IF NOT EXISTS trips (
    id          text NOT NULL PRIMARY KEY,
    name        text NOT NULL,
    -- Both slugs are UNIQUE for correctness, not just speed: resolving a slug
    -- to a trip is the first thing every single request does, and two trips
    -- sharing a slug would silently hand one trip's write access to the other.
    rider_slug  text NOT NULL UNIQUE,
    viewer_slug text NOT NULL UNIQUE,
    start_date  date NOT NULL
);

COMMENT ON COLUMN trips.rider_slug IS
    'Unguessable read+write slug. Possession of this value is the whole write authorisation.';
COMMENT ON COLUMN trips.viewer_slug IS
    'Unguessable read-only slug. Write endpoints reject it with 403 FORBIDDEN, never 404.';

-- Stop — a place the riders stopped, captured on the road and possibly offline.
CREATE TABLE IF NOT EXISTS stops (
    id              text NOT NULL PRIMARY KEY,
    trip_id         text NOT NULL REFERENCES trips (id) ON DELETE CASCADE,
    name            text NOT NULL,
    -- double precision, not numeric: these come straight from the browser's
    -- geolocation API as IEEE doubles and are only ever plotted, never summed.
    lat             double precision NOT NULL,
    lng             double precision NOT NULL,
    -- Mirrors LocationSource in app/models/stop.py. The CHECK exists so a typo
    -- in a repository fails at write time rather than producing a value the
    -- Pydantic enum will refuse to serialise back out on read.
    location_source text NOT NULL CHECK (location_source IN ('gps', 'manual')),
    arrived_at      timestamptz NOT NULL,
    notes           text NULL
);

COMMENT ON COLUMN stops.location_source IS
    'How lat/lng were obtained: ''gps'' = automatic fix, ''manual'' = rider tapped the map.';

-- Photo — one uploaded image, attached to a stop.
--
-- Two fields the API returns that are deliberately NOT columns here:
--
--   * PhotoOut.url is a presigned GET URL minted per request by the storage/
--     module (docs/api-contract.md, "Photo serving"). Persisting it would bake
--     an expiry and a provider hostname into the database, and would break the
--     day MinIO becomes R2. The DB stores object_key; the URL is derived.
--   * PhotoOut.archived is DERIVED, not stored: archived = (one_drive_file_id
--     IS NOT NULL). The OneDrive sync sets the file id when it lands the photo,
--     so a separate boolean would be a second source of truth for the same
--     fact and could disagree with it.
CREATE TABLE IF NOT EXISTS photos (
    id                text NOT NULL PRIMARY KEY,
    stop_id           text NOT NULL REFERENCES stops (id) ON DELETE CASCADE,
    object_key        text NOT NULL,
    -- NULL until the background OneDrive archive sync succeeds. Nullable is the
    -- normal steady state for a freshly uploaded photo, not an error state.
    one_drive_file_id text NULL,
    uploaded_by       text NOT NULL,
    taken_at          timestamptz NOT NULL
);

COMMENT ON COLUMN photos.object_key IS
    'Key in the S3-compatible store. The served URL is presigned from this at read time and never stored.';
COMMENT ON COLUMN photos.one_drive_file_id IS
    'Microsoft Graph file id, NULL until the background archive sync lands this photo. PhotoOut.archived is derived as (one_drive_file_id IS NOT NULL) — there is no stored archived column.';
COMMENT ON COLUMN photos.uploaded_by IS
    'Display name only. There is no user account behind it (spec Section 6).';

-- Bike — a motorcycle on the trip. Rider identity lives here and on
-- photos.uploaded_by; spec Section 3 deliberately has no riders table.
CREATE TABLE IF NOT EXISTS bikes (
    id         text NOT NULL PRIMARY KEY,
    trip_id    text NOT NULL REFERENCES trips (id) ON DELETE CASCADE,
    rider_name text NOT NULL,
    make       text NOT NULL,
    model      text NOT NULL,
    year       integer NOT NULL,
    -- NOT NULL DEFAULT '' matches BikeCreate.specs, which defaults to the empty
    -- string rather than None — so "no specs written yet" is one value here,
    -- not two (NULL and '') that the API would have to collapse on read.
    specs      text NOT NULL DEFAULT ''
);

-- Foreign-key columns are not indexed automatically by Postgres, and every
-- read path in the API is "all children of this parent" (stops for a trip,
-- photos for a stop, bikes embedded in GET /trips/{slug}).
CREATE INDEX IF NOT EXISTS ix_stops_trip_id ON stops (trip_id);
CREATE INDEX IF NOT EXISTS ix_photos_stop_id ON photos (stop_id);
CREATE INDEX IF NOT EXISTS ix_bikes_trip_id ON bikes (trip_id);
