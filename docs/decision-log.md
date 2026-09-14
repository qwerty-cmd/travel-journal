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
| 6 | ErrorCode missing 405/500 members | `common.py`, `errors.py`, `main.py` | ba escalated rather than inventing codes; user added INTERNAL_ERROR + METHOD_NOT_ALLOWED. **Decision settled; its 405 illustration had two pending expiries and the first has now FIRED** — `t-head-on-get-routes` landed 2026-09-14, so `/api/health` is GET+HEAD and the entry's "GET-only" wording is false (wording only — POST is in neither method set, so the 405 and the conclusion stand); `s-stop-crud` DELETE example still pending. Both edits owned by `t-stops-405-doc-revisit`: the `GET`-only wording is now fixed (contract re-worded in place, entry footnoted 2026-09-15), the DELETE example is still open — see correction + 2026-09-14 and 2026-09-15 footnotes |
| 7 | Error handler mutation coverage | `errors.py`, `main.py`, `test_error_envelope.py` | qa found INTERNAL_ERROR leaked message + SPA catch-all swallowed /api 404s; green suite missed both — mutation testing validates the tests you thought to write, never a path you didn't consider. **Both halves now fixed**: the 405-collapse half closed 2026-09-14 by `t-405-router-route-collapse` (qa-verified on real uvicorn, zero surviving mutants). The body's 2026-09-14 correction is the record of what was broken then, not present state — but its "the catch-all stays" reasoning is still live before touching `main.py` |
| 8 | Cross-trip slug collision | `0001_initial_schema.sql`, `security.py` | Same slug on two trips is fine (random tokens); same slug on one row is not — migration 0002 added CHECK |
| 9 | Seed script print-before-commit | `seed_trip.py` | Print slugs after commit, not before — interrupted print + committed row = unrecoverable slug loss |
| 10 | A shallow copy reasoned about as deep — twice in one patch | `main.py`, `test_error_envelope.py` | `qa` disproved `dev`'s stated reason for the scope copy (the `fastapi` key is *always* already present at handler time); `test-writer` disproved the orchestrator's fix for the resulting test gap (shallow snapshot compared the inner dict against itself). Code was right, reason was wrong; test looked like coverage and had none |
| 11 | HEAD on GET routes: `methods=["GET", "HEAD"]` vs a second registration | `main.py`, `trips.py`, `test_head_method.py` | User chose the explicit method list; `dev` measured that FastAPI emits a duplicate `head:` operation and a duplicate operationId from it, which Kubb turns into a duplicate hook. Shipped as a second, schema-excluded registration of the *same handler* — user's intent kept, literal spelling not. Collapsing the two back into one route leaves **189 of 191 tests green**: only the two OpenAPI guards fail |
| 12 | Finding triage gate, and who may write the governance files | `docs/finding-triage-gate.md`, `CLAUDE.md`, `.claude/**` | The orchestrator's weaker three-question triage was replaced by the user's gate — Stop Condition names the over-investigation behaviours rather than trusting judgment, QA classification is evidence not authority. Separately: no agent can write `CLAUDE.md` or `.claude/**`, so the **orchestrator** owns those paths; widening `docs` to `.claude/**` was rejected as self-modifying permissions |

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
