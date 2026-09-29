# Migrations

SQL migrations for the Postgres schema (Trip, Stop, Photo, Bike — spec Section 3),
matching the Pydantic models in `app/models/` and the Core table metadata in
`app/data/tables.py`. The same files run against the local docker-compose
Postgres and against Neon — the only difference is `DATABASE_URL`.

## Running them

```bash
cd backend
docker compose -f ../docker-compose.yml up -d postgres   # local only
uv run python -m app.data.migrate
```

The runner is `app/data/migrate.py`. It:

- creates `schema_migrations (version text primary key, applied_at timestamptz not null default now())`
  if it doesn't exist — that's the ledger of what has already run;
- applies every `*.sql` file in this directory, **in filename order**, that
  isn't already recorded;
- sends each file to Postgres **whole and unsplit**, then inserts its ledger row,
  both in a single transaction — so a file either lands completely or not at
  all, and there is no half-applied state for the next run to trip over;
- prints what it applied.

Running it twice is safe and expected: the second run applies nothing and says
so. That's what makes it usable as a container start-up step.

`DATABASE_URL` may be either a plain `postgresql://` URL (what `.env.example`,
`psql` and the Neon console all give you) or an explicit `postgresql+asyncpg://`
one. `app/data/db.normalize_database_url` rewrites the former onto the async
driver, because `create_async_engine` rejects a non-async dialect outright.

## Adding a migration

1. Create `NNNN_short_description.sql` — a **four-digit zero-padded** prefix, one
   higher than the current highest, then snake_case. Zero-padded because
   ordering is lexical on the filename: unpadded, `10_...` would run before
   `2_...`.
2. Write plain SQL, separating statements with `;` as you would in `psql`. The
   runner does **not** parse or split the file: it hands the whole thing to the
   driver's simple query protocol in one call, so anything `psql` accepts as a
   script is accepted here — including dollar-quoted function bodies and nested
   block comments. Two consequences worth knowing: bind parameters are not
   available (a migration is static SQL, not a parameterised query), and an
   error is reported against the file rather than against a statement number.
3. Add the matching change to `app/data/tables.py` in the **same patch**.
   `tests/test_schema.py` reflects the live database and compares it to that
   metadata, so a migration without the corresponding Core change fails the
   suite.
4. Never edit a migration that has already been applied anywhere. Its version is
   recorded, so an edited file is simply never re-run — the change would exist on
   your machine and nowhere else. Write a new file instead.

Migrations are forward-only. There are no `down`/rollback files: the schema is
small, the deploy target is a single container, and a rollback script that has
never been run is worse than not having one.

## Files

| File | What it does |
|---|---|
| `0001_initial_schema.sql` | Creates `trips`, `stops`, `photos`, `bikes`, their FK indexes and the `location_source` check constraint. |
| `0002_trips_slugs_differ_check.sql` | Adds `trips_slugs_differ_check` — `rider_slug <> viewer_slug`. A row with both slugs equal resolves to rider access, so a read-only link would silently grant writes. |
| `0003_accounts_membership.sql` | Accounts and membership (decision-log Entry 29): creates `users`, `sessions`, `trip_members` and `join_requests`; adds `visibility`, `public_delay_hours`, `created_by` and `created_at` to `trips` and `created_by` to `stops`, `photos` and `bikes`; makes both slugs nullable, paired by `trips_slugs_paired_check`. Additive and relaxing only: existing trips become private with a 24h delay through column defaults, slugs are untouched, and the pre-0003 image runs unchanged on the result. |
