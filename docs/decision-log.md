# Decision Log — contested calls and why they resolved the way they did

This is not a changelog. It records **disagreements between agents** (or between an agent and
the orchestrator) and how they were settled — specifically, the reasoning that *lost*.

Why that matters: the losing argument is the part that evaporates when a session ends. The code
that survives is visible in git; the option that was tried, measured and rejected is not. Without
this file, a future agent rediscovers the same constraint, reaches the same first conclusion, and
re-introduces something that was already disproved.

**Do not read this whole file.** Scan the index below, then read only entries relevant to your current task.

**Except when you are reopening a settled call** — if you are about to argue for something recorded
here as rejected, read that entry in full first. **The burden is on whoever reopens it to show the
original evidence no longer applies, not to re-derive that evidence from scratch.**

Owned by the `docs` agent. A new entry gets written whenever one agent overrules, contradicts, or
empirically disproves another.

## Index

| # | Topic | Files | One-line summary |
|---|-------|-------|-----------------|
| 1 | SQL splitter vs asyncpg simple-query | `migrate.py` | Bespoke splitter deleted; asyncpg simple-query works inside SA transactions — qa proved it |
| 2 | Vacuous slug-uniqueness test | `test_schema.py` | Substring-match test couldn't fail; fixed to assert per-column against pg_index |
| 3 | Slug uniqueness — ON CONFLICT DO UPDATE | `progress.json`, seed script | DO UPDATE on slugs is an authorization leak, not an upsert — let UNIQUE reject and regenerate |
| 4 | "Add a note" ambiguity | `map.py`, `__init__.py` | Barrel-export instruction was ambiguous; cost a recorded rationale |
| 5 | Barrel export convention | `models/__init__.py` | One-off barrel re-export rejected — don't invent conventions for a single use |
| 6 | ErrorCode missing 405/500 members | `common.py`, `errors.py`, `main.py` | ba escalated rather than inventing codes; user added INTERNAL_ERROR + METHOD_NOT_ALLOWED. **Decision settled; its 405 illustration had two pending expiries and BOTH HAVE NOW FIRED** — (1) `t-head-on-get-routes` landed 2026-09-14, so `/api/health` is GET+HEAD and the entry's "GET-only" wording is false; (2) `t-stops-list-endpoint` landed 2026-09-15 (`3ebded7`), so `DELETE /trips/{slug}/stops` is now **405 with `Allow: GET, HEAD`**, not 404 — verified by `test_spa_delete_on_the_registered_stops_path_is_405`, not predicted. Both were wording only: POST is in neither method set, `qa`'s original 404 reading was correct for the tree it was taken against, and the rule (a 405 needs a *registered* path with a *different* method) does not expire. `t-stops-405-doc-revisit` is **closed** — all three edits landed; see correction + the 2026-09-14 and two 2026-09-15 footnotes |
| 7 | Error handler mutation coverage | `errors.py`, `main.py`, `test_error_envelope.py` | qa found INTERNAL_ERROR leaked message + SPA catch-all swallowed /api 404s; green suite missed both — mutation testing validates the tests you thought to write, never a path you didn't consider. **Both halves now fixed**: the 405-collapse half closed 2026-09-14 by `t-405-router-route-collapse` (qa-verified on real uvicorn, zero surviving mutants). The body's 2026-09-14 correction is the record of what was broken then, not present state — but its "the catch-all stays" reasoning is still live before touching `main.py` |
| 8 | Cross-trip slug collision | `0001_initial_schema.sql`, `security.py` | Same slug on two trips is fine (random tokens); same slug on one row is not — migration 0002 added CHECK |
| 9 | Seed script print-before-commit | `seed_trip.py` | Print slugs after commit, not before — interrupted print + committed row = unrecoverable slug loss |
| 10 | A shallow copy reasoned about as deep — twice in one patch | `main.py`, `test_error_envelope.py` | `qa` disproved `dev`'s stated reason for the scope copy (the `fastapi` key is *always* already present at handler time); `test-writer` disproved the orchestrator's fix for the resulting test gap (shallow snapshot compared the inner dict against itself). Code was right, reason was wrong; test looked like coverage and had none |
| 11 | HEAD on GET routes: `methods=["GET", "HEAD"]` vs a second registration | `main.py`, `trips.py`, `test_head_method.py` | User chose the explicit method list; `dev` measured that FastAPI emits a duplicate `head:` operation and a duplicate operationId from it, which Kubb turns into a duplicate hook. Shipped as a second, schema-excluded registration of the *same handler* — user's intent kept, literal spelling not. Collapsing the two back into one route leaves **189 of 191 tests green**: only the two OpenAPI guards fail |
| 12 | Finding triage gate, and who may write the governance files | `docs/finding-triage-gate.md`, `CLAUDE.md`, `.claude/**` | The orchestrator's weaker three-question triage was replaced by the user's gate — Stop Condition names the over-investigation behaviours rather than trusting judgment, QA classification is evidence not authority. Separately: no agent can write `CLAUDE.md` or `.claude/**`, so the **orchestrator** owns those paths; widening `docs` to `.claude/**` was rejected as self-modifying permissions |
| 13 | The one backlog item the gate promoted — Gate 2 vs "the slug is already in the access log" | `core/errors.py`, `progress.json` | `ba` classified `t-error-log-parameter-redaction` CURRENTLY OBSERVABLE (handler and route both run today; qa *observed* the slug in the rendered traceback). The note's own argument — marginal disclosure is zero, so ORDINARY DEBT — lost: Gate 2 tests whether a runtime path is current, not how severe it is. Orchestrator additionally found the task closes only **one of two** sinks; re-scoped, not re-classified |
| 14 | A client-generated id that already exists under a *different* trip — the sixth `ErrorCode` | `models/common.py`, `core/errors.py`, `repositories/stops.py`, `api-contract.md` | `ba` escalated a second contract gap rather than inventing a code (Entry 6's rule, applied again); user ruled **`CONFLICT` / `409`**. Replay matches **`(parent, id)`**, never `id` alone, and the cross-parent branch is found **by a check, never a failed INSERT** (that renders `500` and the queue retries forever). Four readings rejected — the composite `(trip_id, id)` **primary key is rejected on cost, not correctness**: it cascades into `photos.stop_id` and the unbuilt photo-upload design. Entry 3 does **not** forbid `ON CONFLICT (id) DO NOTHING` here |
| 15 | `StopCreate.arrivedAt` must be timezone-aware — a naive datetime is a `422` | `api-contract.md`, `models/stop.py` | **Architect ruling (user).** Supersedes an earlier scoping call that the description must *not* claim timezone-awareness *because nothing enforced it* — right about the gap, wrong about which side to close: enforce the claim rather than withdraw it. JSON Schema **cannot express** tz-awareness (`AwareDatetime` and a hand validator emit the same `format: date-time`), so Kubb types it `string` and this is **server-enforced only**. `VALIDATION_ERROR` is never-retry, so a naive value **loses the stop** instead of retrying it — accepted, because a stop silently filed at the wrong hour is unrecoverable. **The endpoint table does not change** — same `422` already on the row. The photo form's `takenAt` was explicitly **left open** here on an EXIF premise — **since ruled the same way by Entry 16, independently, after that premise was tested and failed**; do not cite this entry for it |
| 16 | The photo form's `takenAt` must be timezone-aware — and `PhotoCreateForm` is deleted, not bound | `api-contract.md`, `routes/photos.py`, `models/photo.py` | Two losing arguments. (a) **The EXIF objection, disproved:** "`DateTimeOriginal` is naive by design so reject-naive may be unsatisfiable" — EXIF is never the wire source, the frontend composes the field and `getTimezoneOffset()` is always available. (b) **The gate classification was wrong:** filed TRIGGERED on "no route imports `PhotoCreateForm`" — true of the model, false of the behaviour, because `photos.py` re-declared `takenAt: datetime` inline. **"No route imports it" is not "no route implements it."** Measured: a naive value is resolved in the *host process's* zone by asyncpg, so the same upload stored `02:00Z` on a UTC+8 host and `10:00Z` in the container. Binding a form model was proven impossible on FastAPI 0.141.1 (`_should_embed_body_fields` returns `True` unconditionally past one body field), so the pre-authorised fallback shipped: model deleted, inline `Form(...)` params are the contract. **"Impossible" is too broad: see Entry 24** |
| 17 | `ErrorEnvelope` in the OpenAPI document — the unreachable GET `422`, and the SPA catch-all | `api/responses.py`, `routes/*.py`, `main.py`, `api-contract.md` | Gate 3 item promoted before M3: its trigger (`s-stop-crud`) had fired and lapsed, and Kubb would have typed errors `ErrorEnvelope \| HTTPValidationError`. One constant (403/404/409/422; **405/500 never declared**), model from the constant, description from the route. **"Declare only reachable statuses" lost**: an undeclared `422` gets FastAPI's `HTTPValidationError`, so the four GETs declare an unreachable envelope `422` and deliberately differ from the contract table. The brief's "catch-alls out of scope" premise was disproved: the SPA fallback (registered only when `frontend/dist` exists) re-injected `HTTPValidationError` — `include_in_schema=False` added, qa mutant confirms |
| 18 | M3 URL shape and first open: `/t/$slug`, and the paste-link screen at `/` | `frontend/src/routes/`, `vite.config.ts` (manifest), `docs/user-guide.md` | **Architect ruling (delegated by user, final).** Trip routes are `/t/$slug`. `/` redirects to localStorage `lastSlug`, or shows a one-field "paste your trip link" screen. Reason: an installed iOS web app has storage isolated from Safari and opens at `start_url` `/`, so `lastSlug` is empty on first launch. The same isolation applies to IndexedDB, so the user guide must say "capture from the installed app". The display-name prompt shows **only for `access === "rider"`**, which departs from the literal wording of spec §6.1. Rejected: `/$slug`, a per-trip dynamic manifest, and omitting `start_url`. **MEDIUM confidence on iOS `start_url`**, to verify on a real device in M4 |
| 19 | M3 offline queue: one IndexedDB store drained FIFO from the page, with retry classification | `frontend/src/` (queue module), `api-contract.md` | **Architect ruling (delegated by user, final).** One auto-increment store holding `{kind, payload, blob?, attempts, lastError}`. A stop is always enqueued before its photos, so FIFO order replaces a dependency graph. The page drains the queue (**never SW Background Sync, which iOS lacks**) and stops at the first retryable failure, with backoff from 5s doubling to a 5min cap. Offline cold open works from TripOut persisted per slug and passed as `initialData`. Rejected: per-entity stores, Background Sync, and persisting the whole Query cache. The contract adds `403`/`404` as never-retry, treats no-envelope failures as retry, and keeps a failed item (blob included) until the rider dismisses it |
| 20 | Photo upload is one idempotent request; the "resumable multipart" requirement is amended | `routes/photos.py`, `api-contract.md`, spec §4, `CLAUDE.md` Stack | **Architect ruling (delegated by user, final). Changes a sentence in CLAUDE.md's locked Stack section**, but not the technology (still S3), so it is flagged for the user's morning review. S3/R2 multipart parts must be ≥5 MiB except the last. A ~1600px JPEG is under 1 MiB, so it is always one part and there is nothing to resume. Spec §4's two lines cannot both hold, and compression wins. Resumability lives in the queue: the blob persists in IndexedDB, and a replay is `200` with no storage write, over a deterministic key. Rejected: presigned direct multipart and backend-proxied multipart (both still can't resume <5 MiB). **Supersedes** `t-photo-s3-multipart-upload`'s Gate 1 filing |
| 21 | Write-PIN declined; the rider link stays the only write gate | spec §8, `offline/queue.ts`, `seed_trip.py`, `progress.json` | **Architect ruling (HIGH confidence, no escalation), flagged for the user's morning review because it closes spec §8's open item.** Spec calls the PIN optional everywhere. The rider slug is 256 random bits, so the only threat is a forwarded link, and a PIN is usually forwarded with it. **The PIN lost on cost:** `FORBIDDEN` is never-retry, so a wrong or rotated PIN would permanently fail every queued stop and photo. A safe version needs a 7th `ErrorCode`, a paused-queue state, a prompt UI, per-origin storage and a brute-force policy, all in the top-rigor queue. Mitigation instead: rotate `rider_slug` with one SQL `UPDATE`. Reopen trigger and reopen design are in the body. Spec file left untouched (Entry 20 policy) |
| 22 | Graph refresh token: mint once and reuse, don't persist the rotated one | `storage/onedrive_sync.py`, `deploy-cutover-runbook.md`, `progress.json` | **Architect ruling (option A).** The code comment's premise that Graph "rotates" the token is wrong: Microsoft issues a new refresh token on redemption but **does not revoke the old one**. Each token dies ~90 days after issue (**MEDIUM confidence for personal accounts**, no published number), so the sync works until mint date + ~90 days. **Option B lost on cost, not correctness:** a single-row Postgres table for the rotated token breaks no invariant, but it touches off-limits token handling and puts a long-lived secret in Neon and its backups, for no gain on a trip under ~80 days. A lapse only pauses archiving; nothing is lost. Runbook now says mint close to departure and record the dates |
| 23 | Malformed body → `422` before the access guard — won't-fix | `core/security.py`, `test_stops_create_endpoint.py`, `progress.json` | **Orchestrator, 2026-09-29.** FastAPI parses the body before it solves dependencies. The resulting `422` is **byte-identical for rider, viewer and unknown slugs**, so it is no slug oracle. **"Reorder so the guard answers first" lost:** FastAPI can't do it natively, so it needs a second copy of the access check ahead of parsing, and that copy can drift. Rationale is beside `require_rider_access` |
| 24 | Bound form model for photo upload — won't-fix | `routes/photos.py`, `api-contract.md`, `progress.json` | **Orchestrator, 2026-09-29.** Corrects Entry 16's "impossible": a model carrying `UploadFile` **does** bind, but it flips the OpenAPI request type to `x-www-form-urlencoded`, which breaks the `multipart/form-data` contract and the Kubb client, for a cosmetic gain. Inline `Form(...)` params stay. Rationale is beside the params |
| 25 | `qa`/`docs` write-boundary hooks, promoted ahead of their triggers | `.claude/hooks/*.sh`, `.claude/agents/{qa,docs}.md` | **User call, 2026-09-29, `c9d6b64`.** Triage had these as triggered/ordinary debt with no trigger fired. **Limits:** the `qa` tree guard **detects, doesn't prevent**, misses change-and-revert within one command, and **false-positives when another agent edits the same checkout in parallel**. The `docs` path guard **can't tell a doc comment from a logic edit** inside app/test source. Both narrow prose rules; neither replaces them |
| 26 | Stop and photo times: stored as instants, shown in the reader's zone with the zone labelled | `frontend/src/format.ts`, `models/stop.py`, `api-contract.md`, `progress.json` | **Architect ruling, 2026-09-29, option A.** The review found times rendered with no zone, and the rider's own offset is not stored (`timestamptz` keeps the instant, not the offset). **Option B lost on need, not correctness:** `arrived_offset_minutes` / `taken_offset_minutes` columns would work, but no spec line asks for rider-local time; filed as triggered debt `t-stop-rider-offset`. **Option C is a no-op** (sending a local-offset spelling changes nothing, storage is UTC). **Option D lost on cost** (a timezone lookup from lat/lng adds a dependency) |

---

## 1. Bespoke SQL statement splitter vs. asyncpg's simple-query protocol

**Who:** `dev` vs `qa`. Resolved in `qa`'s favour by the user.
**Where:** `backend/app/data/migrate.py`, task `t-db-schema-migration`.

**`dev`'s position.** SQLAlchemy's normal execute path goes through asyncpg's *extended* query
protocol, which accepts exactly one command per call — so a multi-statement `.sql` migration file
can't just be handed to `conn.execute()`. `dev` wrote a ~70-line `split_sql_statements` scanner to
split the file client-side, and explicitly rejected the alternative (asyncpg's simple-query
protocol, reached via the raw driver connection) on the stated grounds that it "mixes badly with
SQLAlchemy's transaction bookkeeping."

**`qa`'s position.** That specific justification is testable, so `qa` tested it — and it did not
hold. Taking the raw asyncpg connection *inside* the already-open `engine.begin()` block runs the
whole unsplit file via the simple-query protocol **and** keeps the `schema_migrations` ledger
insert in the same SQLAlchemy transaction. `qa` verified atomicity survived a forced mid-file
rollback: nothing half-applied, no orphaned ledger row.

`qa` also found four defects in the scanner itself:
- nested block comments mis-handled;
- block-comment *removal* glued adjacent tokens together — `CREATE/* x */TABLE` became `CREATETABLE`;
- an unterminated dollar-quote was not detected;
- false dollar-quote tag matching.

Its broader argument: bespoke SQL parsing is the wrong machinery for a four-table schema, and the
failure mode of a parser defect is a **corrupted migration against production Neon**. High
consequence, low frequency — the worst category of thing to hand-roll, because the defect surfaces
rarely enough to escape testing and expensively enough to matter when it doesn't.

**Resolution.** The user chose replacement. `split_sql_statements` was deleted. `migrate.py` now
calls `await conn.get_raw_connection()` and executes the file whole on
`raw_connection.driver_connection`, inside the existing transaction.

**Why it matters going forward.** The one-command-per-execute limitation is real and *will* be
rediscovered by anyone touching the migration runner. When that happens, writing a splitter is the
obvious first instinct — and it's the instinct that was already tried and rejected here, not on
taste but because the stated reason for preferring it was shown to be factually false. The
rationale is duplicated as a module docstring in `migrate.py` (i.e. at the place the mistake would
be made), and this entry records *why* that docstring exists. Do not reintroduce a SQL parser.

---

## 2. A vacuous slug-uniqueness guard in `dev`'s test

**Who:** `qa` vs `dev`'s test. Resolved in `qa`'s favour.
**Where:** `backend/tests/test_schema.py::test_slugs_are_unique`, task `t-db-schema-migration`.

**The original test.** `dev` joined every index definition on `trips` into a single string and
asserted `"UNIQUE" in definitions`.

**`qa`'s demonstration.** That assertion passes against a synthetic schema where *neither* slug
column is unique — because `trips_pkey` is itself a UNIQUE index, so the substring "UNIQUE" is
present in the concatenated output no matter what. The test could never fail. It was not a weak
guard; it was not a guard at all.

**Resolution.** Rewritten to query `pg_index.indisunique` per column, filtered to single-column
indexes, parametrised over `rider_slug` and `viewer_slug`. `dev` then proved the new test can
actually fail by running it against a deliberately broken schema — and, to confirm the broken
schema was genuinely non-unique rather than merely indexed differently, inserted two trips sharing
a slug.

**Why it matters going forward.** The live schema was correct the entire time. Only the guard was
illusory — which is the dangerous shape of this bug: a fully green suite actively concealed the
absence of coverage on the thing spec Section 12 ranks *top* priority (access control). Slugs are
the whole authorization model; a non-unique slug is a cross-trip data leak.

The transferable rule: **never assert against concatenated introspection output.** Any check of the
form "dump the schema to a string and look for a keyword" will pass on the strength of some
unrelated object. Assert per-object, against a structured catalog column (`pg_index.indisunique`,
`pg_constraint.contype`, etc.), and prove the test fails against a broken fixture before trusting it.

---

## 3. Slug uniqueness presented to the orchestrator as an open design call

**Who:** orchestrator vs. a stale status summary. Resolved: the premise was stale; no design change made.

**What was proposed.** A summary reached the orchestrator framing slug uniqueness as an unresolved
architectural decision, offering three "paths" to enforce it and asking whether the vacuous test
(Entry 2) should be rewritten.

**What was actually true.** Both items were already closed:
- Database-level `UNIQUE` constraints on `rider_slug` and `viewer_slug` have existed since
  `0001_initial_schema.sql` first applied. Verified against the live database:
  `trips_rider_slug_key`, `trips_viewer_slug_key`.
- The test rewrite had already landed.

The same summary also described the SQL splitter defects as outstanding when the splitter had
already been deleted (Entry 1).

**Resolution.** The premise was corrected against the live database and the current test file. No
design change was made, because there was no open decision.

**Why it matters going forward — two separate things:**

**(a) The database-level constraint is not in question.** It exists, it is verified, and it should
not be re-opened as a design choice.

**(b) The proposed alternatives assumed the wrong kind of slug.** Two of the three paths presumed
*user-derived* slugs (readable, chosen, potentially colliding by coincidence). These are not that.
Per spec Section 4 they are **unguessable random tokens** (`secrets.token_urlsafe`). Two consequences:

- Slug "aesthetics" or readability is not a tradeoff that exists in this system. Any argument that
  weighs it is reasoning about a different design.
- `INSERT ... ON CONFLICT (slug) DO UPDATE` would be **actively harmful here**. It would silently
  overwrite an existing trip's slug — producing exactly the authorization leak the UNIQUE
  constraint prevents. With cryptographically random tokens, a collision is not a routine condition
  to absorb gracefully; it is a **bug signal** (a broken RNG, a reused seed, a duplicated insert).
  The constraint rejecting the insert *loudly* is the correct behaviour. Fail, don't reconcile.

**This is directly relevant to story `s-seed-trip-record`.** When that seeding script is picked up,
it must not use `ON CONFLICT DO UPDATE` on a slug column. Let the insert fail.

---

## 4. "Add a note" — an ambiguous instruction that cost recorded rationale

**Who:** `docs` vs orchestrator. Resolved in `docs`' favour.
**Where:** `docs/progress.json`, task `t-db-schema-migration`.

**What happened.** The orchestrator instructed `docs` to "add a note" to a task entry. `docs`
replaced the existing note rather than appending to it, destroying the recorded reasoning for why
primary keys are `text` rather than `uuid` (ids are client-generated and the contract does not
enforce UUID4 format). `docs` noticed and flagged it rather than proceeding silently.

**`docs`' further argument.** Beyond the mechanical fix, `progress.json` is the wrong home for that
reasoning at all. It is a *checkpoint* file — its job is "what was interrupted and in what state,"
read by the next session after an abrupt failure. It is not where a future reader encounters the
rationale for a column type, because nobody about to change a column type goes looking in a
progress tracker. The durable home is a comment beside the column definitions.

**Resolution.** The note was restored. On checking, the `text`-vs-`uuid` rationale *already* existed
in `0001_initial_schema.sql` — where such a change would actually be attempted — so nothing needed
duplicating.

**Why it matters going forward.** **Rationale belongs where the mistake would be made, not only
where the work is tracked.** Applies generally: a constraint's reason goes next to the constraint, a
driver workaround's reason goes in the module that works around it, a contract quirk's reason goes
in the Pydantic field `description=`. Task-tracker notes are a supplement to that, never a
substitute. Secondarily: an ambiguous instruction ("add a note") that turns out to be destructive
should be flagged, not quietly executed.

---

## 5. A barrel export that would have invented a one-off convention

**Who:** `ba` vs an implied convention in its own task scope. Resolved in `ba`'s favour.
**Where:** `backend/app/models/map.py` / `backend/app/models/__init__.py`, task `t-map-model`.

**What was asked.** `ba` was told to scope `map.py` "plus its export in `__init__.py`."

**What `ba` did.** It checked, and found that **none** of the five existing model modules
(`common`, `trip`, `stop`, `bike`, `photo`) are re-exported from `models/__init__.py`. Adding one
for `map.py` alone would establish a barrel-export convention that applies to exactly one file out
of six — worse than either having the convention or not having it, because the inconsistency reads
as significant when it isn't. `ba` declined and flagged it back instead of complying.

**Resolution.** Scope narrowed to the new file only. `models/__init__.py` left untouched. Imports
throughout the codebase remain `from app.models.map import ...`.

**Why it matters going forward.** A scoping instruction that would silently introduce an
inconsistent convention should be challenged, not followed. Agents receive scope as an instruction,
but scope can be wrong — and a convention introduced by accident in a single task is one that
nobody ever decided and nobody can later explain. If barrel exports are wanted, that's a deliberate
call applied to all six modules at once, not a side effect of one task's file list.

---

## 6. `ErrorCode` had no legal value for a 405 or a 500 — `ba` escalated instead of inventing one

**Who:** `ba` vs. the API contract as written. Escalated to the user, who decided.
**Where:** `docs/api-contract.md` §"Error envelope", `backend/app/models/common.py`, while scoping
task `t-error-envelope-handlers`.

**The contradiction `ba` found.** The contract makes two promises that cannot both hold. It says
*every* non-2xx response uses `ErrorEnvelope`, and it defines `code` as **exactly three** values —
`FORBIDDEN`, `NOT_FOUND`, `VALIDATION_ERROR`. Two statuses reachable in the app as it stands fall
outside all three: a `405` (`POST /api/health` hits a real, `GET`-only route) and an unhandled `500`.
Neither has a code it can legally report. The envelope promise was therefore unsatisfiable, not
merely underspecified.

> **Correction (logged, not silently edited).** As originally written, this entry — and
> `docs/api-contract.md` — cited `DELETE /trips/{slug}/stops` as the reachable `405`. That example
> was **wrong**, and it originated with the orchestrator; `docs` recorded it in good faith without
> checking. `qa` ran the request and got **`404`**. Reason: the stops router is still an empty
> `APIRouter` stub with no methods registered, so Starlette matches no route at all and there is
> nothing for the method to mismatch against. A `405` needs a *registered* path with a *different*
> method. The example has been replaced above with `POST /api/health`, which is verified to return
> `405`. **The entry's conclusion is unaffected** — `405` is genuinely reachable, and five codes
> remain the right call. Only the illustration was false. This correction is recorded rather than
> quietly overwritten: a decision log that rewrites itself invisibly is worth less than one that
> shows where it was wrong and who caught it.
>
> **Footnote (2026-09-14) — the correction above is itself time-limited.** Appended, not merged into
> the correction, for the same reason the correction was appended rather than edited in: the record
> of what `qa` measured and when must stay readable as written.
>
> The correction's finding — `DELETE /trips/{slug}/stops` returns `404`, because the stops router is
> an empty `APIRouter` stub with nothing for the method to mismatch against — **is still true today
> and is not being changed here.** But it is true for a reason with an expiry date. Once `s-stop-crud`
> registers `GET`/`POST` on that path, the path *is* registered, and the same request becomes a
> genuine `405` — which would make this correction's illustration wrong in the opposite direction,
> and would make `qa`'s original measurement look like a mistake rather than an accurate reading of a
> different codebase. Flagged by `ba` while scoping the 405 fix.
>
> Two consequences worth stating separately, because they fail differently:
> - The illustration goes stale. Tracked as `t-stops-405-doc-revisit`; **do not pre-emptively edit it
>   now** — it is correct as it stands.
> - The *behaviour* may not follow the docs. `t-405-router-route-collapse` (Entry 7's 2026-09-14
>   correction) is the reason a registered stops path could still return `404` after `s-stop-crud`
>   lands, silently, via the same `getattr(route, "methods", None)` blindness. If this footnote is
>   being read at `s-stop-crud` and that task has **not** landed, the right move is to verify the
>   observed status before writing anything down — the two failures look identical from the outside,
>   and this entry exists because an unverified illustration was written down once already.
>
> **Footnote (2026-09-15) — the `GET`-only half of this entry's `405` illustration has expired.**
> Appended, not merged into the paragraph above, for the same reason the correction and the first
> footnote were appended: this entry's convention is that its text stays readable as originally
> written, with what falsified it recorded underneath and dated.
>
> `t-head-on-get-routes` landed **2026-09-14** (Entry 11). `/api/health` is now registered for `GET`
> and — via a second, schema-excluded registration of the same handler — `HEAD`. The words
> "`GET`-only" in "The contradiction `ba` found" above are therefore false as of that date. Read the
> illustration as *a real route that does not accept `POST`*, which is the work it was always doing.
>
> **This is wording only. Every part of this entry's decision is unaffected.** `POST` is in neither
> method set — `GET`-only or `GET`+`HEAD` — so `POST /api/health` still returns `405`, the `405` is
> still genuinely reachable, and `METHOD_NOT_ALLOWED` is still a necessary member of the five. Nothing
> here reopens the escalation, the two-codes-not-one call, or the rejected alternatives. If you
> arrived at this footnote intending to re-derive any of those from the falsified clause: don't — the
> clause was illustrative, the conclusion never rested on `HEAD`.
>
> The same sentence in `docs/api-contract.md` ("Status → code mapping") was re-worded **in place** on
> the same patch: that file is a plain contract document with no append-only convention, and a
> contract that carries its own errata inline is harder to read than one that is simply correct.
>
> The correction's *other* expiry has **not** fired. `DELETE /trips/{slug}/stops` still returns `404`
> — the stops router remains an empty `APIRouter` stub — so that illustration stands unedited and
> `t-stops-405-doc-revisit` stays **open** for it until `s-stop-crud`.
>
> **Footnote (2026-09-15, second of the day) — the correction's other expiry has now fired too.**
> Appended, not merged, for the third time and for the same reason: what `qa` measured, and the
> codebase it was measured against, must stay readable exactly as written.
>
> `t-stops-list-endpoint` registered `GET` on `/trips/{slug}/stops` — plus its schema-excluded `HEAD`
> sibling (Entry 11) — in `3ebded7`. The stops router is no longer an empty `APIRouter` stub, so the
> correction's illustration has flipped: **`DELETE /trips/{slug}/stops` now returns `405`, with
> `Allow: GET, HEAD`.** The set becomes `{"GET", "HEAD", "POST"}` once `t-stops-create-endpoint`
> lands.
>
> **This was verified, not predicted — which is what the 2026-09-14 footnote above demanded.** That
> footnote warned that a registered stops path could *silently stay* `404` via the
> `getattr(route, "methods", None)` blindness, and that the two failures are indistinguishable from
> the outside. `t-405-router-route-collapse` has landed, and the observed value is pinned by a test
> rather than by inference: `test_spa_delete_on_the_registered_stops_path_is_405` asserts `405` with
> `Allow` exactly `{"GET", "HEAD"}`, and it passes.
>
> **`qa`'s original measurement was correct and is not being retracted.** It read `404` against a
> codebase where the stops router had no methods registered; what changed is the codebase, not the
> reading. This is precisely the failure mode the 2026-09-14 footnote anticipated — that a later
> reader would mistake an accurate reading of a different tree for a mistake — so it is stated here
> explicitly. **Wording only: every part of this entry's decision is unaffected.** The rule the
> correction exists to teach — a `405` requires a *registered* path with a *different* method — does
> not expire; only the illustration did, and it now illustrates the same rule from the other side.
>
> `docs/api-contract.md`'s matching paragraph was re-worded **in place** on the same patch, per that
> file's convention (no append-only errata in a plain contract document). With this, all three edits
> owned by `t-stops-405-doc-revisit` have landed and the task is **closed**.

**What `ba` did — the part worth recording.** It declined to decide. Its stated reasoning: inventing
a new `ErrorCode` member is a *contract change*, not an implementation detail, and a scoping agent
does not get to make contract changes by writing them into a task description. It escalated instead.

It also refused the silent fix that was available to it. Rather than mapping `405` or `500` onto
`NOT_FOUND` or `VALIDATION_ERROR` to make the gap disappear, it **deliberately left them on FastAPI's
default responses** in the scoped task and flagged the gap — accepting a task that visibly doesn't
meet the contract over one that appears to meet it by lying.

**Resolution.** The user chose **two new codes, five total**: `METHOD_NOT_ALLOWED` and
`INTERNAL_ERROR`. The mapping the handler implements is 403→`FORBIDDEN`, 404→`NOT_FOUND`,
422→`VALIDATION_ERROR`, 405→`METHOD_NOT_ALLOWED`, everything else including unhandled 500→`INTERNAL_ERROR`.

**Rejected alternatives:**
- **One catch-all code for both.** Rejected because a `405` is a *client* error and a `500` is a
  *server* fault. A single code would report both as the same kind of event, which is inaccurate
  about which side is broken — and that distinction is load-bearing (below).
- **Narrowing the contract's promise** so that only *some* non-2xx responses use the envelope.
  Rejected because the uniform shape is the whole reason the envelope exists: the frontend client is
  Kubb-generated, and two response shapes mean two parsing paths at every call site.

A contract-level guarantee was attached at the same time: `message` is documented as safe to show a
rider directly, so the `INTERNAL_ERROR` message is **always a fixed generic string**. The originating
exception text is logged server-side and never returned. A raw database error can carry the database
host and user — this is a leak boundary, not politeness.

**Why it matters going forward — two things:**

**(a) The tempting silent fix is a lie that surfaces later.** Quietly mapping `405` to `NOT_FOUND`
would have made the contract *appear* satisfied while misreporting what happened. It would not have
stayed cosmetic. The offline queue reads `code` to decide whether a request can **ever** succeed —
retry-vs-never-retry is a programmatic branch on that value (spec Section 12 ranks the offline queue
top-three for testing rigor). A `405` disguised as a `404` tells the queue "this resource doesn't
exist" when the truth is "this method never will be accepted," and the wrong retry decision follows
from the wrong code. Making an inconsistency invisible is not the same as resolving it.

**(b) A scoping agent that finds the contract inconsistent should stop, not choose.** This
generalises past this one gap. `ba` is read-only by design and its output is a task description,
which is exactly the wrong instrument for amending a contract: the change would land with no record
of who decided it or why, buried in a task's scope. The correct move when the contract cannot be
satisfied as written is to escalate and leave the gap visible in the meantime.

---

## 7. Mutation testing said the error handlers were covered; `qa` found two holes it structurally couldn't see

**Who:** `qa` vs `dev`. Resolved in `qa`'s favour; one sub-question escalated by `qa` and decided by the user.
**Where:** `backend/app/core/errors.py`, `backend/app/main.py`, `backend/tests/test_error_envelope.py`, task `t-error-envelope-handlers`.

**`dev`'s position, and it was a good one.** Having read Entry 2 (the vacuous slug guard), `dev` did
not just write tests and declare them passing — it mutation-tested its own suite and reported the
results. Deliberately collapsing `403`→`NOT_FOUND` failed 2 tests. Deliberately leaking `str(exc)`
into the `INTERNAL_ERROR` body failed 2 tests. Deleting the `405` mapping failed 2 tests. That is
exactly the discipline Entry 2 was written to produce, and `qa` said so explicitly before disagreeing
with the conclusion drawn from it. The conclusion — "the suite is proven non-vacuous, therefore the
handlers are verified" — is where it broke.

**What `qa` found that the mutations could not.**

**(a) An unguarded leak boundary on the handler nobody was thinking about.** There are three
handlers. `api_error_handler` returned `exc.message` **verbatim, regardless of code** — so an
`ApiError` raised with `INTERNAL_ERROR` would put its own message straight into the response body,
right through the leak boundary the contract makes load-bearing (`api-contract.md`, "`message` and
the `INTERNAL_ERROR` leak boundary"). The generic-string guarantee held on the unhandled-exception
path and not on this one. The mutation testing did not miss this by bad luck: `dev` mutated the
paths it had already reasoned about, which is definitionally the set of paths its tests already
covered. A mutation you think to make is one you already had in mind.

**(b) A test suite that was green because a directory did not exist.** The SPA catch-all in
`main.py` swallows unknown `/api` paths. Every test passed — because the catch-all only mounts when
`frontend/dist` exists, and it doesn't exist yet. The condition was never exercised. `qa` reproduced
the bug by *creating the directory*: unknown `GET /api/*` then returned **`200 text/html`** instead
of a `404` envelope, and unknown non-`GET` returned **`405`** instead of `404`. The frontend build
would have turned this on silently, in Week 3, far from the task that introduced it — and the
generated Kubb client would have received HTML where it expected `ErrorEnvelope`.

**Resolution.** Both fixed under the same task; it stays open until the fixes pass QA.

**The escalation.** `qa` also found that `ApiError` let a caller construct contradictory
status/code pairs (e.g. status `403` with code `NOT_FOUND`) — the exact conflation Entry 6 and the
403-vs-404 section of the contract exist to prevent. `qa` **declined to decide it** and escalated,
on the same reasoning as Entry 6(b): which of several valid API shapes to impose is not a
verification call. The user chose **both** available remedies rather than picking one — classmethod
constructors so the bad pair is unrepresentable at the call site, **plus** an `__init__` assertion
as a backstop for anyone constructing `ApiError` directly.

**Why it matters going forward — this is the transferable part:**

**(a) Mutation testing validates the tests you thought to write. It cannot reveal a path you didn't
consider.** It is a strong check on *vacuousness* (Entry 2's failure mode) and no check at all on
*coverage gaps*. A clean mutation report is evidence the existing assertions bite, not evidence the
assertion set is complete. When something is enforced in more than one place — three handlers, one
leak boundary — enumerate the places and check each, because mutating one of them proves nothing
about the other two.

**(b) A green suite that is green because environment state is absent is not evidence of
correctness. It is evidence the condition isn't being exercised.** The resulting rule, which applies
well beyond this task: **when behaviour is conditional on environment state — a built `frontend/dist`,
a configured credential, a feature flag, a present bucket — the test must create that state, not
wait for it.** A conditional branch that no test enters will first execute in the session where
someone unrelated makes the condition true, and the failure will be attributed to their change.
This is live and specific right now: the `main.py` catch-all guard and `frontend/dist` are the
concrete instance, and `s-offline-queue` / the Week 3 frontend stories are when the directory
appears.

> **Correction (2026-09-14) — half of this entry's (b) fix does not work, and the test that says it
> does is blind by construction. Logged rather than silently edited, per Entry 6's convention.**
>
> **Who:** `qa`, verifying `t-trip-metadata-endpoint` on a real uvicorn process, against the fix this
> entry records as landed. **Status: open, not resolved** — `ba` is scoping
> `t-405-router-route-collapse`. This note exists because, until then, a reader of Entry 7 would
> reasonably conclude the catch-all problem was fixed and covered. It was not.
>
> The fix had two halves: unknown `GET /api/*` must return a `404` envelope rather than `200
> text/html`, and a *wrong verb on a real API route* must return `405` rather than `404`. The first
> half holds. The second half works for exactly one route in the application —
> `/api/health`, the only one registered directly on `app`. `_methods_allowed_elsewhere` in
> `main.py` skips any route where `not getattr(route, "methods", None)`, and this FastAPI version
> represents an included router as a single lazy `_IncludedRouter` entry with `path=None`,
> `methods=None` and no `.routes` attribute. So **all eight contract endpoints are invisible to the
> scan**: `POST /api/trips/whatever` returns `404`, with no `Allow` header. `qa` confirmed the
> catch-all is the cause rather than Starlette — deleting it makes the same requests return `405`
> correctly, because `_IncludedRouter.matches()` *does* report `Match.PARTIAL` for the wrong verb.
> The information was there; the `getattr` probe just could not reach it.
>
> **Why this matters beyond one header.** Entry 6 is the reason: `405`-collapsed-to-`404` is exactly
> the contract lie that entry refused to accept, and the offline queue branches retry-vs-never-retry
> on `code`. A `404` tells the queue "this resource doesn't exist" when the truth is "this method is
> never accepted." The two new codes were added *specifically* so this would not happen, and at
> runtime it happens anyway on every route that matters.
>
> **The transferable part — this entry's own rule was followed and still produced a blind test.**
> `test_spa_wrong_method_on_a_real_api_route_is_still_405` does create `frontend/dist`, exactly as
> (b) demands. It is green, and it has never once exercised the case it is named for, because it
> asserts against `/api/health` — the single route the broken scan can see. So (b) needs a second
> half: **creating the environment state is necessary and not sufficient; the assertion must also
> target a representative instance of the population the guard claims to cover.** Picking the
> most convenient fixture picked the one route that is structurally unlike all eight real ones. That
> is Entry 2's vacuousness failure mode wearing different clothes — a guard that cannot fail for the
> cases it exists to protect — recurring inside the entry written to prevent its neighbour.
>
> **Do not read this as "remove the catch-all" being pre-approved.** Whether the fix is a
> `route.matches()`-based probe, a rewrite of the scan, or deleting the catch-all outright is open,
> and the first half of the fix (unknown `GET /api/*` not returning the SPA shell) depends on the
> catch-all existing. `s-stop-crud` is the deadline: a `DELETE` on a registered stops path must be
> `405` and will silently stay `404`.

---

## 8. Cross-trip slug collision: the schema permits it, and that is deliberately left alone

> **Scope amended — see "Amendment (2026-09-13)" at the end of this entry before relying on it.**
> This entry covers **one** of two collision shapes. `qa` later found a second, different shape
> (`rider_slug == viewer_slug` on a *single row*) which this entry's reasoning does **not** cover and
> which was resolved the opposite way. Everything below is the original text, unedited.

**Category note — this entry is a different shape from the others.** Entries 1–7 record a
*disagreement* that one side won. This one records a design gap that was **raised and consciously
dismissed**: `ba` found it, argued it needs no fix rather than quietly passing over it, and the
orchestrator agreed. It is here because the gap is real and visible in the schema, so a future
reader will find it independently and re-raise it. Without a record they would either re-derive this
analysis from scratch or, worse, "fix" it — and the obvious fix has a cost (below). A considered
dismissal that leaves no trace is indistinguishable from an oversight.

**Who:** `ba`, raised while scoping `t-slug-access-dependency`; agreed by the orchestrator.
**Where:** `backend/migrations/0001_initial_schema.sql` (the two `UNIQUE` constraints on `trips`),
`backend/app/core/security.py`.

**The observation.** `trips.rider_slug` and `trips.viewer_slug` each carry their own `UNIQUE`
constraint, and the two are **independent**. Nothing in the schema is a cross-column constraint. So
a single string can legally be trip A's `rider_slug` and simultaneously trip B's `viewer_slug`.
Every per-column uniqueness guarantee holds; the pair is still ambiguous. A lookup that matches on
`rider_slug = :slug OR viewer_slug = :slug` could in principle match two different rows.

**Why it is moot — three independent reasons, any one of which would be enough:**

1. **The collision cannot occur.** Slugs are `secrets.token_urlsafe` tokens (spec Section 4,
   restated in Entry 3). This is not "unlikely by convention" like a user-chosen handle; it is the
   same order of improbability as guessing a slug outright, which is the security assumption the
   whole authorization model already rests on. If it *did* happen, the cause would be a broken RNG
   or a duplicated insert — a bug signal, exactly as Entry 3 concluded for the single-column case,
   and not a condition to design around.
2. **The contract says nothing about it.** `docs/api-contract.md` defines behaviour for "the trip
   this slug belongs to." It makes no promise about a slug belonging to two trips, so there is no
   specified behaviour being violated. There is nothing to bring the implementation into line with.
3. **The design is self-consistent regardless — this is the load-bearing one.** `security.py`
   derives `Access` by comparing the presented slug against the **matched row's own columns**, not
   by inferring which branch of the `OR` fired. Whichever single row the query returns, the answer
   it yields is internally coherent: that row's `rider_slug`/`viewer_slug` decide the access level
   for that row. There is no path where a collision produces rider access to a trip whose rider slug
   was never presented. The ambiguity would be "which trip did you mean," never "what may you do."

**Rejected: adding a cross-column constraint.** The obvious remedy is an exclusion constraint or a
shared uniqueness index across both columns (e.g. a lookup table of all slugs, or a functional index
over the pair). Rejected on cost-versus-impossibility: it puts a **second index on the hot slug
lookup path** — the query on the critical path of every single authenticated request in the app —
to rule out a case that cryptographically random tokens already rule out. It also adds a failure
mode at insert time that the seed script would then have to reason about, for a condition that
signals a broken RNG rather than a data conflict worth resolving.

**Why it matters going forward.** If you are reading the schema and notice that the two `UNIQUE`
constraints don't talk to each other: yes, that is true, it was seen, and it is intentional. The
thing that makes it safe is **not** the schema — it is that `security.py` reads access off the
matched row rather than off which `OR` branch matched. That property is what must be preserved. If
someone ever rewrites the lookup to infer access from the query structure (two separate queries, a
`CASE` over the predicate, a returned "matched_on" flag), this dismissal stops being valid and the
gap becomes real.

**~~Outstanding~~ — rationale not yet co-located (Entry 4's rule).** `security.py` is still a stub at
the time of writing, so the "derive access from the matched row's columns, never from which `OR`
branch fired" rationale exists only here. It belongs *beside the lookup*, where the rewrite would
be attempted. `t-slug-access-dependency` should land that comment in `security.py` as part of the
implementation; this entry is then the long form of it.

> **Closed 2026-09-13 — co-located and verified.** Left standing above rather than deleted, per the
> same convention as Entry 6's correction and this entry's amendment: a reader should be able to see
> that the item existed and was discharged, not find a gap where it was. `t-slug-access-dependency`
> landed it. The rationale now lives in `backend/app/core/security.py`, in the docstring of
> `access_for_slug` — which is the function that performs the comparison, i.e. exactly where the
> "just infer access from which `OR` branch matched" rewrite would be attempted. Verified by reading
> the landed code, not assumed from the task being marked done.
>
> The docstring goes further than this item asked for, and the extra part is the half that was still
> missing: it states the **precondition** the Amendment established — that deriving access from the
> matched row is sound *only if that row's two slug columns are distinct* — names
> `trips_slugs_differ_check` as what makes that true, and says plainly what breaks without it
> ("if that constraint is ever dropped, this function starts granting writes through viewer links and
> nothing here will notice"). That closes the loop in both directions: `migrations/0002_trips_slugs_differ_check.sql`
> carries the reason the constraint exists (checked, present), and `security.py` carries the reason it
> may not be dropped. Neither file depends on a reader having found this log first. The Amendment's
> own "Co-location" paragraph is likewise satisfied by that migration's header comment.

### Amendment (2026-09-13) — this entry was narrower than it reads, and the second shape went the other way

**Who:** `qa`, reviewing `t-slug-access-dependency`, against this entry as written. Resolved by the
user, who approved a fix. **Where:** `backend/migrations/0002_*.sql` (new, landing in the current
fix-up), `backend/app/core/security.py`.

**Recorded here as an amendment rather than a new entry because it corrects this entry's *scope*.**
The original text is left standing above, per the same principle as Entry 6's correction: a log that
rewrites itself invisibly is worth less than one that shows where it was too broad and who caught it.
A reader who found only the original would reasonably conclude that "slug collisions were considered
and dismissed," full stop. That conclusion is wrong for half the problem.

**There are two collision shapes, not one.** The original entry addressed only the first:

1. **Cross-trip** — one string is trip A's `rider_slug` and trip B's `viewer_slug`. Two rows.
   **Still waived, for the reasons above, unchanged.**
2. **Same-row** — `rider_slug == viewer_slug` **on a single trip row**. Nothing in the schema
   prevents it: both `UNIQUE` constraints are satisfied, because each column is unique *within its
   own column*, and one row holding the same value in both columns violates neither.
   **Not waived. Constrained.**

**What `qa` demonstrated — empirically, not on taste.** It inserted a trip row with the same string
in both slug columns. The insert **succeeded**. It then resolved that slug through
`access_for_slug`, which returned **`RIDER`** — because the rider comparison is checked first and
matches. So a link handed out as the *viewer* link for that trip silently grants **write** access,
and nothing anywhere reports an anomaly. The viewer link and the rider link are the same string;
there is no observable difference between them for the person holding one.

**Why this entry's two arguments do not transfer:**

- **"The ambiguity would be 'which trip did you mean', never 'what may you do'."** That sentence is
  the load-bearing claim of the original dismissal, and it is *true for the cross-trip shape*. For
  the same-row shape it is **false, and inverted**: there is exactly one trip, so "which trip" is
  never in question — the ambiguity is precisely and only "what may you do." The one case the
  original reasoning declared impossible is the case that actually exists.
- **"Rejected on hot-path cost."** The cross-column index that argument priced is a cost *on every
  authenticated lookup*, and that pricing stands for the cross-trip shape. It does not apply here.
  A `CHECK (rider_slug <> viewer_slug)` is evaluated at **insert/update time only** and costs
  **nothing** on the lookup path. The remedy for shape 2 is not the remedy that was priced and
  rejected for shape 1, so the rejection does not carry over.

Note the third original argument — that a `token_urlsafe` collision is astronomically improbable —
is *also* weaker here. Shape 1 needs two independently generated tokens to collide. Shape 2 needs
only a **single generation bug**: one variable reused, one copy-paste in a seed script, one
`slug = generate()` called once and assigned twice. That is an ordinary programming mistake, not an
RNG failure, and `s-seed-trip-record` is precisely where it would be made.

**Resolution.** The user approved adding `CHECK (rider_slug <> viewer_slug)` to `trips`. It lands as
migration **`0002`** in the current `t-slug-access-dependency` fix-up. The same-row case now fails
**loudly at insert**, which is the Entry 3 principle applied consistently: with random tokens, a
collision is a **bug signal**, and the correct response to a bug signal is to refuse the write, not
to resolve it.

**Which half of Entry 8 still stands.** The cross-trip dismissal, entirely — including its rejection
of a cross-column index on the hot lookup path. Do not add one. What no longer stands is the
*implied generality*: this entry is not authority for waiving slug collisions as a class, and the
sentence about "which trip did you mean" describes shape 1 only.

**Why it matters going forward — the transferable part.** Two cases that share a name were resolved
**differently and for different reasons**: same-row constrained, cross-trip waived. That asymmetry
looks arbitrary from the schema alone — someone reading `0002` may reasonably ask why the CHECK
stops short of covering cross-trip too, and someone reading Entry 8 may reasonably ask why a
dismissed problem grew a constraint. Neither is inconsistent; they are different failure modes with
different costs and different remedies.

More generally: **a dismissal is only as broad as the case it actually analysed.** The original entry
enumerated three independent reasons and was careful about each — and still generalised one shape of
a problem to a name that covered two. When waiving something, state the shape examined, not just the
category. And the specific rule that fell out: the safety of this system rests on
`security.py` deriving access from the matched row's columns, which is sound *only if those columns
are distinct*. The CHECK is what makes that precondition true; it is not redundant belt-and-braces.

**Co-location (Entry 4's rule).** The rationale must sit beside the constraint in `0002`, because
that is where the "this can't happen anyway, drop the CHECK" instinct will strike — most likely from
a future agent who reads the original Entry 8, sees collisions dismissed, and concludes the
constraint contradicts a settled call. This amendment is the long form of that comment.

---

## 9. Seed script: print the slugs before committing, or after? — and a fix-up dispatch that was interrupted mid-run

**Who:** `qa` vs `dev`. Resolved **in `dev`'s favour on the ordering**, in `qa`'s favour on
everything around it. **Where:** `backend/app/data/seed_trip.py` (`_seed_with_new_engine`),
task `t-seed-trip-script`.

Two things are recorded together here because they happened in the same round and the second one is
*why the first one's fix landed in two pieces*. They are otherwise unrelated; skip to "The process
event" if that is what you came for.

### The contested call — commit-then-print vs. print-then-commit

**What `dev` built.** `seed_trip()` inserts the trip, `main()` commits, and only then are the two
slugs printed to stdout. The slugs are the entire access model: `secrets.token_urlsafe` tokens, no
accounts, no rotation, no recovery flow, and this single stdout write is the only copy the operator
will ever be handed.

**`qa`'s challenge, and the part of it that was correct.** `qa` did not merely note the ordering —
it analysed both orderings to their failure modes and found a **real asymmetry**, which was accepted
and is not in dispute:

- **Print-then-commit fails *recoverably*.** If the commit dies after the print, the operator is
  holding slugs for a trip that does not exist. Nothing is lost. `trips` is empty, so the script's
  own already-exists refusal does not trigger, and the fix is to run the command again.
- **Commit-then-print fails *unrecoverably*.** If the print dies after the commit, the trip exists
  in Postgres with two slugs **no human has ever seen** — and the script, seeing a row in `trips`,
  now *refuses to run again*. The operator is locked out of the trip by the very guard that exists
  to protect it.

That is a genuine difference in kind, not degree, and `qa` was right to press on it.

**Why print-first lost anyway.** The two failures are not comparable on severity alone, because they
are different *kinds* of wrong. Commit-then-print's bad outcome is an **operational** one: a trip
exists that the operator can't reach through the script. Print-then-commit's bad outcome is that the
script **hands out credentials for a row that was never written** — it tells the operator "here are
the rider and viewer links for your trip" when there is no trip. That is a security-shaped lie: the
output's entire contract is "this is authoritative, save it, it is the only copy," and print-first
makes that statement conditionally false in a way the operator cannot detect from the output itself.
An operator who saves those slugs and closes the terminal believes they hold a working trip.

The decisive point is that the unrecoverable case is only unrecoverable **from inside the script**.
A `SELECT rider_slug, viewer_slug FROM trips` reads them straight back out. So the asymmetry `qa`
identified is real but its severity depends on something fixable — whether the operator knows that
query exists. Print-first's failure has no equivalent mitigation, because a row that was never
committed cannot be read back by anything.

**Resolution — the ordering was kept and the window was narrowed instead.** Commit-before-print
stands. What changed is everything that made it dangerous:

1. `emit()` moved **inside** the `try`, ahead of engine disposal. The window between commit and
   print previously contained `engine.dispose()` — several milliseconds of real network I/O on
   connections that may already be dead or idle-timed-out by Neon. A raising `dispose()` would have
   propagated *instead of* the result: trip committed, slugs never printed, operator shown an
   `OSError` about a socket.
2. Disposal moved to a `finally` wrapped in `suppress(Exception)`, so it can no longer replace a
   return value or an in-flight error. By then the work is either committed and printed or already
   propagating a better error; whatever is still open is closed by the process exiting.
3. The **documented recovery `SELECT`** is what makes the residual risk survivable, and it is
   printed on both paths that leave an operator without slugs in front of them — the already-exists
   refusal and a failed stdout write (which exits 1 with the query on stderr). The refusal also
   explicitly warns *against* DELETE-and-reseed, because an operator who believes the slugs are
   irrecoverable will reach for exactly that, destroying the trip and everything scoped to it.

**Why it matters going forward.** If you are reading `_seed_with_new_engine` and the commit-then-
print ordering looks like an oversight: it isn't, it was challenged on exactly these grounds, and
the challenge was answered by shrinking the window rather than by reversing the order. **Do not
reorder it.** The thing that must be preserved is not the order by itself — it is the pairing: the
order is only defensible while (a) nothing that can perform I/O or raise sits between the commit and
the print, and (b) the recovery query is actually told to the operator on every path that loses the
output. Delete either and the ordering becomes the bad call `qa` said it was. The rationale is
co-located in that function's docstring and in the comment above `return emit(result)`, which is
where the reversal would be attempted; this entry is the long form of those comments.

Noted alongside, because it constrains any future rewrite of the error paths: `dev` confirmed
empirically that the bound-parameter leak is **not** limited to `IntegrityError`. An
undefined-column INSERT renders **both slugs into the raw exception string** on the generic
`SQLAlchemyError` branch. That is why no branch in this module interpolates a driver exception into
a message, and why the generic branch reports SQLSTATE rather than `str(exc)`.

### The process event — a fix-up dispatch that landed half its work

**Who:** `dev` (second run) vs. the task description it was handed. Resolved in `dev`'s favour.

The first fix-up dispatch was **interrupted mid-run**. It had applied the six source changes to
`seed_trip.py` but had not written the corresponding tests. The next `dev` run received a task
describing work as outstanding that was, in the file, already done — and noticed, because the task's
cited line numbers did not match the file in front of it.

**What it did instead of either obvious thing.** It did not assume the task was right and redo the
edits (which would have double-applied or conflicted). It did not assume the task was stale and
declare the work complete. It **verified each of the six fixes by behaviour rather than by reading
the code**, made no edits to the source at all, and wrote only the genuinely missing piece — the
tests. It then demonstrated the test suite was complete rather than merely passing, by **reverting
each of the six fixes in turn and confirming each mapped to exactly one failing test.**

**Why it matters going forward.** This is the general rule, and it is not specific to seeding:
**when a task's premise does not match the file in front of you, the premise is the thing to check
first — empirically, then report the discrepancy.** Both default reactions are wrong in the same
way: "the task must be right, redo it" and "the file must be right, it's done" each resolve the
contradiction by *assuming* one side, and an interrupted run is exactly the situation where neither
side is trustworthy. Read the current state, prove it by behaviour, and hand the discrepancy back.

Note also the verification technique, which is worth reusing wherever a fix-up lands tests after
source: reverting each fix individually and requiring exactly one test to fail is a *completeness*
check on the test set, not a vacuousness check on individual tests. It answers "is there a fix here
that nothing would catch," which is the question Entry 7(a) showed mutation testing structurally
cannot answer about paths you did not think of — here the set of paths was enumerated externally, by
`qa`'s findings list, rather than by the author's own recollection.

### Status footnote, because it is easy to misread

`t-seed-trip-script` is **done**; story `s-seed-trip-record` is **not**. The script is built and
`trips` still holds **0 rows** — the one-time seed is deliberately unconsumed. Running it is a human
step: it prints permanent, unrotatable credentials, and no agent should run it because that puts
live slugs into an agent transcript (the module's own rule 1 — no logs, no files — exists for the
same reason and would be defeated by it). The story closes when the trip actually exists.

---

## 10. A shallow copy reasoned about as if it were deep — in the implementation's justification, then in the test meant to check it

**Who:** `qa` vs `dev` (a), then `test-writer` vs the orchestrator (b). Both resolved against the
party that proposed the reasoning; neither changed the shipped behaviour.
**Where:** `backend/app/main.py` (`_methods_allowed_elsewhere`),
`backend/tests/test_error_envelope.py`, task `t-405-router-route-collapse`.

**Why these are one entry and not two.** They are the same mistake in two places. A shallow copy was
reasoned about as though it were deep — once in the justification for the production code, once in
the test written to cover the gap that justification left. Filing them separately would hide the
thing worth noticing: the patch contained the error, the review of the patch reproduced it, and only
the second catch was made before it landed. This file already carries Entry 2, a test that could not
fail. This is that failure mode caught **twice in a single patch**.

### (a) `qa` disproved `dev`'s stated reason for copying the request scope

**`dev`'s position.** The probe loop calls `route.matches()` against `{**request.scope, "method": verb}`
rather than the live scope. `dev`'s docstring justified the copy on the grounds that
`_IncludedRouter.matches` inserts FastAPI's bookkeeping `"fastapi"` key into whatever scope it is
handed **and does not take it out again** — so probing the live scope would leave that key behind as
a side effect of answering a 405.

**What `qa` measured.** That claim is testable at the only moment that matters — when the handler
actually runs — so `qa` measured it on the live app rather than in a constructed scope. The
`"fastapi"` key is **always already present** by then. Starlette's router calls
`scope.setdefault("fastapi", {})` on the **live** scope during routing, before the handler is
reached. So `{**request.scope, ...}` is a *shallow* copy that **shares that inner dict** with the
live scope: `inner_dict_same_object = True`, with 104 probe writes observed landing on the live
object.

The code was correct. The reason was wrong — and wrong **precisely in the case that occurs at
runtime**, rather than the one a unit test constructs. A test that builds a scope by hand without
the key exercises `dev`'s stated mechanism; no real request ever does.

**The true reason the copy is needed**, which is what must survive in the docstring:

- `_IncludedRouter.matches` brackets each bookkeeping write in `try`/`finally`, and the probe loop is
  synchronous, so the intermediate state is never observable by another task. The key is not the
  problem.
- The copy's one real job is keeping the probe's **`method` overwrite** off the live scope. **uvicorn
  reads `scope["method"]` back at response-send time** — for the access log, and to decide whether a
  response may legally carry a body. A probe that left `method` as the last verb tried would
  mis-log the request and could strip or admit a body against the real method.

### (b) `test-writer` disproved the orchestrator's fix for the resulting test gap

**The orchestrator's position.** Given (a), the test scope should be seeded with a real `"fastapi"`
key so the suite exercises the key-*present* path that production actually takes.

**Why that alone would have produced a test with no coverage.** `test-writer` caught it before it
landed. The test snapshots with `before = dict(scope)` — a **shallow** snapshot. Seed a `"fastapi"`
key and `before["fastapi"] is scope["fastapi"]`, so the final `scope == before` equality compares the
inner dict **against itself**. It cannot observe a probe write to that dict no matter what the code
under test does. The proposed change would have *looked* like new coverage of the newly-understood
path and provided none.

**Resolution.** `test-writer` added a second, inner snapshot. Measured with the restore disabled:
shallow snapshot → `scope == before` is `True` (blind); corrected → `False` (catches). Verified
against the real test by monkeypatching `fastapi.routing._restore_fastapi_scope_key` to a no-op —
i.e. the test was proven able to fail before being trusted, per Entry 2.

**Why it matters going forward.**

**(a) A justification is a factual claim and can be checked independently of the code it justifies.**
`dev`'s code passed every test and shipped unchanged; only its stated reason was false. That is not
harmless — the docstring is what a future reader consults before deciding the copy is redundant, and
a reader who tests `dev`'s stated mechanism will find it does not occur, conclude the copy is
unnecessary, and remove it. The false reason is more dangerous than no reason. **When code is
correct for a reason other than the one written beside it, fix the reason.** The corrected rationale
belongs in `_methods_allowed_elsewhere`'s docstring, per Entry 4 — beside the code, not only here.

**(b) `dict(...)` and `{**...}` are one level deep, and this is the second time in one patch that
was forgotten.** Any snapshot-and-compare test over a nested structure is vacuous at every level
below the first. The rule that generalises: **when a test asserts "X was not mutated," confirm the
snapshot is independent of X at the depth the mutation would occur** — and prove it by disabling the
restore, which is exactly what turned a `True` into a `False` here.

**(c) The uncovered half is uncoverable, and that is a finding, not a gap to fill.** The "no other
task observes the intermediate state" property rests on the loop being synchronous. No
post-condition test can check it: a state restored before the call returns is invisible to an
assertion made after it. It is a code-review invariant — there is no `await` in that function — and
it is recorded in `docs/progress-notes.md` under `t-405-router-route-collapse` together with the
deep-copy proposal it would otherwise keep reviving. If that proposal is revived it must be a
one-level `dict()` copy and never `copy.deepcopy`: the scope holds `_IncludedRouter` instances and
route contexts FastAPI reads back **by identity**, so a deep copy would clone live routing objects.

---

## 11. A decision the user made could not be implemented as spelled — `methods=["GET", "HEAD"]`

**Who:** `dev` vs. a mechanism **the user chose**, contradicted on measured evidence. Resolved in
`dev`'s favour on the spelling, in the user's favour on everything the spelling was for.
**Where:** `backend/app/main.py`, `backend/app/api/routes/trips.py`,
`backend/tests/test_head_method.py`, task `t-head-on-get-routes`.

**Category note.** Entries 1–10 record agents disagreeing with each other. This one records an agent
contradicting **the user's own decision**, which is why it is worth more than the two-line diff it
produced: the finding was not "there is a better way", it was "the way you picked does not exist on
this FastAPI version". The distinction matters for how it was handled — `dev` did not substitute its
own preference, it preserved the decision and changed only what the decision was expressed in.

**The decision.** Offered a global HEAD→GET middleware or an explicit `methods=["GET", "HEAD"]` on
each GET route, the user chose the explicit method list. The middleware was rejected for reasons
already on file: Entry 7b had rejected routing-duplicating middleware (a drifting second copy of the
routing table), and stripping the body from a HEAD response is the ASGI server's job, not the
application's. **That rejection is untouched by this entry. Do not re-propose the middleware.**

**What `dev` measured.** `fastapi.openapi.utils.get_openapi_path` loops `for method in
route.methods` with **no HEAD exclusion**, while `operation_id` is per-*route*, not per-operation.
So a single route carrying both verbs emits a second `head:` operation into the OpenAPI document,
with **both operations sharing `operationId: health_api_health_get`**, plus a `Duplicate Operation
ID` warning at generation time. The document is what Kubb generates the frontend client from, so the
literal spelling of the user's choice would have produced a duplicate, identical hook in
`frontend/src/api/` — a repository away from the change that caused it. Measured on FastAPI 0.141.1.

Per the task's own instruction (Entry 6b — a gap left visible beats a gap papered over), this was a
stop-and-escalate condition rather than something to improvise around.

**What shipped instead.** A second registration of the **same handler** on the same path:
`add_api_route(..., methods=["HEAD"], include_in_schema=False)` in `main.py` for `/api/health`, and
the `router.add_api_route` equivalent in `trips.py` for `GET /trips/{slug}`.

**Why this is not a third option smuggled past the decision.** The path's route set is `GET` +
`HEAD` — which is exactly what `_methods_allowed_elsewhere` probes and reports in `Allow`, now
`{"GET", "HEAD"}` on both paths. It is still a **route-level declaration**: no second copy of the
routing table, nothing inspecting requests ahead of routing, body-stripping still uvicorn's job.
Because the registration reuses the handler object, the access dependency is not re-declared and
cannot drift: `qa` confirmed the GET and HEAD routes resolve **identical dependency trees**, that
`require_trip_access` genuinely runs on HEAD, and that an unknown slug is a `404` rather than a
`403`, so the slug-space oracle stays closed. **The user's intent survived; only the literal
spelling did not.**

**Why it matters going forward — the part worth recording.** `qa` ran the collapse as a mutation:
folding the two registrations back into one `methods=["GET", "HEAD"]` route leaves **189 of the 191
tests passing**. Every behavioural test stays green, because nothing about request handling changes
— HEAD is still answered, by the same handler, with the same status and no body. Only the two
OpenAPI guards in `test_head_method.py` fail
(`test_openapi_document_declares_no_head_operation` and
`test_generating_the_openapi_document_emits_no_duplicate_operation_id_warning`).

So the "obvious simplification" — and it *is* obvious; two registrations of one handler reads like
redundancy — is **invisible to every test that sends a request**, and its damage lands in a
*generated client*, not in the API. That is why those two guards exist, and why the inline comments
above both registrations name `get_openapi_path` explicitly rather than saying "don't merge these"
(Entry 4's rule: the rationale belongs where the mistake would be made). **A reader who deletes
either guard is removing the only thing standing between that simplification and a duplicate Kubb
hook.** If you are about to merge the registrations, you are reopening this entry, and the burden is
to show `get_openapi_path` no longer loops HEAD in — not to re-derive it.

---

## 12. A discovered issue is not automatically work — the triage gate, and who is allowed to write the files that carry it

**Who:** the user vs. the orchestrator, on two connected points in the same sitting. Resolved in the
user's favour on both. **Where:** `docs/finding-triage-gate.md` (new, canonical), and — via tasks
`t-claude-md-triage-gate` and `t-agent-defs-triage-gate` — `CLAUDE.md` and `.claude/agents/`.
Story `s-finding-triage-gate`.

**Category note.** Like Entry 11, this records the user contradicting an agent rather than agents
contradicting each other — but it is a *process* call, not a code one, which is why it has no file in
`backend/` to co-locate against. Its co-location targets are the governance files themselves, and
that is exactly what made point (b) necessary.

### (a) The triage rule — a weaker version was proposed and replaced

**The orchestrator's position.** Faced with a backlog growing faster than the feature work, the
orchestrator proposed a three-question triage, stated here in its own terms rather than as a straw
man, because it is a reasonable rule and that is the point:

1. Is something spec Section 12 ranks top priority — access control, data integrity, the offline
   queue — *currently* wrong?
2. Does a client that exists today observe the behaviour?
3. Otherwise it is debt.

Plus a hard stop: a finding discovered while doing task X does not become task Y in the same sitting.

That rule is not wrong. Everything in it survives into the final version. It was replaced because of
what it left to judgment.

**What the user's version adds, and why each addition is load-bearing:**

- **A Stop Condition that names the specific behaviours** constituting over-investigation — do not
  modify production code, add speculative tests, refactor surrounding code, redesign the affected
  behaviour, investigate hypothetical consumers, or explore unrelated edge cases. The orchestrator's
  version said "don't turn it into task Y" and trusted judgment for the rest. Judgment is precisely
  what fails here: nobody ever decides to over-investigate. They decide to "just check one thing,"
  six times. An enumerated prohibition can be noticed being violated; a disposition cannot.
- **QA classification as evidence, not authority, with an explicit disagreement protocol**
  (`QA classification:` / `Implementation classification:` / `Reason for disagreement:` /
  `Evidence:`). Without the protocol there are only two available failure modes, and this log
  already contains both: QA's word taken as final, or QA's word silently re-triaged by whoever
  disagreed. Entry 7 exists because `qa` was right and `dev`'s conclusion was wrong; Entry 11 exists
  because `dev` was right and contradicted a decision from above. Both directions happen. The
  protocol makes the disagreement a visible artifact instead of a fork in someone's private
  reasoning.
- **Gate 1 broadened past spec Section 12's three categories** to *any* explicit specification
  requirement, invariant, security/access-control rule, data-integrity requirement, or existing
  behavioural contract. The narrow version would have mis-classified real defects this project
  already hit: Entry 7's `INTERNAL_ERROR` leak is a contract guarantee, not one of the three
  categories; Entry 6's `405`-collapsed-to-`404` is a contract lie whose damage only *reaches* the
  offline queue. A gate whose first question can be answered "not one of the three, so it's debt"
  about a live contract violation is a gate that files real breakage.
- **The four `≠` distinctions stated as rules** — `Could happen ≠ Does happen`,
  `Could break ≠ Is broken`, `Could consume ≠ Currently consumes`, `Future risk ≠ Current defect`.
  They were implicit in the three questions. Implicit is not enough, because the slide from "could"
  to "does" happens inside a sentence, usually in the sentence justifying the work. Written down,
  they are something a reviewer can point at.

**The losing reasoning is the valuable part, and it is not the three questions.** It is the instinct
the three questions were too weak to stop, and it must be recorded verbatim because it is the
default an orchestrator reaches for *every single time*:

> "It's a small follow-up, just build it now."

Every instance of it is locally reasonable. The finding is real; the fix is small; the context is
already loaded; dispatching a separate task later costs more than doing it now. None of that is
false. The failure is only visible in aggregate, which is why it must be written down rather than
re-decided case by case — **without the rejected version on file, the gate erodes one
reasonable-looking exception at a time**, and each exception is defensible in isolation while the sum
of them is the backlog.

**The evidence that decided it, measured against `docs/progress.json` as it stood:**

- **13 of the 15 open tasks sat in `s-data-layer-foundation`** — a story whose own note describes it
  as a "shared prerequisite" for the endpoint stories — while **four `m2-core-api` endpoint stories
  (`s-stop-crud`, `s-photo-upload-onedrive-sync`, `s-bike-management`, `s-map-geojson-endpoint`)
  were still `not_started`**. The foundation story was not being finished; it was accumulating
  everything noticed while passing by.
- **`t-head-on-get-routes` consumed a full `dev` → `test-writer` → `qa` → `docs` cycle for behaviour
  no client exercises.** It is good work — Entry 11 records a genuine FastAPI finding that came out
  of it — and there is no frontend yet, no client sending HEAD to anything, and no contract
  requirement that was being violated. Under the gate it is Gate 3 at best: file it with the trigger
  "when a client issues HEAD," and build a stops endpoint instead. It also spawned two further tasks
  (`t-head-route-kwarg-divergence`, `t-add-endpoint-head-convention`), which is the shape of the
  problem in miniature — an ungated finding is not one task, it is a tree.

**Resolution.** The user's text is canonical and lands verbatim at `docs/finding-triage-gate.md`.
It is fixed text: agents transcribe and cite it, they do not redraft it.

**Why it matters going forward.** When you are about to argue that a specific finding is an
exception, you are reopening this entry, and per this file's rule the burden is on you to show the
evidence above no longer applies — not to re-derive it. The specific counter you will want to make
("but this one really is small") is the one that was rejected; smallness was never the disputed
claim. The disputed claim is that smallness is a reason to skip the gate.

### (b) Governance-file ownership — no agent could write the files that carry the rules

**The problem, which is structural rather than a disagreement about taste.** The roster has no agent
that can write `CLAUDE.md`, `.claude/agents/` or `.claude/skills/`. `docs` is `docs/`-only. `dev`
writes application code. `devops` is `Dockerfile`, `docker-compose.yml` and `infra/`. `ba` is
read-only. So the files that define how every agent behaves are writable by nobody in the system that
follows them.

**This was not theoretical — it blocked three separate pieces of work:**

1. Restoring the agent definitions.
2. `t-add-endpoint-head-convention` — a **one-line** edit to `.claude/skills/add-endpoint/SKILL.md`,
   recorded with `agent: "docs"`, which `docs` correctly refused as outside its write scope and
   flagged back rather than attempting (see its note in `progress-notes.md`). The task has sat
   blocked since, with the blocker text "Needs reassignment" and nobody who could be reassigned to.
3. This gate — a rule about how agents work, which by construction has to reach `CLAUDE.md` and the
   agent definitions to have any effect.

**Resolution.** The **orchestrator is the recorded owner** of `CLAUDE.md`, `.claude/agents/` and
`.claude/skills/`.

**Rejected: widen the `docs` agent's write scope to `.claude/**`.** This is the smallest-looking
change — `docs` already writes prose, and agent definitions are largely prose. It was rejected
because of the part that is not prose: an agent definition file contains that agent's own `tools:`
grant and its peers'. Letting `docs` write `.claude/agents/` means an agent can edit the file that
constrains it, and the files that constrain the agents reviewing it. That is **self-modifying
permissions**, and it is a category difference from writing documentation, not a difference of
degree — the failure mode is not a bad sentence, it is a constraint that quietly stops existing with
no diff anyone thought to review as a permissions change.

**Rejected: a dedicated `governance` agent.** It answers the scope objection by giving the job to
something whose *purpose* is those files. Rejected on two counts. It is a whole agent definition —
prompt, tool grants, a row in the roster, a thing every future agent reads past — for a handful of
edits a month. And it hits the same objection one step removed: a `governance` agent can write
`.claude/agents/`, therefore it can write **its own** definition and every other agent's `tools:`
grant. The self-modification is not removed, only given a more official-sounding name.

**What survives the change, and it is the part that matters.** The pipeline's real guarantee is not
"a subagent's hands were on the keyboard." It is that **`ba` scopes the work as a task and the user
reviews the resulting patch**. Both hold here: `s-finding-triage-gate` is scoped as four sequenced
tasks, each produces one reviewable patch, and the user reviews each before the next starts. Only the
*writing actor* changes. The orchestrator is also the one party the roster was never designed to
constrain via tool grants, because it is the party dispatching them — so recording ownership there
adds no permission that did not already exist; it names one that was unallocated.

**And this shape is already a carve-out in `CLAUDE.md`.** Session 0 is documented as the sole
exception to the pipeline, on the grounds that its output *is* the agent definitions, so nothing
existed yet to dispatch to. That reason has expired — the agents exist now. It is replaced here by a
durable one: agent definitions and the rules they follow are the one category of file where
delegating the write would mean delegating the permission boundary itself. Session 0's exception was
about bootstrapping; this one is about not handing an agent the pen that writes its own limits.

**Why it matters going forward.** If you are looking at `CLAUDE.md` or a file under `.claude/` and
reaching for the obvious fix — widen `docs` by one glob — that is the rejected option, and the
objection is not "scope creep," it is that the widened agent can then rewrite tool grants. Two tasks
carry this entry's reasoning to where the mistake would be made (Entry 4's rule): the roster table in
`CLAUDE.md` must state who owns those paths, and `t-add-endpoint-head-convention` must be reassigned
rather than left blocked on an agent that structurally cannot execute it.

---

## 13. The one backlog item the gate promoted — Gate 2, against "the slug is already in the access log"

**Who:** `ba` (applying the gate in the retro-triage) vs. the finding's own recorded argument, written
by `qa` when it filed the item. Resolved in `ba`'s favour, with an addition from the orchestrator that
the user accepted. **Where:** `backend/app/core/errors.py` (`unhandled_exception_handler`, line 241;
the `logger.exception` call at 248), task `t-error-log-parameter-redaction`, story
`s-data-layer-foundation`. Produced by `t-backlog-retro-triage`.

**Context.** `t-backlog-retro-triage` classified all 15 open tasks against
`docs/finding-triage-gate.md`. Fourteen were uncontested — 3 ORDINARY DEBT, 11 TRIGGERED DEBT. This
is the fifteenth, and the only one where two readings of the same evidence gave different gates.

### The classification (`ba`, and what carried it)

`unhandled_exception_handler` logs via `logger.exception`, so a `DBAPIError` traceback carries
`parameters: ('<live-slug>', ...)`. A trip slug is not an identifier in this system — it *is* the
credential, the whole of the authorization for a link.

Three facts decided it, and the first is the one that matters:

1. **`qa` observed the rendered traceback.** It did not reason from the presence of `logger.exception`
   to what the output would contain — it read the output. That is the difference between evidence and
   a plausible inference, and the gate's Evidence Requirement asks for exactly the former.
2. **Both the handler and a route that reaches it are registered and run today.** This is not a
   dormant code path awaiting a future endpoint. `GET /api/trips/{slug}` is live, the handler is
   wired to `Exception`, and any unhandled `DBAPIError` on that path renders the slug.
3. **Gate 2 asks whether a current runtime path exercises the behaviour — not how bad it is.**
   Nothing in the gate's text weighs consequence. `CURRENTLY OBSERVABLE → FIX NOW` is satisfied by
   the existence of the consumer.

### Against it — the note's own argument, which is not weak

The finding as filed argued for ORDINARY DEBT, and stated its own reasoning plainly enough that it
must be recorded rather than summarised away:

> The slug is already in the uvicorn access log by virtue of being in the URL, so this is a duplicate
> rather than a new exposure class — but it is the copy most likely to reach a third-party error
> tracker.

Taken seriously, that is a real argument on two legs. **Marginal disclosure is zero:** the slug is
already written to disk on every request by the access log, so redacting the traceback removes the
second copy of something the first copy already exposed. And **the named harm is conditional:**
"reaching a third-party error tracker" requires a log sink that does not exist in this project — no
Sentry, no shipper, no aggregator. Under that reading the correct classification is ORDINARY DEBT
with the promotion trigger "first error tracker or log shipper configured," and the item waits.

### Resolution, and precisely where the losing argument fails

**CURRENTLY OBSERVABLE (Gate 2). FIX NOW.**

The losing argument is not wrong about the facts — it is wrong about which question the gate asks.
Every one of its claims is a claim about **severity**: how much worse the second copy makes things,
and how likely the bad outcome is. The gate's first two questions are about **currency**: is a
requirement violated now, does a path exercise it now. Severity belongs in *ordering* — what gets
built first once several things are promoted — not in *classification*. Admitting severity into
classification is what makes a gate erodible, because severity is always arguable and "this one is
minor" is available for every finding.

Note the shape of the losing move, because it is the inverse of Entry 12's: Entry 12 records the
instinct to **build** something the gate would file ("it's a small follow-up, just build it now").
This is the instinct to **file** something the gate would promote, on the same underlying reasoning —
that smallness is a classification input. It is the same error with the sign flipped, which is why
both belong in this log rather than only the one that costs work.

### The orchestrator's addition — accepted, and it changes the scope, not the gate

The task as written closes **one of two copies**. Fixing `logger.exception` leaves the slug in the
uvicorn access log, so the task does not close the exposure its own title names. This was raised by
the orchestrator and accepted by the user.

**It is re-scoped: cover both sinks, or explicitly decide the access log is acceptable and say why.**

Recorded deliberately as a **change to the task's scope, not to its classification.** The two are
easy to conflate — "it doesn't fully fix the problem" reads like an argument that the problem is less
real — and conflating them here would hand the losing argument a second route in. The gate is Gate 2
either way. What changed is what "done" means for the task.

Note also that the re-scope is what turns the losing argument into useful input rather than a
discarded one: "the slug is already in the access log" was not a good reason to file the item, but it
is exactly the right description of the second sink. The argument was mis-aimed, not mistaken.

### Why it matters going forward

**This item was classified FIX NOW and deliberately not fixed in the same patch.** The retro-triage's
Stop Condition binds its own output, so `t-backlog-retro-triage` recorded the promotion and stopped.
That is not an exception to the gate and does not reopen Entry 12 — a retro-triage that ended with
three fixes would have demonstrated the gate does not bind its own author, and the evidence for a
Gate 2 call should survive being written down and reviewed before anyone edits production code on the
strength of it.

**Co-location is still owed (Entry 4's rule).** The rejected option would be retried in
`backend/app/core/errors.py` — a future reader looking at a redaction guard in
`unhandled_exception_handler` will reasonably think "the slug is in the access log anyway, this is
pointless" and remove it. That reasoning needs to sit beside the code, and
`backend/` is outside the `docs` agent's `docs/`-only write scope, so it could not be placed by the
patch that recorded this entry. **Whoever implements the fix carries it into that file.** It is part
of the work, not tidy-up.

---

## 14. A client-generated id that already exists under a *different* trip — the sixth `ErrorCode`

**Who:** `ba` vs. the API contract as written. Escalated to the user, who decided. **This is Entry 6
happening a second time**, on the same contract, by the same route, and `ba` applied Entry 6's own
rule to get there.
**Where:** `docs/api-contract.md` §"Idempotency" and §"Error envelope",
`backend/app/models/common.py`, `backend/app/core/errors.py`,
`backend/app/data/repositories/stops.py`. Found while scoping `s-stop-crud`'s create endpoint.

**The gap.** The contract's idempotency rule said: *"Id that already exists → return the existing
record with `200`. Do not create a duplicate. Do not return a conflict error."* That sentence was
written with one scenario in mind — the offline queue resending a request whose response never
arrived — and it is correct for that scenario.

But `stops.id` is a **global primary key** with `trip_id` as a *separate* foreign key
(`backend/app/data/tables.py:72-73`); `photos` (`:101-102`) and `bikes` (`:115-116`) have exactly
the same shape. So an id is unique across the whole table while the parent it belongs to is a
different column, and the contract's "id that already exists" silently conflates two different
facts: *this id exists here* (a replay) and *this id exists somewhere else* (not a replay at all).
For the second case the contract had **no answer**, while appearing to have one — and the answer it
appeared to give, `200` with the existing record, is the worst available option, because the
existing record belongs to a trip the caller has no link to.

**What `ba` did.** It escalated rather than inventing an answer, citing Entry 6: a new `ErrorCode`
member is a *contract change*, and a scoping agent does not make contract changes by writing them
into a task description. It did not quietly pick `422`, and it did not scope the create endpoint
with the ambiguity left in.

**Resolution — the user ruled.** Add a sixth member, **`CONFLICT`, status `409`**. The idempotency
rule becomes a three-way branch, stated once for all three create endpoints:

- id unseen → `201`, record created;
- id already exists **under this same parent** → `200` replay with the stored record;
- id exists **under a different parent** → `409` / `CONFLICT`, nothing created.

Two mechanics were fixed at the same time, because the branch is only safe with both:

1. **Replay is matched on `(trip_id, id)`** — `(stop_id, id)` for photos — never on `id` alone.
2. **The cross-parent case is detected by an explicit check, never by letting the `INSERT` fail.**

### The four rejected readings

**1. Look up by `id` alone.** The most natural reading of the sentence as written, and the reason
the gap is dangerous rather than merely incomplete. It returns another trip's stop *through this
trip's slug* — a cross-trip leak, spec §12's top-priority failure class. The slug is the entire
access model; a lookup that ignores the parent hands out a row the caller's slug does not authorise.
Rejected outright, on correctness.

**2. Look up by `(trip_id, id)` and let the `INSERT` fail.** Correct lookup, wrong failure
mechanism. The driver's primary-key violation is an unhandled exception, which renders
`500` / `INTERNAL_ERROR`. The offline queue branches on `code`: `INTERNAL_ERROR` means "the server
broke, retry later", so the queue would **retry forever a request that can never succeed** — the id
belongs to another trip on this attempt and on every future one. Entry 6 calls this class of thing a
contract lie: a status that misreports which side is broken does not stay cosmetic, because
something downstream is making a decision from it.

**3. Reuse `VALIDATION_ERROR` (422).** The strongest of the rejected options, and the one that
*almost* works: the queue treats `422` as never-retry, which is the correct retry behaviour here.
It lost on honesty. The request is well-formed — every field validates, the types are right, the
body is exactly what the schema asks for. Labelling it a validation failure tells the rider and the
client that they sent something malformed when they did not. This project has been burned
repeatedly by statuses that misreport what happened (Entry 6's `405`-as-`404`, Entry 7's collapsed
405); getting the right retry behaviour from a wrong label is a coincidence, not a design, and it
breaks the moment anything branches on `code` for a reason other than retry.

**4. A composite `(trip_id, id)` primary key.** ***Record this one most carefully — it is the option
a future reader will rediscover, and it is not wrong.***

The argument for it is genuinely good. Make the primary key the pair, and the same `id` under two
different trips becomes **legal**. There is then no collision, no third branch, no sixth error code
and no check to forget: the `409` becomes *unreachable by construction*, which is a stronger
guarantee than any handler discipline. And the migration is **free today** — `stops`, `photos` and
`bikes` are all empty, so there is no data to rewrite and no backfill to get wrong. Compared against
"add a code, then rely on every future create handler remembering to check", it is the structurally
safer design.

**It was rejected on cost, not on correctness.** The change does not stop at `stops`.
`photos.stop_id` is a foreign key to `stops.id` (`tables.py:102`), so widening `stops`' primary key
to `(trip_id, id)` forces `photos` to carry `trip_id` as well and reference the pair — which drags
in the photo-upload design that **has not been built yet** (`s-photo-upload-onedrive-sync`:
multipart upload, object keys, the OneDrive sync's own identifiers). The choice was between a
schema change whose blast radius reaches into an unbuilt feature, and a contract change confined to
one enum member, one status mapping and one repository check. The user took the second.

**What this means if you are reopening it:** the burden is not to show the composite key is *good* —
that was granted. It is to show the photos cascade is now cheap, which means `s-photo-upload-onedrive-sync`
has landed and its key design is settled, or that it has not started and can absorb the change in its
own design. Between those two states, the answer stays no.

### Entry 3 does not forbid `ON CONFLICT (id) DO NOTHING` here

Recorded pre-emptively, because a reader who greps this log for `ON CONFLICT` will land on Entry 3
and reasonably conclude the construct is banned project-wide. **It is not, and Entry 3 does not
reach this case.**

Entry 3 rejected `INSERT ... ON CONFLICT (slug) DO UPDATE` on the **trips** table. Its objection was
specific and it does not transfer on any of its three legs: it was about `DO UPDATE` (which silently
*overwrites*), on a **slug** column (which is a credential, so overwriting one re-points a live
link — an authorization leak), where a collision is a **bug signal** from a broken RNG rather than a
routine condition. Here the statement is `DO NOTHING`, the column is a client-generated `id` on
`stops`, nothing is overwritten, no credential is involved, and a collision is an ordinary condition
the offline queue is *expected* to produce. Entry 3's actual rule — "fail, don't reconcile, when the
colliding column is a credential" — is untouched.

What still rules `ON CONFLICT` out as the *primary* mechanism here is rejected reading 2, not Entry
3: whatever the statement, the cross-parent case must be decided by a check whose outcome the
handler can see, because `DO NOTHING` returning zero rows cannot by itself distinguish "same parent,
replay, return the stored row" from "different parent, `409`".

### Why it matters going forward

**(a) Entry 6's rule held under repetition, which is the only real test of it.** The first
escalation could have been a one-off. This one came from the same agent, on the same contract,
against a deadline with `dev` already working in `backend/` — and `ba` still declined to decide,
and still refused to quietly reuse an existing code to make the gap disappear. The rule is: a
scoping agent that finds the contract inconsistent **stops and escalates**, it does not choose.

**(b) The build-order gate is what made this cheap.** The contract change was written and reviewed
*before* `dev` opened `common.py` for the create endpoint. Had the gap been found during
implementation, the sixth code would have arrived as an improvised handler decision inside an
endpoint patch, with no record of why `422` was not used.

**(c) "Do not return a conflict error" was a true sentence that became false.** Nothing about it was
careless when written — it was written about the replay scenario, and it is still right about that
scenario. It went wrong by being stated more broadly than the case it was reasoned about. Worth
noticing as a failure mode of contract prose in general: the sentence did not need to be *wrong* to
be dangerous, it only needed to be *unqualified*.

**Co-location owed (Entry 4's rule).** Three sites, all outside the `docs` agent's `docs/`-only
scope, so none could be placed by the patch that recorded this entry:

- `backend/app/models/common.py` — beside the new `CONFLICT` member: why it is not `VALIDATION_ERROR`
  (reading 3), and that it is never-retry for the queue.
- `backend/app/core/errors.py` — beside `_STATUS_TO_CODE`/`ApiError`: note that `409` is endpoint
  contract rather than framework-level. Adding the mapping row also makes `ApiError(409, CONFLICT, …)`
  constructible, which is the intended effect. `__init__`'s status/code backstop is a **`ValueError`,
  deliberately not an `assert`** — `assert` is stripped under `python -O`, which would silently
  disable the check in exactly the optimised build where a contradictory body goes unnoticed. (Entry
  7's body describes it as "an `__init__` assertion"; the code and the `t-error-envelope-handlers`
  note are the accurate record. Do not propagate the looser wording.)
- `backend/app/data/repositories/stops.py` — beside the create path: the `(trip_id, id)` lookup and
  why the cross-parent branch must not be a caught `IntegrityError`. **This is the file where
  readings 1 and 2 would actually be retried**, by someone simplifying a two-step check into one
  insert.

---

## 15. `StopCreate.arrivedAt` must be timezone-aware — the description that was told not to make a claim

**Who:** an earlier scoping pass vs. the user, acting as Architect. The user ruled.
**Where:** `docs/api-contract.md` §"`StopCreate.arrivedAt` must be timezone-aware",
`backend/app/models/stop.py`, `backend/tests/test_stops_list_endpoint.py`.
Task `t-arrivedat-tz-contract` — this entry and the contract section are the **build-order gate**:
the model change that enforces the rule could not start until they landed.

**The ruling.** `StopCreate.arrivedAt` is a **timezone-aware** instant. A naive datetime — one with
no UTC offset — is **rejected** with `422` / `VALIDATION_ERROR`. It is not defaulted to UTC, not
assumed to be the server's local time, and not assumed to be some fixed "trip offset".

**The reasoning on record.** `arrivedAt` is captured **on the device**, potentially hours offline,
while the rider crosses timezone boundaries, and synced later. A naive value therefore has no
correct offset to assume — the server's offset is wrong (the request may arrive days later from
somewhere else), UTC is a guess, and a fixed trip offset is wrong the moment the rider crosses a
border. Accepting one would silently place the stop at the **wrong hour in the timeline**, with
nothing to flag it: wrong pin label, wrong position in the chronological trail, success status,
no error anywhere.

### The position this replaced

A previous scoping pass argued the **opposite direction**, and argued it well: the field description
must **not** claim timezone-awareness *precisely because nothing enforced it*.

State that fairly, because it is the part that evaporates. Its logic: a Pydantic `description=` is
not a comment. It feeds the OpenAPI document, which feeds Kubb's generated client and every agent's
context (CLAUDE.md, "Conventions"). A description asserting a guarantee the code does not implement
is therefore a **contract lie** of exactly the class this log already records twice — Entry 6's
`405`-reported-as-`404` and Entry 7's collapsed handler — where something downstream makes a
decision from a statement that is not true. Given a field typed bare `datetime`, which accepts naive
values silently, the honest description is one that does not promise an offset. On its own terms
that argument is correct, and it was correct when it was made.

**Its evidence is still visible in the tree.** `StopCreate.arrivedAt` is a bare `datetime` with **no
`description=` at all**, while `StopOut.arrivedAt` — where the value genuinely is tz-aware, having
come back out of a `timestamptz` column — *does* say "a timezone-aware ISO 8601 instant". That
asymmetry between the input and output models is not an oversight. It is the earlier call,
implemented exactly as reasoned.

### Why it lost

**Not empirically.** Nothing was measured that contradicted it; no test disproved it. It lost on a
judgment about which side of the gap to close, and that is worth saying plainly rather than dressing
up as a discovered fact.

The gap it identified is real: the description claimed more than the type enforced. There are two
ways to close that, and the earlier pass took the weaker one.

1. **Withdraw the claim** — correct *only* when the guarantee is genuinely unavailable.
2. **Enforce the claim** — available here for the cost of one field type.

The guarantee was one `AwareDatetime` away, so (1) was never forced. And the two errors cost
different amounts: a description that under-claims costs some clarity in the generated types, while
an unenforced naive value costs the stop's place in the timeline — permanently, silently, with no
surface anywhere that it happened. When one side of a gap is cheap to close and the other is
lossy to leave open, "stop claiming it" is the wrong repair.

### The half of the losing argument that survives, and must not be forgotten

Enforcement does **not** fully close the honesty gap, because **JSON Schema has no vocabulary for
timezone-awareness.** `AwareDatetime` and a hand-written field validator emit the *identical*
schema: `{"type": "string", "format": "date-time"}`. There is no keyword meaning "offset required".

So Kubb types this **`arrivedAt: string`**, and the generated client cannot catch a naive value —
no type error, no client-side validation failure, nothing at the call site. The constraint is
**server-enforced only**. The earlier pass's central worry — a stated guarantee that nothing
checks — therefore still applies to the *frontend half* of the system. What the ruling achieves is a
move from "nobody enforces this" to "only the server enforces this". That is the best available
outcome, not a complete one, and the contract section says so rather than implying the generated
client helps.

### The consequence that makes this worth an entry at all

`VALIDATION_ERROR` is a **never-retry** code, and a never-retry outcome is **dequeued permanently
and surfaced to the rider** (see the contract's "Error envelope"). Chain those:

> A frontend bug that sends a naive `arrivedAt` does not retry the stop. It **loses** it.

That is the **accepted trade**, taken deliberately: a surfaced error is recoverable because the
rider is told and can act on it; a stop silently filed at the wrong hour is not, because nothing
ever flags it. But it puts real weight on the frontend sending an offset, and it makes this the
create field where a client-side mistake is most expensive — while being the field the generated
client is least able to protect.

### What did not change

- **The endpoint table is untouched.** A naive value fails Pydantic schema validation *before the
  handler runs*, exactly like a missing `lat` — so it is the **same `422`** already listed on the
  `POST /trips/{slug}/stops` row. No second `422` row, no new code, no new status. All three create
  rows stay byte-identical. Recorded here because "add the 422 for the timezone case" is a
  helpful-looking edit a later reader will otherwise make.
- **`StopOut.arrivedAt`'s description.** Its "timezone-aware ISO 8601 instant" wording is now true on
  input as well as output; it was already pinned by `test_arrived_at_is_timezone_aware` in
  `backend/tests/test_stops_list_endpoint.py`.

### Was explicitly not settled here: the photo form's `takenAt` — SETTLED SEPARATELY, Entry 16

**Status as of 2026-09-17: ruled, by Entry 16, not by this entry.** The paragraph below is the
original text and it still says something true — *this* entry does not settle `takenAt` and must not
be cited as having done it. Entry 16 reached the same conclusion **independently**: by measuring the
server, and by testing the EXIF premise below, which failed. Cite Entry 16.

> `takenAt` is structurally the same field — bare `datetime`, `timestamptz` column, captured on-device
> and possibly offline — and the obvious move is to apply this ruling to it verbatim. **This entry does
> not do that, and must not be cited as having done it.**
>
> Photos carry a constraint stops do not: the natural source of `takenAt` is EXIF `DateTimeOriginal`,
> which is **naive by design** — the offset lives in a separate `OffsetTimeOriginal` tag that is
> frequently absent on imported, exported or edited images. So "reject naive" may be materially harder
> for the frontend to satisfy here, and the answer may have to be that the **frontend supplies the
> device offset at capture** rather than trusting EXIF to carry one. Decided when the photo endpoints
> are scoped (`s-photo-upload-onedrive-sync`). Filed as `t-takenat-tz-question`.

The last sentence of that paragraph turned out to be the answer — the frontend does supply the
offset — but as the *rule*, not as a weaker alternative to one. Entry 16.

**Co-location owed (Entry 4's rule).** One site, outside the `docs` agent's `docs/`-only scope, so
it could not be placed by the patch that recorded this entry:

- `backend/app/models/stop.py`, beside `StopCreate.arrivedAt` — why the type enforces awareness,
  that JSON Schema cannot express it so the generated client will not catch it, and that the
  resulting `422` is never-retry and therefore loses the stop. **This is the file where the rejected
  option would actually be retried**: by someone relaxing the aware type back to bare `datetime` on
  the reasoning that the emitted schema is byte-identical either way. That reasoning is *true* — and
  it is exactly the point being missed, because the schema was never what was doing the enforcing.

---

## 16. The photo form's `takenAt` must be timezone-aware — and the model that was supposed to carry it is deleted

**Who:** the filing that classified this as debt, and the EXIF argument inside it, vs. what was
measured when the task was finally picked up. Both lost, and both lost **empirically**.
**Where:** `docs/api-contract.md` §"`takenAt` on the photo upload form must be timezone-aware",
`backend/app/api/routes/photos.py`, `backend/app/models/photo.py`,
`backend/tests/test_photo_endpoints.py`, `backend/tests/unit/test_photo_model.py`.
Task `t-takenat-tz-question` (gate: **broken**), which absorbed `t-photocreateform-field-descriptions`.

**The ruling.** `takenAt` on `POST /api/trips/{slug}/stops/{stop_id}/photos` is an **offset-aware
ISO 8601 instant**. A naive value is **rejected** with `422` / `VALIDATION_ERROR` — not defaulted, not
assumed UTC, not assumed server-local.

**Read the next two sections before the third.** The conclusion is identical to Entry 15's, and it was
**not** copied from it. The filing for this task said in as many words: *do not close this by pointing
at Entry 15 and copying the `arrivedAt` ruling across; the input is a different kind of input.* It was
not copied. The server was measured, the premise that made photos different was tested, and the
premise failed. That ordering is the whole reason this entry exists — the conclusion on its own would
be indistinguishable from the lazy answer.

### (A) What was measured: the same upload stored two different instants

SQLAlchemy's asyncpg dialect has **no bind processor** for `timestamptz`. A naive `datetime` therefore
reaches asyncpg untouched, and asyncpg's `timestamptz_encode` calls `obj.astimezone(utc)` — which
resolves a naive datetime in the **host process's local zone**.

| Host zone | Input | Stored |
|---|---|---|
| `Malay Peninsula Standard Time` (UTC+8) | `2026-06-14T10:00` | `2026-06-14T02:00Z` |
| UTC (the container) | `2026-06-14T10:00` | `2026-06-14T10:00Z` |

**Eight hours of divergence for identical input, decided by where the API runs.** A developer's local
`uvicorn` and the deployed container wrote different instants from the same request — today, on a
shipped route, with no error raised anywhere. Not "could be misread": *was* read two ways by two
deployments of one codebase.

### (B) The losing argument that was disproved: EXIF

The filing's central claim, and the reason this was kept out of Entry 15:

> `arrivedAt` is supplied by the app at capture, so "reject naive" is a satisfiable rule. `takenAt`
> comes from EXIF, where `DateTimeOriginal` **is naive by design**: the UTC offset lives in a separate
> `OffsetTimeOriginal` tag that is frequently absent on imported, edited or re-encoded images. So
> "reject naive" may be **unsatisfiable** for a large share of real photos — the offset genuinely is
> not in the file.

Stated fairly, because every factual sentence in it is true. `DateTimeOriginal` really is naive,
`OffsetTimeOriginal` really is often missing, and if EXIF were the source of the wire value the
conclusion would follow.

**It fails on its unstated premise: that EXIF is the wire source. It is not.** The frontend composes
this field before the request is built, and `Date.prototype.getTimezoneOffset()` is available
unconditionally. There is no case where the client holds a capture time and no offset to pair with it.
The ladder, now contract text for `s-frontend-add-stop-flow`:

- **(a)** `DateTimeOriginal` + `OffsetTimeOriginal` → use both.
- **(b)** `DateTimeOriginal`, no offset tag → wall-clock reading + the device's **current** offset.
- **(c)** No usable EXIF → `new Date().toISOString()` at pick time.

(b) and (c) can be wrong — an imported photo from a camera whose clock was set elsewhere. They are
wrong by a **bounded, explainable** amount. The unenforced naive value was wrong by *wherever the
server was running*, which is not a property of the photo at all. A worse-but-bounded answer beats an
answer that is a deployment detail.

### (C) Only now does it land where `arrivedAt` landed

With the EXIF objection gone, the field is structurally what `StopCreate.arrivedAt` is, and gets the
same answer for the same reasons. Entry 15's three consequences carry over unchanged and are not
re-derived here: the endpoint table does **not** gain a `422` row (a naive value fails schema
validation before the handler runs, so it is the `422` already listed); JSON Schema has no vocabulary
for timezone-awareness, so Kubb types it `string` and this is **server-enforced only**; and
`VALIDATION_ERROR` is never-retry, so a naive value **loses the photo** rather than retrying it —
accepted, because a photo silently filed hours off is unrecoverable and a surfaced error is not.

### The second losing argument, and the transferable one: the gate classification was wrong

This was filed **TRIGGERED DEBT (Gate 3)**, with the current-consumer line stated plainly:

> Current consumer: NONE. No route imports `PhotoCreateForm` — the model exists and nothing binds it.

**True of the model. False of the behaviour.** `backend/app/api/routes/photos.py` had re-declared
`takenAt: datetime` as an inline `Form(...)` parameter. The route implemented the form **without
importing it** — so the grep that justified "no consumer" was searching for a *symbol* while the
question was about a *behaviour*. The naive-acceptance bug was live on a shipped endpoint for the
entire time it sat in the backlog as having no consumer. Reclassified **Gate 1 (CURRENTLY BROKEN)**.

> **"No route imports it" is not "no route implements it."**

That is the part to carry forward. The triage gate asks whether a runtime path exercises the
behaviour today, and an import graph is only a proxy for that — a good one for models bound as request
bodies, a bad one wherever a route can restate a field inline. `Form(...)`, `Query(...)`, `Header(...)`
and hand-built dicts are all places the same substitution can be made. When a finding's no-consumer
evidence is "nothing imports the class", check whether anything **re-declares the field**.

### The design that was pre-authorised and proved impossible

The task's first choice was to bind the form model properly: `Annotated[PhotoCreateForm, Form()]`,
which would have given the field one home and closed the descriptions task at the same time.

**Structurally impossible on the pinned FastAPI (0.141.1) — proven, not assumed.**
`fastapi.dependencies.utils._should_embed_body_fields` returns `True` **unconditionally** once there
is more than one body field, and `form` + `file` are two. There is no escape hatch:
`params.Form.__init__` never forwards an `embed` argument to `Body`, so `Form(embed=False)` is
silently swallowed — `qa` confirmed that independently. Reproduced on a minimal app: the body schema
becomes `{"form": {"$ref": ...}, "file": {...}}` and a flat multipart POST returns
`422 {"loc": ["body", "form"]}`. **The flat wire format this contract promises is unreachable that
way.**

So the pre-authorised fallback was taken. **`PhotoCreateForm` is deleted** — from `models/photo.py`
and from the tree — and the three inline `Annotated[..., Form(description=...)]` parameters on the
route **are** the request contract. `takenAt` is `Annotated[AwareDatetime, Form(...)]`. That is also
how `t-photocreateform-field-descriptions` closed: the descriptions it wanted now live on the
parameters, which is the only place a client ever saw them from.

**Footnote, so nobody re-derives the general claim as unqualified.** A form model that carries
`UploadFile` as a *field* **does** bind flat on FastAPI 0.141.1 — so "a bound form model is impossible
here" is false as stated. It was disqualified for a different reason: FastAPI then declares the
request content type as `application/x-www-form-urlencoded` rather than the `multipart/form-data` this
contract promises. What is impossible is a bound form model **in criterion 2's stated shape** (model +
separate `UploadFile` parameter, flat multipart wire format), and that narrower claim is what made the
fallback legitimate. Recorded as ordinary debt; see `docs/progress-notes.md` under
`t-takenat-tz-question`.

### What changed, and the one thing that did not

**The OpenAPI diff is description text and nothing else — measured.** The synthesised request body
schema keeps its name `Body_upload_photo_api_trips__slug__stops__stop_id__photos_post` and its four
properties `{id, uploadedBy, takenAt, file}` with identical types. `takenAt` is still
`{"type": "string", "format": "date-time"}`, because `AwareDatetime` and `datetime` emit the same
JSON Schema. No rename, no type change, no new component, and no generated client to break
(`frontend/src/api/` is still a README).

**Which is exactly the trap.** Relaxing `AwareDatetime` back to `datetime` shows up in a spec diff as
*nothing at all*, while reintroducing the host-zone bug in (A). The reasoning someone will use — "the
emitted schema is byte-identical either way" — is **true**, and is precisely the point being missed:
the schema was never what was enforcing this. Entry 15 records the same trap for `arrivedAt`; this is
the second field it applies to.

**`PhotoOut.takenAt`'s description was inverted, deliberately.** `t-photoout-field-descriptions`
(2026-09-17) had written it to make **no timezone claim at all**, and pinned that silence word-by-word
with a ratchet test. That restraint was correct *while nothing enforced an offset*: a description is
contract text Kubb ships into the generated client, and a rule stated there would have settled this
question through a docstring, where nobody looks for rulings. The ruling makes the claim true, so the
description now states it and the ratchet was **inverted into a positive pin**. The earlier call was
not overturned — its condition expired.

**Verification.** Suite green at **506** (from 483 with 1 failing). Mutation-tested: reverting
`AwareDatetime` → `datetime` fails 8 tests; reverting `PhotoOut.takenAt`'s description fails 1;
re-adding the old placeholder string fails 1. `qa` also verified live that a viewer-slug POST carrying
a naive `takenAt` still returns **403**, not `422` — the tightened type opened no 422-before-403
disclosure channel, which is the access-control failure this kind of change most plausibly introduces.

**Co-location (Entry 4's rule) — already placed, by the implementing patch rather than by this one.**
`backend/app/api/routes/photos.py` carries the asyncpg measurement as a comment directly above the
`takenAt` parameter, ending "Do not relax this to `datetime`", and cites the contract section by its
exact title. That is the file where the rejected option would actually be retried, and it is the
reason the contract section's title must not be renamed: the citation is by string.

**The section was retitled, and that is downstream of the fallback.** The `ba` brief proposed
"`PhotoCreateForm.takenAt` must be timezone-aware", parallel to Entry 15's heading. That names a class
this patch deleted, so the shipped title is **"`takenAt` on the photo upload form must be
timezone-aware"** — which is what the route already cites. Not cosmetic: a heading naming a
nonexistent model is how the four stale `PhotoCreateForm` references this patch also had to repair got
there in the first place.

---

## 17. `ErrorEnvelope` in the OpenAPI document — promoting a Gate 3 item, declaring an unreachable `422`, and a catch-all the brief ruled out of scope

**Task:** `t-openapi-error-responses` (2026-09-28). **Files:** `backend/app/api/responses.py` (new),
`backend/app/api/routes/{trips,stops,photos,bikes,map}.py`, `backend/app/main.py`,
`backend/tests/test_openapi_error_responses.py`, `docs/api-contract.md`.

### (a) The promotion — the trigger had already fired

`t-backlog-retro-triage` filed this as **TRIGGERED DEBT (Gate 3)** on 2026-09-15: at runtime every
non-2xx already used the envelope, and the generated client was a *future* consumer — "could consume ≠
currently consumes". That classification was right on the day. Its recorded trigger was "the next route
to land (`s-stop-crud`)", so the shared `responses=` constant would be decided once rather than
improvised per route. **The trigger fired and nothing acted on it**: every M2 route then landed with its
own hand-copied `responses=` dict, and the four GETs each exposed FastAPI's auto-injected `422 ->
HTTPValidationError`. The architect ruling (applied by the orchestrator) promoted it before M3, because
M3 opens with Kubb generation: generated against that document, the client types every error as
`ErrorEnvelope | HTTPValidationError`. `HTTPValidationError` has no `code`, and the offline queue's
retry logic branches on `code` alone (api-contract.md, Error envelope) — the one-envelope premise
would have shipped broken in the generated types while holding perfectly at runtime.

**Transferable point:** a Gate 3 filing is only as good as someone checking its trigger when the
triggering story closes. Here the trigger was specific and named, and still lapsed across four routes.

### (b) The shape — and the argument that lost on the GET `422`s

Ruled: one module-level constant, `ERROR_RESPONSES`, mapping **403/404/409/422 only** to
`ErrorEnvelope` with a generic description; routes pick statuses through `error_responses({status:
description})`, which takes the **model from the constant and the description from the route**, so no
route can declare a non-2xx with any other schema. The `200` replay entries on the three creates stay
per-route (they are success shapes, not errors). **`405` and `500` are not declared on any operation** —
api-contract.md already rules them framework-level, and declaring them per-route would contradict the
per-endpoint table.

The losing position: **declare only reachable statuses**, so the OpenAPI document matches the contract's
endpoint table exactly. On the four GETs the `422` is structurally unreachable — the only inputs are
plain-string path parameters that Starlette's `[^/]+` converter has already validated (qa confirmed this
earlier with 15 hostile inputs, all clean 404s). Declaring a status the server cannot produce is, on
its face, a false statement in the contract. **Why it lost:** FastAPI injects its own `422 ->
HTTPValidationError` on any operation with parameters that doesn't declare a `422` itself. "Declare
only what's reachable" does not produce a document without a `422`; it produces one with the *wrong*
`422`. The only way to keep `HTTPValidationError` out of `components.schemas` is to declare the `422`
with the envelope. So the document and the table **deliberately differ** on those four rows, and the
GET `422` description says the failure could only be on a path parameter and that it does not occur
today (`PATH_PARAMETERS_422`) — it invents no body. Both rules are recorded in api-contract.md, under
"What the OpenAPI document declares differs from the table below — deliberately".

### (c) The brief's premise that was empirically wrong — the SPA fallback

The brief listed "`include_in_schema=False` HEAD siblings **and catch-alls**" as out of scope, and put
`main.py` out of scope. `dev` hit a blocker against that premise: the SPA fallback
`@app.get("/{full_path:path}")` (`backend/app/main.py`, ~L205) is registered **only when
`frontend/dist` exists**, was **not** `include_in_schema=False`, and — being a parameterised operation
with no declared `422` — its auto-injected `422` put `HTTPValidationError`/`ValidationError` straight
back into `components.schemas`. Acceptance criterion 4 could not be met with `main.py` untouched. The
orchestrator added `main.py` to scope for **exactly one edit**: `include_in_schema=False` on that route.
Metadata only, and for the same reason as the HEAD siblings in Entry 11 — an undocumented route must
not produce a Kubb hook. **qa ran a mutant without that edit and the guard tests fail**, so this is
measured, not argued.

Why it is easy to miss: the route only exists in a tree that has a built frontend. A spec check run on a
backend-only checkout would have passed and the problem would have reappeared the first time M3
produced `frontend/dist`.

### What guards it

`backend/tests/test_openapi_error_responses.py` (18 tests) walks the **whole** document rather than a
hard-coded route list — no `HTTPValidationError`/`ValidationError` in `components.schemas`, every
declared non-2xx `$ref`s `ErrorEnvelope`, no operation declares `405`/`500` — plus the per-operation
status sets, the `200` replay entries and the GET `422` wording. 6 of the 18 fail at the pre-patch
HEAD. qa diffed the pre- and post-patch spec: existing descriptions are byte-identical, and the only
additions are the four GET `422`s. The rationale for the constant sits in the docstring of
`backend/app/api/responses.py`, which is where an agent would go to add a `405` or a non-envelope model.

---

## 18. M3 URL shape and first open — `/t/$slug`, and what `/` does when there is no slug to go to

**Ruling:** architect, 2026-09-28. The user was away overnight and delegated contentious calls to the
architect, so this ruling is **final**. It records a contested design choice made before M3 was
scoped: several plausible shapes competed, and one of them departs from the literal spec. No code
exists yet. Recorded by `docs` together with the M3 task rows in `progress.json`.

### The ruling

- Trip routes live at **`/t/$slug`**. Stop detail is `/t/$slug/stops/$stopId`.
- **`/`** redirects to the `lastSlug` stored in localStorage. If there is none, `/` shows a one-field
  **"paste your trip link"** screen. It accepts either a full URL or a bare slug.
- The **display-name prompt is shown only when `TripOut.access === "rider"`**. The name only fills
  `uploadedBy`, and a viewer cannot write, so asking a viewer for it collects nothing. This is a
  **deliberate departure from spec §6.1's literal wording**, which prompts on every first open.

### Why `/` needs a screen at all: iOS storage isolation

On iOS, a web app installed to the home screen does not share storage with Safari, and it opens at the
manifest's `start_url`, which is `/`. So on the first launch after install, `lastSlug` is **empty**,
even though the rider opened the trip link in Safari a minute earlier. Without the paste screen, that
first launch would be a dead end. The paste screen exists for exactly that moment.

**The same split applies to IndexedDB**, which means stops queued in Safari are not visible to the
installed app, and the reverse is also true. `docs/user-guide.md` must tell riders to **capture from
the installed app, not from Safari**.

### Rejected

- **`/$slug`**: this makes the root path namespace belong to the slug, so every future top-level route
  (`/bikes`, or anything else) would collide with a possible slug.
- **A dynamic per-trip manifest**, with the slug baked into `start_url`: this needs a new backend route
  outside the eight contract endpoints. The client-side alternative, a blob-URL manifest, installs
  unreliably.
- **Omitting `start_url`**: Chrome will not offer to install the app.

### Confidence and verification

**MEDIUM on the iOS `start_url` behaviour.** The ruling rests on it, and it has not been observed on a
real device. Verify it on `s-real-device-testing` (M4). If an installed app turns out to open at the
page it was installed from, the paste screen becomes a rarely-seen fallback, and the ruling still holds.

---

## 19. M3 offline queue — one store, FIFO, drained from the page, with a retry classification

**Ruling:** architect, 2026-09-28, delegated by the user, **final**. The competing designs were
per-entity stores versus a single store, and a Service Worker sync versus a page drain. The ruling
also closes gaps in the contract's retry rules that a queue implementation would otherwise have had to
decide on its own.

### The ruling

- **One IndexedDB object store** with an auto-increment key, drained **FIFO, one entry at a time**.
  Entry shape: `{kind: "stop" | "photo", payload, blob?, attempts, lastError}`.
- **A stop is always enqueued before its photos**, so FIFO order already makes photos wait for their
  stop. No dependency graph is needed.
- **The page drains the queue, never a Service Worker via Background Sync**, because iOS does not
  support Background Sync. A drain runs on app start, on `online`, on `visibilitychange` to visible,
  and right after an enqueue. Only one drain runs per tab. If two tabs drain at once, nothing breaks,
  because every replay is a `200`.
- **The drain stops at the first retryable failure.** Backoff starts at 5s, doubles, and caps at 5min.
  It resets on `online`. The drain does not check `navigator.onLine` first; it simply attempts.
- **Offline cold open:** after each successful `GET /trips/{slug}`, the response (`TripOut`) is saved to
  localStorage keyed by slug and passed to the query as `initialData`. vite-plugin-pwa precaches the
  app shell. The app calls `navigator.storage.persist()` once.

### Contract additions (now in `docs/api-contract.md`, Error envelope)

- `FORBIDDEN` and `NOT_FOUND` are **never retry**. The client only queues a write under a slug it has
  already seen resolve to `rider`, and a photo's stop is always sent first. So a `403` or `404` means
  the trip or the parent stop is gone for good. If a stop fails with a never-retry code, its queued
  photos fail with it and are never sent.
- A failure with **no parseable envelope** is **retry**. This covers a network error, a timeout, or a
  `502`/`503`/`504` HTML page from a proxy. Classification works on `code` alone, and a response with
  no envelope has no code.
- "Dequeued and surfaced" means the entry **moves to a failed state and stays in IndexedDB, blob
  included, until the rider dismisses it**. It is never deleted automatically. After 10 or more failed
  attempts, a still-retrying item is shown in the offline indicator along with its last error.

### Rejected

- **Per-entity stores** (stops, photos): these need ordering across stores to keep photos behind their
  stop, which a single FIFO store gets for free.
- **Service Worker Background Sync**: iOS does not support it, and iOS is the main capture device.
- **Persisting the whole TanStack Query cache**: this needs a new dependency to restore one query that a
  single localStorage key already covers.

---

## 20. Photo upload is one idempotent request — the "resumable multipart" requirement is amended

**Ruling:** architect, 2026-09-28, delegated by the user, **final**. This ruling **contradicts two
standing requirements**: spec §4 line 46 ("photo uploads use S3 multipart upload… resumes the multipart
upload on the next attempt") and the Stack sentence in `CLAUDE.md` ("Multipart upload for photos
(resumable on failure)"). It also overturns the Gate 1 (CURRENTLY BROKEN) classification recorded for
`t-photo-s3-multipart-upload` on 2026-09-17.

> **FLAG FOR THE USER'S MORNING REVIEW:** this changes a sentence in `CLAUDE.md`'s **locked Stack
> section**. The technology does not change (still S3-compatible storage, MinIO locally, R2 in prod).
> Only the upload mechanism changes. The orchestrator updates `CLAUDE.md` in the same patch. `docs` did
> not edit it, and did not edit the spec.

### Why the two spec lines cannot both be satisfied

S3 and R2 require every multipart part **except the last to be at least 5 MiB**. Spec §4 also requires
the client to compress photos to about 1600px on the long edge. A JPEG of that size is well **under
1 MiB**, so it is always a single part. With one part, "resume from the parts already uploaded" has
nothing to resume. The two lines cannot both hold. **Compression wins**, because it is what keeps
uploads small on a weak mobile connection in the first place.

### Where resumability actually lives

Resumability moves from S3 parts to the offline queue:

- The blob is stored in IndexedDB (Entry 19), so it survives an app restart.
- A retry after the row already exists is a **replay**. It returns `200` and does not touch storage
  (`backend/app/api/routes/photos.py`, the `find_existing` check at ~L170-174).
- If the server crashes between the S3 put and the DB insert, an orphan object is left behind. The
  retry overwrites it, because the object key `{trip_id}/{stop_id}/{photo_id}` is deterministic
  (~L181-187).

**Worst case, a dropped connection costs re-sending one photo under 1 MiB.**

The `api-contract.md` photo upload notes now say this: one request per photo, retried in full with the
same `id`.

### Rejected

- **Presigned direct-to-S3 multipart**: this needs bucket CORS in `infra/` (off-limits without
  approval) and exposes provider upload ids and ETags to the client, which conflicts with the "only
  `storage/` knows the provider" invariant. It still cannot resume anything under 5 MiB.
- **Backend-proxied multipart**: this needs about three new endpoints (initiate, part, complete), which
  breaks "Eight endpoints … No others". It has the same 5 MiB problem.

### What follows from it

- `t-photo-s3-multipart-upload` is closed as **superseded** (`progress.json` status `done`, and the
  title says superseded).
- The remaining real gap is memory, not resumability: the handler does `await file.read()` and holds
  the whole body in memory. This is filed as `t-photo-upload-stream-to-s3` (TRIGGERED). It replaces the
  read with `upload_fileobj`, which streams and switches to multipart automatically for large bodies.
  Promotion event: a client path starts uploading uncompressed originals, or a request-size cap is
  introduced.
- **Rationale beside the code:** anyone reopening this would do it in `routes/photos.py`. When
  `t-photo-upload-stream-to-s3` touches that block, the comment there should cite this entry, so that
  "add resumable multipart here" is not re-proposed without the 5 MiB constraint in view.

---

## 21. Write-PIN declined — the rider link stays the only write gate

**Ruling:** architect, 2026-09-29, HIGH confidence, **no escalation**. This resolves the open item in
spec §8 ("Write-PIN or trust the rider link as-is"). The answer is: trust the rider link.

> **FLAG FOR THE USER'S MORNING REVIEW:** this closes an open item you listed in spec §8. The spec file
> is **deliberately left untouched**, the same policy as Entry 20: this entry is what supersedes the
> spec. `s-write-pin` is closed as `done`, resolved by decision, with no code.

**Who disagreed:** the spec's optional PIN (spec L50 "Optional: a write-PIN on top of the rider link, in
case it gets forwarded past the trip group"; L86 "write-PIN if wanted"; §8 L93) vs the architect.
Declining it violates nothing, because the spec calls it optional in every place it appears.

### The position that lost: add a write-PIN

The case for it is fair. A rider link forwarded outside the group gives full write access, and a PIN
would be a second factor that a forwarded link alone would not carry.

### Why it lost

- **The threat it covers is small.** The rider slug is `secrets.token_urlsafe(32)`
  (`backend/app/data/seed_trip.py` ~L429), 256 bits, so it cannot be guessed. The only way in is a
  forwarded link. A PIN helps only if it is *not* forwarded along with the link, and in a small group
  chat it usually is.
- **The cost lands in the offline queue, the highest-rigor area.** `FORBIDDEN` is never-retry
  (`api-contract.md` ~L124; `isNeverRetry` in `frontend/src/offline/queue.ts`). A wrong or rotated PIN
  would therefore **permanently fail every queued stop and its photos**. A safe version needs all of
  these: a 7th `ErrorCode`, a paused-queue state, a PIN prompt UI, PIN storage per origin (the iOS
  storage isolation in Entry 18), and a brute-force policy.

### Mitigation instead: rotate the slug

If a rider link leaks, rotate `trips.rider_slug` with one SQL `UPDATE`. The seed script's "cannot be
rotated" note is about the script, not the schema. After rotation:

- the old slug returns `404 NOT_FOUND`, which is never-retry;
- queued items made under the old slug fail visibly, with their blobs kept until the rider dismisses
  them (the contract's "never-retry is not a silent drop");
- riders open the new link.

### Reopen trigger

Reopen this only if **a leaked rider link is actually seen in use outside the group**, or the user
wants writes gated **per person**.

### Design if reopened

Do not re-derive this. The agreed shape is:

- `trips.write_pin_hash`: hashed and nullable. Set by a migration plus a seed-script argument, **not an
  env var**.
- An `X-Write-Pin` header on **every** rider write. A header works for both JSON and multipart bodies.
- A new `PIN_REQUIRED` / `401` `ErrorCode`. The queue treats it as **pause the drain and prompt**,
  never as a failure. This is the part that avoids the never-retry loss above.
- The UI learns a PIN is needed from `TripOut.pinRequired`.
- The PIN is stored per origin in `localStorage`.
- A rate limit or lockout per slug.

**Rationale beside the code:** a PIN would be added in `frontend/src/offline/queue.ts`
(retry classification) and the rider write routes. Anyone reopening it should start from this entry.

---

## 22. Graph refresh token: mint once and reuse — don't persist the rotated one

**Ruling:** architect, 2026-09-29, option A. No escalation: neither option breaks an architecture
invariant.

**Who disagreed:** the premise written into `backend/app/storage/onedrive_sync.py` (the comment at
~L132-135, from `t-onedrive-sync-job`) vs the architect's research. The comment says Graph "rotates the
refresh token on every redemption". Read literally, that means the configured `GRAPH_REFRESH_TOKEN`
stops working after its first use, so a job that throws the new token away would fail on its second
run. If that were true, persisting the rotated token (option B) would be required, not optional.

### What the research showed

- Microsoft **issues** a new refresh token when one is redeemed, but **does not revoke the old one**.
  The configured token keeps working. The comment's premise is wrong.
- Each refresh token has its own lifetime of about **90 days from issue**. **MEDIUM confidence for
  personal Microsoft accounts:** Microsoft publishes no specific number for them. So the sync keeps
  working until **mint date + ~90 days**.
- A token can die **earlier** than that if:
  - the account owner revokes sessions,
  - the app's consent is removed,
  - an admin resets the account or password, or
  - `GRAPH_CLIENT_SECRET` expires. The client secret has its own expiry date, set when it was created.
- Sources: learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens,
  …/configurable-token-lifetimes, …/v2-oauth2-auth-code-flow.

### The position that lost: option B, persist the rotated token

Store each newly issued refresh token in a single-row Postgres table and redeem that one next time.
That would keep the token fresh indefinitely, with no 90-day horizon. It is a fair design, and it
**breaks no invariant**: Postgres is already a synchronous dependency, and access would go through
`data/`.

### Why it lost

- **It touches off-limits code.** OneDrive token handling needs explicit user approval (CLAUDE.md
  "Off-limits").
- **It moves a long-lived secret into Neon**, and from there into every Neon backup and branch. Today
  the token lives only in the Container Apps secret stores and the owner's password manager.
- **It buys nothing for this trip.** The trip is under ~80 days, so a token minted close to departure
  outlives it.
- **A lapse is cheap.** It only pauses archiving. Photos stay in R2, which is the source of truth, and
  `list_pending_archive` picks up everything unarchived on the next run after a re-mint. Nothing is lost.

### What follows from it

- `docs/deploy-cutover-runbook.md` §1 (Graph): mint as close to departure as practical, record the mint
  date and the client-secret expiry date, and check that mint date + 90 days falls after the trip ends.
  Re-minting means re-running the helper and updating the secret on **both** the app and the Job.
  §5: the pre-departure preflight uses a freshly minted token. A new "If archiving stops mid-trip"
  section covers recovery.
- Filed, both ORDINARY and both needing explicit user approval because they touch off-limits token
  handling:
  - `t-graph-token-error-code-logging`: today a rejected token logs only `HTTP <status>`, so an expired
    token looks the same as an outage. Log the response's `error` code (for example `invalid_grant` /
    `AADSTS700082`).
  - `t-onedrive-rotate-comment-misleading`: fix the "rotates" comment.

### Reopen trigger

Reopen option B only if a single trip (or continuous archiving) has to run **longer than ~80 days on
one mint**, or if a real token is seen dying well before 90 days for reasons other than the early-death
list above.

**Rationale beside the code:** option B would be built in `backend/app/storage/onedrive_sync.py`,
right beside the misleading comment. That file is off-limits, so `docs` could not add the rationale
there. `t-onedrive-rotate-comment-misleading` must replace the comment with the corrected fact **and
cite this entry**, so that "persist the rotated token" is not re-proposed from the wrong premise.

---

## 23. A malformed body returns 422 before the access guard runs — closed won't-fix

**Ruling:** orchestrator, 2026-09-29, while clearing the debt backlog at the user's request ("clear all
tech debt that doesn't need me"). `t-malformed-body-precedes-access-guard` set to `done` with the title
suffix "CLOSED WON'T-FIX".

**Who disagreed:** the finding's own framing vs the ruling. `qa` found it during
`t-validation-message-offset`: FastAPI parses and validates the request body **before** it solves route
dependencies. So `'{"id": '` on a write route returns `422` for a rider slug, a viewer slug and an
unknown slug alike, and `require_rider_access` never runs. A body that is valid JSON but fails field
validation still gets `403`/`404` first.

### The position that lost: reorder, so the guard answers first

The spec ranks access control first for testing rigour, and "the access guard is not the first thing
that answers" reads like a violation of that. The fix would be to resolve the slug (404/403) before
the body is parsed.

### Why it lost

- **It is no oracle.** Status and message are byte-identical across all three slug classes, as `qa`
  observed on real uvicorn. The response tells the caller only that its own body was unparseable,
  which it already knew. Nothing that needs authorisation happens on the 422 path: no row is read,
  written or disclosed.
- **FastAPI has no switch for the order.** Getting the guard in first means hand-rolling the body read,
  or adding a slug check (middleware, or a pre-parse dependency trick) that runs ahead of the body.
  Either way that is **a second copy of the access check that can drift** from `require_rider_access`.
  That is the same failure Entry 7(b) recorded for routing-duplicating middleware. A drifted copy would
  be a real access-control defect, which is worse than a harmless ordering.

### Why it matters

"Reorder so the guard answers first" is the obvious suggestion from anyone reading a 422 on a viewer
slug, and the spec's priority order makes it sound urgent. It isn't a leak. The rationale sits beside
`require_rider_access` in `backend/app/core/security.py`, and the real ordering is recorded in a
test docstring in `backend/tests/test_stops_create_endpoint.py`.

**Reopen trigger:** the 422 bodies stop being identical across slug classes (for example, a message
that interpolates something trip-specific), or FastAPI gains a supported way to run dependencies
before body parsing.

---

## 24. A bound form model for the photo upload — closed won't-fix

**Ruling:** orchestrator, 2026-09-29, same debt-clearing pass. `t-photo-form-model-binding` set to
`done` with the title suffix "CLOSED WON'T-FIX".

**Who disagreed:** Entry 16's summary line vs `dev`'s later finding. Entry 16 recorded that binding a
form model "was proven impossible on FastAPI 0.141.1". `dev` then found, during `t-takenat-tz-question`,
that a Pydantic form model carrying `UploadFile` as a **field** does bind flat. So "impossible" was too
broad. The debt row stayed open on the chance that the model was worth bringing back.

### The position that lost: bring the model back

A single `PhotoUploadForm` model is tidier than three inline `Form(...)` parameters plus a `File`. It
matches how every JSON route takes a model, and it gives the fields one home.

### Why it lost

- **It changes the wire contract.** With `UploadFile` inside the model, FastAPI declares the request
  body as `application/x-www-form-urlencoded` in the OpenAPI document, not the `multipart/form-data`
  that `docs/api-contract.md` promises. Kubb generates the frontend client from that document, so the
  generated upload call and its types follow the wrong content type. The offline queue's photo drain
  (top rigour tier) sits on top of that call.
- **The gain is cosmetic.** Behaviour, validation and field descriptions are identical either way.
  The descriptions already live on the inline params, and `takenAt` is already `AwareDatetime` there.

### Why it matters

Entry 16's "impossible" is corrected here to the precise claim: **a bound form model is possible, but
only with a contract change.** Anyone proposing it is proposing a content-type change to the contract,
not a refactor. The rationale sits beside the inline params in `backend/app/api/routes/photos.py`.

**Reopen trigger:** a FastAPI release that keeps `multipart/form-data` in the OpenAPI document for a
form model carrying `UploadFile`. Verify that against the generated spec, not the changelog.

---

## 25. Agent write-boundary hooks for `qa` and `docs`, promoted ahead of their triggers

**Ruling:** user, 2026-09-29 ("clear all tech debt that doesn't need me"), shipped in `c9d6b64`. Closes
`t-qa-mutation-hook` and `t-docs-agent-unscoped-grant`.

**Who disagreed:** the backlog triage vs the user's call. Both items had been triaged as not-yet-work:
- `t-qa-mutation-hook` was **TRIGGERED DEBT**. Its trigger was the first non-empty
  `git status --porcelain` after a `qa` dispatch, and the prose rule in `qa.md` had never been
  breached.
- `t-docs-agent-unscoped-grant` was **ORDINARY DEBT with deliberately no trigger**, because nothing
  watched `docs`'s writes, so "first breach" would have been unobservable.

The triage position was that a working control shouldn't be replaced before it fails, and that a hard
command-line match fails closed against legitimate mutation testing (see the `t-qa-mutation-hook`
note). The user promoted both anyway. That is a priority call the gate allows, not a
re-classification. Neither trigger fired.

### What shipped

- **`qa`**: `.claude/hooks/qa-tree-guard.sh`, wired as a `PreToolUse` + `PostToolUse` pair on Bash
  in `.claude/agents/qa.md`. Before and after each Bash call it fingerprints the working tree (tracked
  plus untracked, `.gitignore` respected) through a throwaway index, so the real index is never touched.
  If the fingerprint changed, it fails the call and tells `qa` to report, not restore. This sidesteps
  the objection that won the first time: it doesn't parse command lines, so mutation work in a scratch
  tree is unaffected however the path was spelled.
- **`docs`**: `.claude/hooks/docs-path-guard.sh`, a `PreToolUse` hook on Edit/Write in
  `.claude/agents/docs.md`. It resolves the target path and allows only `docs/`, `backend/app/`,
  `backend/tests/` and `frontend/src/`, and it blocks `frontend/src/api/` (Kubb-generated).

### Known limits — both hooks are narrower than they look

- **The `qa` guard detects; it does not prevent.** It runs after the command. A file changed and
  reverted within one command leaves identical fingerprints and is not seen at all. A change that is
  left behind is reported, but it has already happened.
- **The `qa` guard gives false positives under parallel work.** It fingerprints the whole checkout,
  so any other agent (or the user) editing the same checkout during a `qa` Bash call trips it. The
  message tells `qa` to say so rather than assume, but the hook itself can't tell whose change it was.
- **The `docs` guard can't tell a doc comment from a logic edit.** It is a path check. Inside
  `backend/app/`, `backend/tests/` and `frontend/src/` (except `api/`), any edit passes, including one
  to executable code. There the "doc comments only" rule is still prose, as before. What the hook
  enforces is that `docs` cannot write `.claude/**`, `CLAUDE.md`, `infra/`, the `Dockerfile`, compose
  or `.env*` (Entry 12's self-modifying-permissions concern).

### Why it matters

These hooks narrow two prose boundaries; they don't replace them. Anyone reading "qa is hook-guarded"
or "docs is path-scoped" as complete enforcement is overreading the hooks. The two prose rules in
`qa.md` and `docs.md` still carry the parts the hooks can't see.

---

## 26. Stop and photo times: stored as instants, shown in the reader's zone with the zone labelled

**Ruling:** architect, 2026-09-29, option A, during the full-codebase review. No escalation: no option
breaks an architecture invariant, and option D was rejected before it could touch the locked stack.

**Who disagreed:** the review's finding vs the storage model the contract settled in Entries 15 and 16.
The review found that stop times were rendered with `toLocaleString()` and no zone, so a viewer at home
and a rider on the road read the same stop at different clock times with nothing to say which clock.
The obvious reading of that finding is "show the rider's local time", and that is the position that
lost. Entries 15/16 require an offset on input, but `arrivedAt` and `takenAt` land in `timestamptz`
columns, which store the instant and discard the offset. Every response is UTC with a `Z`. So the
server cannot tell anyone what the rider's clock said.

### The options

- **A (ruled).** Keep storing instants. Show every time in the **reader's** zone, **with the zone
  labelled**. Shipped in the frontend review batch as `formatInstant` (`frontend/src/format.ts`, used by
  the timeline, the map pin labels and the stop detail page), which renders with
  `timeZoneName: "short"` (for example "ACST" or "GMT+9:30").
- **B: store the rider's offset.** Add `arrived_offset_minutes` (stops) and `taken_offset_minutes`
  (photos), captured from the device, returned beside the instant, and used to render rider-local time.
- **C: have the client send its local offset** in the ISO string (`+09:30` rather than `Z`).
- **D: derive the zone from the coordinates** with a timezone lookup from lat/lng.

### Why B, C and D lost

- **B lost on need, not correctness.** It works, and it is the right design if rider-local time is
  ever wanted. But no spec line asks for it. It is a migration, two model fields, a contract change,
  a Kubb regeneration and queue payload changes, for a display nobody has requested. The labelled
  reader-zone display already removes the actual defect, which was the ambiguity. Filed as **triggered
  debt `t-stop-rider-offset`**. **Trigger: the spec adds a requirement to show the rider's local
  time.**
- **C does nothing.** Storage is `timestamptz`, so a `+09:30` spelling is normalised to UTC on write
  and gone on read, exactly as `api-contract.md` "`takenAt` comes back in UTC" already records. It
  changes the request and nothing else. (The add-stop form sends `toISOString()`, which is `Z`, for
  the same reason: the spelling carries no information the server keeps.)
- **D lost on cost.** A lat/lng → zone lookup needs a timezone-boundary dataset or a library, which is
  a new dependency to keep current. It would also be wrong at a border, on a manual map tap, and when
  the device clock itself was on another zone.

### Why it matters

"Just show the rider's local time" will be suggested again by anyone who reads a timeline in a different
zone from the trip. Sending the offset (C) looks like the cheap fix and is not a fix. Rider-local time
needs the offset *stored* (B), and B waits for its trigger.

**Rationale beside the code:** option B would be built in `backend/app/models/stop.py` (and a
migration), and the display lives in `frontend/src/format.ts`. `format.ts`'s doc comment already
explains the reader-zone choice. Citing this entry there, and beside `arrivedAt` in `models/stop.py`,
was left for the later non-`docs/` pass, because this pass edits `docs/` only.
