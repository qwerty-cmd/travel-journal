-- 0003_accounts_membership
-- Decision-log Entry 29 (accounts, public trips, leader-gated membership).
-- Forward-only, additive/relaxing: the pre-0003 image runs unchanged on this schema.

-- Accounts. id is a server-generated UUID4 string (text, matching 0001's id convention).
CREATE TABLE IF NOT EXISTS users (
    id                  text        NOT NULL PRIMARY KEY,
    username            text        NOT NULL,           -- stored lowercased; private login handle
    display_name        text        NOT NULL,           -- public
    password_hash       text        NOT NULL,           -- argon2id PHC string
    recovery_code_hash  text        NULL,               -- sha256 hex of the current one-time code
    failed_logins       integer     NOT NULL DEFAULT 0,
    locked_until        timestamptz NULL,
    disabled_at         timestamptz NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_username_key UNIQUE (username),
    CONSTRAINT users_username_format_check CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
    CONSTRAINT users_display_name_length_check CHECK (char_length(display_name) BETWEEN 1 AND 40),
    CONSTRAINT users_failed_logins_check CHECK (failed_logins >= 0)
);

-- Sessions: only the SHA-256 of the cookie token is stored.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash          bytea       NOT NULL PRIMARY KEY,
    user_id             text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_used_at        timestamptz NOT NULL DEFAULT now(),
    absolute_expires_at timestamptz NOT NULL,
    CONSTRAINT sessions_token_hash_length_check CHECK (octet_length(token_hash) = 32),
    CONSTRAINT sessions_expiry_check CHECK (absolute_expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions (user_id);

-- Trips: visibility, delay, provenance. Existing rows become private via the default.
ALTER TABLE trips ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'private';
ALTER TABLE trips ADD COLUMN IF NOT EXISTS public_delay_hours integer NOT NULL DEFAULT 24;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS created_at timestamptz NULL;   -- NULL = pre-0003, unknown
ALTER TABLE trips ALTER COLUMN created_at SET DEFAULT now();              -- new rows only
ALTER TABLE trips ALTER COLUMN rider_slug  DROP NOT NULL;                 -- app-created trips have none
ALTER TABLE trips ALTER COLUMN viewer_slug DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trips_visibility_check' AND conrelid = 'trips'::regclass) THEN
        ALTER TABLE trips ADD CONSTRAINT trips_visibility_check CHECK (visibility IN ('public', 'private'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trips_public_delay_hours_check' AND conrelid = 'trips'::regclass) THEN
        ALTER TABLE trips ADD CONSTRAINT trips_public_delay_hours_check CHECK (public_delay_hours BETWEEN 0 AND 168);
    END IF;
    -- Both slugs or neither: a half-slugged trip has no meaning under either model.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trips_slugs_paired_check' AND conrelid = 'trips'::regclass) THEN
        ALTER TABLE trips ADD CONSTRAINT trips_slugs_paired_check CHECK ((rider_slug IS NULL) = (viewer_slug IS NULL));
    END IF;
END
$$;
-- UNIQUE(rider_slug), UNIQUE(viewer_slug) and trips_slugs_differ_check still hold: NULLs are distinct / CHECK passes on NULL.

CREATE INDEX IF NOT EXISTS ix_trips_created_by ON trips (created_by);
CREATE INDEX IF NOT EXISTS ix_trips_public ON trips (id) WHERE visibility = 'public';

-- Authorship on children (NULL for pre-0003 rows and for writes by the pre-0003 image).
ALTER TABLE stops  ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;
ALTER TABLE bikes  ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;

-- Delay filter + lastPublicStopAt: max(arrived_at) per trip under a bound.
CREATE INDEX IF NOT EXISTS ix_stops_trip_id_arrived_at ON stops (trip_id, arrived_at);

-- Memberships: surrogate id so revoked rows are kept as history; one ACTIVE row per (trip, user).
CREATE TABLE IF NOT EXISTS trip_members (
    id          text        NOT NULL PRIMARY KEY,
    trip_id     text        NOT NULL REFERENCES trips (id) ON DELETE CASCADE,
    user_id     text        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    role        text        NOT NULL,
    joined_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz NULL,
    revoked_by  text        NULL REFERENCES users (id) ON DELETE SET NULL,  -- NULL with revoked_at set = operator CLI
    CONSTRAINT trip_members_role_check CHECK (role IN ('rider', 'leader')),
    CONSTRAINT trip_members_revoked_by_check CHECK (revoked_by IS NULL OR revoked_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trip_members_active ON trip_members (trip_id, user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_trip_members_user_id ON trip_members (user_id);

-- Join requests: per person. One pending per (trip, user) via partial unique index.
CREATE TABLE IF NOT EXISTS join_requests (
    id          text        NOT NULL PRIMARY KEY,   -- server-generated UUID4
    trip_id     text        NOT NULL REFERENCES trips (id) ON DELETE CASCADE,
    user_id     text        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    state       text        NOT NULL DEFAULT 'pending',
    via         text        NOT NULL DEFAULT 'direct',
    message     text        NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz NULL,                   -- set on every exit from pending (incl. cancel)
    decided_by  text        NULL REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT join_requests_state_check CHECK (state IN ('pending', 'approved', 'rejected', 'cancelled', 'blocked')),
    CONSTRAINT join_requests_via_check CHECK (via IN ('direct', 'legacy_rider_link')),
    CONSTRAINT join_requests_message_length_check CHECK (message IS NULL OR char_length(message) <= 280),
    CONSTRAINT join_requests_decided_check CHECK ((state = 'pending') = (decided_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_join_requests_one_pending ON join_requests (trip_id, user_id) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS ix_join_requests_trip_state ON join_requests (trip_id, state);
CREATE INDEX IF NOT EXISTS ix_join_requests_user_state ON join_requests (user_id, state);

COMMENT ON COLUMN trips.rider_slug IS
    'Legacy locator only (Entry 29). Grants nothing; writes need an active trip_members row. NULL for app-created trips.';
COMMENT ON COLUMN trips.viewer_slug IS
    'Legacy read link for pre-0003 trips; kept until the legacy removal window closes. NULL for app-created trips.';
COMMENT ON COLUMN photos.uploaded_by IS
    'Display name at upload time: the uploading account''s display_name since 0003, free text before it.';
