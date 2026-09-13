# Decision Log — contested calls and why they resolved the way they did

This is not a changelog. It records **disagreements between agents** (or between an agent and
the orchestrator) and how they were settled — specifically, the reasoning that *lost*.

Why that matters: the losing argument is the part that evaporates when a session ends. The code
that survives is visible in git; the option that was tried, measured and rejected is not. Without
this file, a future agent rediscovers the same constraint, reaches the same first conclusion, and
re-introduces something that was already disproved.

**Read this before reopening a settled call.** If you're about to argue for something listed under
"Rejected" below, the burden is to show why the original evidence no longer applies — not to
re-derive it from scratch.

Owned by the `docs` agent. A new entry gets written whenever one agent overrules, contradicts, or
empirically disproves another.

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
