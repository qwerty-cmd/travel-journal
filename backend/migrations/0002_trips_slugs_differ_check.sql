-- 0002_trips_slugs_differ_check
--
-- Forbid a trip from carrying the same string in both slug columns.
--
-- Why this is an access-control fix and not tidiness: `security.py` derives
-- permission with `Access.RIDER if slug == trip.rider_slug else Access.VIEWER`.
-- On a row where rider_slug = viewer_slug, the two branches both describe the
-- same string and the tie goes to the *greater* permission — so a link handed
-- out as read-only would grant writes, silently, with every 403/404 test still
-- passing. The row is the whole bug; no code path can tell the two links apart
-- once it exists, because there is only one link.
--
-- Note this is a *different* shape from the cross-trip slug collision that
-- decision-log entry 8 raised and dismissed. There the ambiguity was "which
-- trip did you mean", and every answer the lookup could give was internally
-- consistent. Here the ambiguity is "what may you do", and the answer is wrong.
-- Entry 8's cost argument does not carry over either: it rejected a second
-- *index* on the slug lookup, which is on the critical path of every request.
-- A CHECK is evaluated on INSERT/UPDATE only and costs a lookup nothing.
--
-- It is cheap to state and the failure it prevents is unrecoverable-by-review:
-- story `s-seed-trip-record` is the next thing that inserts a trip, and a
-- copy-paste of `rider_slug` into `viewer_slug` there produces a privilege
-- escalation that looks exactly like a working seed.
--
-- Guarded by pg_constraint rather than written as a bare ALTER so the file is
-- idempotent on its own, matching the `IF NOT EXISTS` style of
-- 0001_initial_schema.sql. The runner's ledger already prevents a second apply;
-- this makes the file safe to replay by hand against a database whose ledger
-- was lost or rebuilt.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'trips_slugs_differ_check'
          AND conrelid = 'trips'::regclass
    ) THEN
        ALTER TABLE trips
            ADD CONSTRAINT trips_slugs_differ_check CHECK (rider_slug <> viewer_slug);
    END IF;
END
$$;

COMMENT ON CONSTRAINT trips_slugs_differ_check ON trips IS
    'The two slugs must be different strings. Equal slugs would resolve to rider access, silently turning a read-only link into a writable one.';
