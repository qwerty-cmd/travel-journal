# Progress Notes

Per-story and per-task notes extracted from `progress.json`. Keyed by ID so agents can read only what they need (`grep -A 999 '## s-my-story' docs/progress-notes.md`).

**Do not read this whole file.** Look up the specific ID you're working on.

---

## s-cloud-service-setup

Touches OneDrive/.env/deployment config — devops agent only, needs explicit human approval per CLAUDE.md off-limits list. Known trap, empirically confirmed by QA: Neon's console connection string includes ?sslmode=require, which asyncpg rejects (it takes ssl=, not libpq's sslmode=). normalize_database_url in app/data/db.py passes the query string through untouched, so this fails at connect time with TypeError: connect() got an unexpected keyword argument 'sslmode' — which will look like a credentials problem and is not. Fix belongs in normalize_database_url when this story is picked up. Migration runner has only been exercised against local docker-compose Postgres 16, never Neon. The simple-query path is a wire-protocol feature rather than a vendor one so it should carry over, but that is untested.

## s-seed-trip-record

Constraint from decision-log.md Entry 3: the seed script must NOT use INSERT ... ON CONFLICT (slug) DO UPDATE. Slugs are random secrets.token_urlsafe tokens, so a collision is a bug signal, not a routine condition — DO UPDATE would silently overwrite an existing trip's slug, producing exactly the authorization leak the UNIQUE constraint exists to prevent. Let the constraint reject the insert and regenerate. Script must also generate both slugs and print them for the user to save; slug values live only in Postgres and are never committed. Sequencing revised by ba: the original argument — that without a seeded trip every endpoint task validates against fixtures alone — no longer holds, because conftest.py's seeded_trips fixture inserts real rows into real Postgres with real generated slugs, so automated coverage gains nothing from a seed. Two reasons survive and downgrade this from blocker to preference: (1) manual/Swagger exploratory validation (spec Section 12) has no entry point without a slug, and hand-inserting one in psql across five endpoint stories is exactly where someone pastes the same string into both slug columns; (2) this script is the only production-path writer of a trip row, and migration 0002's CHECK was added anticipating it, so landing it now is when that constraint gets its real exercise. SCRIPT BUILT, TRIP NOT YET SEEDED. Running it is a human step, deliberately not done by any agent - it prints permanent unrotatable slugs that must not land in an agent transcript. Command: cd backend && uv run python -m app.data.seed_trip --name '<name>' --start-date YYYY-MM-DD. The printed output is the only copy handed to the operator; recovery afterwards is SELECT rider_slug, viewer_slug FROM trips. Story closes when the trip actually exists.

## s-data-layer-foundation

Shared prerequisite for all five m2-core-api endpoint stories; owned by none of them. Sequenced first.

## s-trip-metadata-endpoint

Closed with its only task, t-trip-metadata-endpoint (QA-verified end-to-end on real uvicorn). All three follow-ups QA raised are foundation-level, not endpoint-level, and are filed under s-data-layer-foundation: t-405-router-route-collapse, t-session-dep-alias, t-bike-order-collation. One of those (t-405-router-route-collapse) does affect this route's live behaviour — POST /api/trips/{slug} currently returns 404 instead of 405 — so this story being done does not mean every observable behaviour of the path is correct. FIXED 2026-09-14 by t-405-router-route-collapse: _methods_allowed_elsewhere now probes each verb with route.matches() against a copy of the request scope instead of reading getattr(route,'methods',None), so POST /api/trips/{slug} returns a 405 METHOD_NOT_ALLOWED envelope with Allow: GET. The preceding sentence is left standing rather than deleted because it describes a real defect this story shipped with, and the observation it generalises — a story closing does not verify every verb on its path — is what caused the defect to be found. Allow on this route is exactly GET, no HEAD; t-head-on-get-routes changes that. IT DID, 2026-09-14: Allow on GET /api/trips/{slug} is now exactly {GET, HEAD}, because the path carries a second schema-excluded HEAD registration of the same handler (decision-log Entry 11).

## s-stop-crud

Two endpoints on one path, which is why this story carries the guards it does: GET (either slug) and POST (rider only) sit under the same prefix, so a router-level `dependencies=[...]` cannot express the access rule and each route declares its own by hand — the exact mistake `t-route-dependency-audit` exists to catch. That task and `t-trip-context-slug-exposure` were MOVED HERE from `s-data-layer-foundation` on 2026-09-15, keeping their `gate: triggered` values: both were filed with the promotion trigger "the FIRST wired write endpoint", and that is `t-stops-create-endpoint` in this story, not a foundation task. Moving them is a re-homing, not a re-classification — the gate field is unchanged in both.

This story also fires the outstanding half of `t-stops-405-doc-revisit`: registering GET/POST on `/trips/{slug}/stops` is what makes the `DELETE` -> 404 illustration in `api-contract.md` and decision-log Entry 6 stale. Per Entry 6's own footnote the new status must be **verified by sending the request**, not assumed to be 405.

The contract gap this story surfaced — a client-generated id that already exists under a *different* trip — was ruled on by the user before implementation started: `CONFLICT` / `409`, decision-log Entry 14. The four `t-conflict-*` tasks under `s-data-layer-foundation` land that ruling; `t-stops-create-endpoint` consumes it and is no longer blocked on it.

## s-photo-upload-onedrive-sync

Constraint verified by QA: the SQLAlchemy engine in app/data/db.py is created at module import time and its asyncpg connections are bound to the event loop that created them. Safe under uvicorn, --workers and --reload (the pool is empty at import; connections are made lazily in the serving loop). UNSAFE from a second event loop — starting this sync as threading.Thread(target=lambda: asyncio.run(sync())) or from a sync scheduler creating its own loop, while touching app.data.db.async_session, produces an intermittent AttributeError: 'NoneType' object has no attribute 'send'. Intermittent because the failed checkout invalidates and replaces the connection. dev is adding this constraint as a comment in db.py.

## s-finding-triage-gate

Lands the user-authored Finding Triage Gate and makes it reach the agents that have to apply it. Canonical rule text is docs/finding-triage-gate.md — USER-AUTHORED AND FIXED, transcribe and cite it, never redraft, condense or reorder it. The contested call that produced it is decision-log Entry 12, both halves: (a) the orchestrator's weaker three-question triage was replaced, and (b) no agent in the roster can write CLAUDE.md or .claude/**, so the orchestrator is the recorded owner of those paths. FOUR TASKS, STRICTLY A->B->C->D, one reviewable patch each: t-finding-triage-gate-doc (docs) -> t-claude-md-triage-gate (orchestrator) -> t-agent-defs-triage-gate (orchestrator) -> t-backlog-retro-triage (ba analyses, docs writes). The ordering is not cosmetic: B and C cite the file A creates, and D applies the gate that B and C put into force — running D early classifies findings against a rule the agents have not been told about. Tasks B and C are orchestrator-executed BY DECISION, not by oversight (Entry 12b); do not "fix" their agent field to docs, which is the exact reassignment that blocked t-add-endpoint-head-convention.

## s-agent-tool-boundaries

Covers the gap between what an agent definition PROMISES and what its tool grant ENFORCES. Three tasks: `t-qa-mutation-scratch-tree` (done — the prose rule in `qa.md`), `t-qa-mutation-hook` (the structural version of that same rule, TRIGGERED), `t-docs-agent-unscoped-grant` (the same class of gap through a different channel, ORDINARY). STATUS REVERTED `done` -> `in_progress` 2026-09-16: the story was closed when it had one task and that task landed, and it has since gained two open ones. `in_progress` rather than `not_started` because a story here means "some of its tasks have landed and some have not" — the same reading `s-data-layer-foundation`, `s-stop-crud` and `s-seed-trip-record` carry; `not_started` would contradict a `done` task sitting under it. The general shape: `qa` is described everywhere as having no edit access, but its grant is Read/Bash/Grep — and Bash writes files. The prose guarantee was real, and was being honoured; nothing structural was holding it up. This story is for the cases where that distinction has been noticed and written down, rather than left as an assumption about how agents will behave.

## s-deploy-cutover

Deployment config — devops agent only, needs explicit human approval per CLAUDE.md off-limits list.

---

## t-error-envelope-model

Written directly by the orchestrator before the Architect-vs-pipeline process (spec Section 10) was fully locked down this session — not retroactively re-scoped.

## t-trip-model

Same pre-pipeline context as t-error-envelope-model.

## t-stop-model

Same pre-pipeline context as t-error-envelope-model.

## t-bike-model

Same pre-pipeline context as t-error-envelope-model.

## t-photo-model

Same pre-pipeline context as t-error-envelope-model.

## t-seed-trip-script

Runs as `uv run python -m app.data.seed_trip`, following migrate.py's precedent — a backend/scripts/ dir would need its own sys.path bootstrap (no [build-system]; see t-pytest-pythonpath). seed_trip(conn, ...) takes an injected connection and never creates an engine, so the happy-path test can DELETE FROM trips, seed, assert and roll back — leaving no rows and staying green after the user really seeds the live trip. Printing split into a pure format_slug_output() so output is asserted against the returned object, never a literal slug. Refuses when any trip exists (exit 1); no --force, because its only possible meanings are 'create a second trip' (silent: both link pairs resolve, discovered when photos land on the wrong trip) or 'overwrite existing slugs' (decision-log Entry 3's DO UPDATE by another route). Re-seed is a deliberate psql DELETE. name/start_date are required CLI args. Trip id is server-generated uuid4: client-generated ids are an offline-queue rule and a trip has no client. Constraint asymmetry is load-bearing: UNIQUE violation regenerates (bounded 3 attempts), trips_slugs_differ_check does NOT retry — it means the script assigned one value twice (Entry 8 Amendment) and retrying re-runs the bug. No logging import, no file writes, no str(IntegrityError) in any message (SQLAlchemy renders bound params into it). dev must not run the script for real — that consumes the one-time seed and prints live slugs into a transcript. No bikes seeded: unlike Trip, bikes have a real POST/PATCH contract, so Section 13's 'nobody will use a create flow' reasoning doesn't transfer, and seeded bikes would break the client-generated-id invariant. Bikes stay in s-bike-management. Completed. QA verified all 8 criteria and found 6 hardening issues, all fixed: engine.dispose() moved out of the unrecoverable window and wrapped in suppress() so a dispose failure can no longer discard the committed result; the already-exists refusal now names the recovery path (SELECT rider_slug, viewer_slug FROM trips) and warns against DELETE-and-reseed, since there is no in-app read path for a slug; a failed stdout write exits 1 with that recovery path on stderr; an un-migrated database gets a legible 42P01 message instead of a raw traceback; the generic SQLAlchemyError branch now carries SQLSTATE; and the accepted SELECT-then-INSERT race is commented rather than locked. dev additionally confirmed the parameter-leak risk is real on the generic branch, not just IntegrityError - an undefined-column INSERT renders both slugs into the raw exception string. 126 tests.

## t-db-schema-migration

Text PKs, not uuid: ids are client-generated and the contract does not enforce UUID4 format. No url/archived columns on photos — both derived at read time. QA found the slug-uniqueness test was vacuous (trips_pkey is itself UNIQUE, so the substring check always passed). Fixed to assert per-column against pg_index, and proven able to fail against a non-unique schema. Bespoke SQL splitter removed in favour of asyncpg simple-query protocol after QA disproved the reason for it.

## t-error-envelope-handlers

Closes api-contract.md Outstanding item 2. Sequenced before core/security.py on purpose: security.py raises FORBIDDEN/NOT_FOUND and needs a rendering mechanism to raise into, or it hand-builds JSONResponse bodies that later have to be unwound. No DB dependency — tests must pass with Postgres stopped. ErrorCode expanded to five members (added INTERNAL_ERROR, METHOD_NOT_ALLOWED) per user decision; see decision-log.md. Still in_progress: QA rejected the first implementation on two findings — api_error_handler returned exc.message verbatim regardless of code (INTERNAL_ERROR leak boundary unguarded), and the SPA catch-all in main.py swallows unknown /api paths once frontend/dist exists (QA reproduced: unknown GET /api/* returned 200 text/html, unknown non-GET returned 405 not 404). dev is fixing both. User also decided ApiError gets classmethod constructors plus an __init__ assertion so contradictory status/code pairs are unrepresentable. See decision-log.md Entry 7. Not done until it passes QA. Completed and committed as 6ef08a3. QA findings F1/F2/F3/F6 all fixed; ApiError gained classmethod constructors plus a ValueError backstop in __init__ (not assert — assert is stripped under python -O, which would silently remove the check in exactly the optimised build where a contradictory body goes unnoticed). F5 (ErrorEnvelope absent from the OpenAPI document) split out as t-openapi-error-responses.

## t-slug-access-dependency

Dependency + repository only; no endpoint wired. GET /trips/{slug} follows under s-trip-metadata-endpoint — it accepts either slug, so wiring it here would exercise none of the FORBIDDEN branch, which spec Section 12 ranks top priority. Repository returns TripRecord and never Access: which slug was presented is a property of the request, not of the stored row, so security.py derives Access by comparing the presented slug to the matched row's own columns. No bike join — TripOut.bikes serves one endpoint of eight. No seeded trip exists (s-seed-trip-record still open), so the test fixture inserts and cleans up its own rows with secrets.token_urlsafe slugs and no ON CONFLICT DO UPDATE (decision-log Entry 3). Five mutations enumerated in the acceptance criteria must each be shown to fail a named test (Entries 2 and 7a), including hard-coded access separately from a deleted rider check — the UI hint and the enforcement point are two encodings of the same fact. QA's first review rejected the task on its F1 (same-row slug collision) — the schema permitted rider_slug == viewer_slug on one row, and QA verified the insert succeeded and access_for_slug resolved it to RIDER, so a viewer link under that row silently granted write access. User approved CHECK (rider_slug <> viewer_slug), which landed as migration 0002. Three further findings deliberately deferred as separate tasks — t-route-dependency-audit, t-trip-context-slug-exposure, t-error-log-parameter-redaction. See decision-log.md Entry 8 Amendment. Completed. F1 fixed in get_by_slug by returning None for a slug containing NUL, which keeps the repository's never-raises contract intact and leaves the 404 decision in security.py. CHECK (rider_slug <> viewer_slug) added as migration 0002 after QA found the same-row collision grants silent write access; see decision-log Entry 8 amendment. Both fixes demonstrated non-vacuously. 116 tests passing.

## t-pytest-pythonpath

The project has no [build-system] so uv never installs it into .venv; `import app` fails under the pytest console script without the bootstrap. QA confirmed `uv run python -m pytest` works without it — that invocation-dependent fragility is why pyproject is the right fix. pyproject.toml was out of scope for the schema task.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: ordinary`.
- Finding: the suite's `import app` depends on a `sys.path` bootstrap in `conftest.py` rather than `pyproject.toml`'s `pythonpath`.
- Evidence: the suite is green under both invocations. QA confirmed `uv run python -m pytest` passes without the bootstrap, and the bootstrap itself makes the console-script invocation work today. Nothing fails now.
- Gate classification: ORDINARY DEBT (Gate 4). Invocation-dependent fragility is not a failure — `could break ≠ is broken`.
- Current consumer: none.
- Promotion trigger: none. Deliberately not "when someone runs plain pytest" — that is a hypothetical, and it works anyway.

## t-schema-type-drift-check

QA confirmed Text vs VARCHAR(20), Double vs REAL, and Integer vs BigInteger all compare equal under column.type.python_type. A migration creating varchar(20) where tables.py declares Text would pass the drift guard. Low urgency: current schema is text/double precision throughout.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: `test_columns_match_metadata` compares `column.type.python_type`, which cannot distinguish Text from VARCHAR(20), Double from REAL, or Integer from BigInteger.
- Evidence: QA confirmed all three pairs compare equal. The current schema is text/double precision throughout, so there is no drift present for the blind spot to miss — the guard runs today and has nothing to catch.
- Gate classification: TRIGGERED DEBT (Gate 3).
- Current consumer: none.
- Promotion trigger: the first migration introducing a narrower or length-constrained column type where `tables.py` declares the wider one.

## t-tests-readme-stale

First real test landed as tests/test_schema.py at the root. Cosmetic, pre-existing — was outside the schema task's scope.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: ordinary`.
- Finding: `backend/tests/README.md` documents test subdirectories that do not exist.
- Evidence: the first real test landed as `tests/test_schema.py` at the root. A README has no runtime path — nothing imports it, executes it or serves it, so no behaviour can depend on its being wrong.
- Gate classification: ORDINARY DEBT (Gate 4).
- Current consumer: none.
- Promotion trigger: none.

NARROWED 2026-09-16 — AND THE ORIGINAL FRAMING WAS WRONG, WHICH IS THE PART WORTH KEEPING. The title said the README documents "nonexistent subdirectories", i.e. that the documentation was stale. `t-stopcreate-tz-tests` landed `backend/tests/unit/test_stop_model.py` and `test-writer` cited `backend/tests/README.md:3` — "`unit/` — stop/photo/bike creation and validation" — as its placement reasoning. The README was not describing a tree that had gone away; it was CORRECT GUIDANCE THAT HAD NOT BEEN FOLLOWED YET, and it was read and followed the moment someone wrote a test it applied to. `unit/` RESOLVED ITSELF BY BEING USED, not by anyone editing the README. Nothing was fixed here and no docs patch was spent.

WHAT ACTUALLY REMAINS, checked against the tree on 2026-09-16: `integration/` only. The README reserves it for the three priority failure modes (access control, data integrity, offline queue); there is no `backend/tests/integration/` directory. Everything else in `backend/tests/` is `unit/test_stop_model.py` plus seven root-level `test_*.py` files.

STILL `not_started`, STILL `gate: ordinary`, AND STILL NOT WORK. No consumer, no trigger — a README has no runtime path. `integration/` is on the same trajectory `unit/` just travelled: the first access-control or offline-queue test to be written will read that line and create the directory, the same way this one did. Editing the README to delete the line would REMOVE the guidance that worked, which is the opposite of the fix. If anything ever closes this it should be the directory appearing, not the sentence disappearing.

## t-openapi-error-responses

QA found components.schemas is entirely empty — ErrorEnvelope appears nowhere in the OpenAPI document, so the uniform-error-shape promise holds only at runtime and never reaches the generated frontend client. That client was the stated rationale for having one shape at all (api-contract.md). Needs ba scoping first: it touches every route signature, so it interacts with the endpoint stories rather than being a standalone patch. QA escalated this from polish to real work. GET /api/trips/{slug} declares 422 -> HTTPValidationError, and HTTPValidationError plus ValidationError are emitted into components.schemas alongside ErrorEnvelope. The 422 is structurally unreachable on that route (Starlette's [^/]+ converter guarantees a non-empty slash-free string before Pydantic sees it, and Path() declares no length or pattern constraint — QA confirmed with 15 hostile inputs, all clean 404s). So the generated client gets an error union of ErrorEnvelope | HTTPValidationError for a branch the server never produces, and HTTPValidationError has no error.code — directly contradicting the contract's premise that one envelope shape is what lets the frontend have a single error path and lets the offline queue branch on code alone. Every route copying this pattern multiplies it.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: `ErrorEnvelope` reached the OpenAPI document on one route only, so the uniform-error-shape promise holds at runtime but does not reach the generated frontend client; the structurally unreachable `422 -> HTTPValidationError` union ships alongside it.
- Evidence: at runtime every non-2xx **does** use the envelope — the handlers are registered and the suite exercises them. `t-trip-metadata-endpoint` landed `responses={404: ErrorEnvelope}` on its own route, so `components.schemas` is no longer empty. All of `m3-frontend-pwa` is `not_started`, so no component imports a generated type and no client has yet branched on a shape it was given.
- Gate classification: TRIGGERED DEBT (Gate 3). One of the two closest calls in this triage — see `t-backlog-retro-triage`. The generated client is a *future* consumer, and `could consume ≠ currently consumes`.
- Current consumer: none.
- Promotion trigger: the next route to land (`s-stop-crud`), where the shared `responses=` constant is decided once rather than improvised per route.

## t-ruff-format-gate

ruff format --check would reformat app/core/errors.py and three other files; ruff check alone doesn't cover formatting. Decide whether format joins the validation gate.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: ordinary`.
- Finding: `ruff format --check` would reformat `app/core/errors.py` and three other files; `ruff check` alone does not cover formatting.
- Evidence: no spec requirement, invariant, access-control rule, data-integrity rule or behavioural contract mandates format enforcement. Nothing is violated by those files being unformatted.
- Gate classification: ORDINARY DEBT (Gate 4). It is a standing decision, not a defect.
- Current consumer: none.
- Promotion trigger: none. Take it at a story boundary per the gate's Debt Promotion clause — explicitly not a calendar review.

## t-validation-message-offset

422 message for an unparseable JSON body reads '1: JSON decode error' — the '1' is pydantic's byte offset rendered as if it were a field path. message is contractually safe to show a rider directly; this is safe but meaningless to one.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: the 422 `message` for an unparseable JSON body renders pydantic's byte offset as if it were a field path.
- Evidence: every registered route today is a GET and none accepts a request body, so no client can reach the branch at all. The text is safe, merely meaningless.
- Gate classification: TRIGGERED DEBT (Gate 3). The second of the two closest calls — see `t-backlog-retro-triage` — because the defect is real and already rendered; what is absent is any path to it.
- Current consumer: none.
- Promotion trigger: the first request-body endpoint, `POST /trips/{slug}/stops`.

## t-errors-docstring-tense

app/core/errors.py docstring says core/security.py raises FORBIDDEN/NOT_FOUND through ApiError; security.py is currently a comment-only stub that raises nothing. Expected to resolve by implementation when t-slug-access-dependency lands — security.py will then genuinely raise through ApiError, making the docstring true. Verify and close rather than working it; do not edit the prose. VERIFIED AND CLOSED after t-slug-access-dependency (64c0fa2); no prose changed in errors.py. Checked: security.py contains exactly two raise statements, both ApiError via classmethod constructor — ApiError.not_found(UNKNOWN_TRIP_MESSAGE) in _resolve_trip (security.py:139, the single 'no such trip' site both dependencies funnel through), and ApiError.forbidden(READ_ONLY_LINK_MESSAGE) in require_rider_access (security.py:195, reached only after a trip resolved). That matches both halves of the errors.py docstring: the FORBIDDEN/NOT_FOUND claim (errors.py:16-18) and the 'raise through the classmethod constructors, never a two-argument call' instruction (errors.py:20-28). Grep for 'raise |ApiError\\.' over security.py returns no other raise site and no direct ApiError(...) construction, so the docstring claims nothing broader than what landed — it does not attribute VALIDATION_ERROR or INTERNAL_ERROR to security.py. (api_error_handler's docstring mentions security.py wrapping a driver error in ApiError.internal, but states it as a future condition, not current behaviour.)

## t-route-dependency-audit

QA demonstrated the mistake is silent: a write route declaring require_trip_access instead of require_rider_access accepts a viewer slug and returns 200, and all 34 slug tests still pass because they exercise the dependencies through probe routes rather than real routes. Five upcoming tasks each declare a guard by hand, and the stops router carries both a GET (either slug) and a POST (rider only) under one prefix, so a router-level dependencies=[...] cannot be used. The signal already exists and nothing consumes it: FastAPI exposes route.dependant, and the two dependencies' Path(description=...) strings differ in the generated OpenAPI. MUST land with the FIRST wired write endpoint (s-stop-crud), not after all five.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: a write route declaring `require_trip_access` instead of `require_rider_access` accepts a viewer slug and returns 200, silently, and the existing slug tests cannot see it because they exercise the dependencies through probe routes rather than real routes.
- Evidence: QA demonstrated it rather than reasoning about it. But no write route exists today — every registered route is a read, and `t-trip-metadata-endpoint` confirmed the inverse mistake on a read route is loud, not silent.
- Gate classification: TRIGGERED DEBT (Gate 3). Access control, so it is top priority *within* the debt — priority is ordering, not classification.
- Current consumer: none.
- Promotion trigger, reused verbatim from the note above: "MUST land with the FIRST wired write endpoint (s-stop-crud), not after all five."

MOVED TO `s-stop-crud` 2026-09-15, `gate: triggered` UNCHANGED. The task was always filed against a promotion trigger that lives in that story ("the FIRST wired write endpoint"), and it sat under `s-data-layer-foundation` only because that is where it was discovered. Re-homing, not re-classification: the gate, the evidence and the trigger above are all untouched. It lands with `t-stops-create-endpoint`.

## t-trip-context-slug-exposure

QA: TripContext.trip necessarily holds rider_slug and viewer_slug so the dependency can derive access, and TripOut has no slug fields — but the first handler that spreads the record into a response (return {**asdict(context.trip)}) breaks the guarantee invisibly. The existing test_no_slug_appears_in_any_response_body asserts against probe routes only, so it will not cover real handlers. Pairs naturally with t-route-dependency-audit.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: `TripContext.trip` necessarily holds both slugs, and the first handler that spreads the record into a response body would break the no-slug guarantee invisibly; `test_no_slug_appears_in_any_response_body` asserts against probe routes only.
- Evidence: `t-trip-metadata-endpoint` folded in a route-local assertion for its own 200 body, so the one real handler that exists today is covered. What remains open is the generalised guard, which has no route to generalise over yet.
- Gate classification: TRIGGERED DEBT (Gate 3).
- Current consumer: none.
- Promotion trigger: the generalised all-routes guard landing with `t-route-dependency-audit` at `s-stop-crud`.

MOVED TO `s-stop-crud` 2026-09-15, `gate: triggered` UNCHANGED — same reasoning as `t-route-dependency-audit` above, with which it pairs: its own promotion trigger names that task and that story. Re-homing, not re-classification.

## t-error-log-parameter-redaction

QA: unhandled_exception_handler uses logger.exception, so a DBAPIError traceback carries parameters: ('<live-slug>', ...). The slug is already in the uvicorn access log by virtue of being in the URL, so this is a duplicate rather than a new exposure class — but it is the copy most likely to reach a third-party error tracker. Low priority; noted so it is a decision rather than an oversight.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: observable`. **THE ONLY FIX NOW IN THE BACKLOG, AND THE ONLY CONTESTED CLASSIFICATION — see decision-log Entry 13.**
- Finding: `unhandled_exception_handler` (`backend/app/core/errors.py:241`) calls `logger.exception` at :248, so a `DBAPIError` traceback carries `parameters: ('<live-slug>', ...)` — a live trip slug, which is the credential.
- Evidence: QA **observed the rendered traceback**, it did not reason about it. Both the handler and the route that reaches it are registered and run today.
- Gate classification: CURRENTLY OBSERVABLE (Gate 2) → FIX NOW. Gate 2 asks whether a runtime path existing today exercises the behaviour, not how severe the consequence is. Severity belongs in ordering, not classification — which is why "low priority" above and "fix now" here are not in conflict.
- Current consumer: the 500 handler itself, on any unhandled `DBAPIError`.
- Promotion trigger: none — already promoted.
- CONTESTED. The note's own argument was for ORDINARY DEBT with trigger "first error tracker or log shipper configured", on the grounds that the slug is already in the uvicorn access log so marginal disclosure is zero. Stated fairly and recorded in full at decision-log Entry 13; do not re-argue it from this summary.
- SCOPE CHANGED, CLASSIFICATION UNCHANGED. The orchestrator observed — and the user accepted — that the task as written closes only *one of two* copies: fixing `logger.exception` leaves the slug in the access log, so the task does not close the exposure it names. It is re-scoped to cover BOTH sinks, or to explicitly decide the access log is acceptable and say why. That is a change to what the task must do, not to its gate.
- NOT IMPLEMENTED IN THE TRIAGE PATCH. Classified FIX NOW and left unstarted, deliberately — the Stop Condition binds `t-backlog-retro-triage` itself, and the evidence for a Gate 2 call should survive being written down and reviewed before anyone edits production code on the strength of it.
- CO-LOCATION OWED: decision-log Entry 13's rationale belongs beside `unhandled_exception_handler` in `backend/app/core/errors.py`, which is outside the docs agent's `docs/`-only scope. Whoever implements this carries it into that file; it is not optional tidy-up (Entry 4's rule).

RE-SCOPE FOUND A THIRD SINK 2026-09-15 — **three, not two.** The re-scope above ("BOTH sinks") was itself counting short. The sink nobody had recorded is **the handlers' own format arguments**: `request.url.path` *is* `/api/trips/<slug>`, and all three 5xx handlers log it. So the task as originally filed — redact the bound SQL parameters carried by `logger.exception` — would have shipped a guard whose own log line still printed the credential. That is the finding, not a detail of it: a redaction task whose remaining log statement re-emits the redacted value closes nothing. The three sinks are (1) the `logger.exception` traceback's `parameters: ('<live-slug>', ...)`, (2) the 5xx handlers' own format arguments via `request.url.path`, and (3) the uvicorn access log.

SINK 3 SPLIT OUT AS ITS OWN TASK, DELIBERATELY. The uvicorn access log is not application code and cannot be closed from `backend/` at all — see `t-access-log-slug-exposure` (agent `devops`, `gate: triggered`). This task covers sinks 1 and 2 only. Splitting it is not a way of dropping it: the split exists because the fix lives in a uvicorn CLI flag, which is a different agent's scope, and folding it in here would have put `devops` work inside a `dev` patch.

IMPLEMENTATION LANDED, TASK STAYS `in_progress` (2026-09-15, commit `bd25cbb`). `dev`'s implementation is in, and `dev` stated plainly that **the green suite is a regression gate and not proof** — it shows nothing that used to work is broken, not that the slug is absent from the three sinks. The binding tests are being written now by `test-writer`. **This task closes when those tests land AND `qa` signs off**, not on the commit. Recording the distinction because a landed commit plus a green suite is exactly the shape that gets a task marked `done` by inspection — and Entry 7(b) is the standing record of a green suite missing a path nobody thought to write a test for.

CLOSED 2026-09-16 (commit `5b80bfb`, "bind the slug redaction on the 5xx log sinks"). The closing condition stated immediately above is the one that was met — binding tests plus `qa` sign-off, not the earlier implementation commit `bd25cbb` on its own. Covers sinks 1 and 2 only: the `logger.exception` traceback's bound SQL parameters, and the 5xx handlers' own format arguments via `request.url.path`. SINK 3 REMAINS OPEN as `t-access-log-slug-exposure` (`devops`, `gate: triggered`) — this task closing does not mean the slug has left every log. The CO-LOCATION OWED item above (Entry 13's rationale beside `unhandled_exception_handler` in `backend/app/core/errors.py`) was part of this task's definition of done and lives in a file the `docs` agent cannot write; confirm it rode along in the patch rather than assuming it did.

## t-405-router-route-collapse

LIVE CONTRACT VIOLATION found by QA on real uvicorn. _methods_allowed_elsewhere in main.py scans app.routes and skips anything where not getattr(route,'methods',None). This FastAPI version represents an included router as one lazy _IncludedRouter entry with path=None and methods=None and no .routes attribute, so every route registered through api_router is invisible to the scan — all eight contract endpoints. Only /api/health, registered directly on app, is visible, which is why the guard appeared to work. POST /api/trips/whatever returns 404; POST /api/health correctly returns 405 with Allow: GET. QA confirmed the /api/{rest:path} catch-all is the cause, not Starlette: remove it and the same requests return 405 correctly, because _IncludedRouter.matches() does return Match.PARTIAL for POST and Match.FULL for GET — the information is there, just unreachable via getattr. Entry 6 calls this collapse a contract lie the offline queue acts on (it branches retry-vs-never-retry on code); Allow on a 405 is an RFC 9110 MUST. Green suite is Entry 7b again: test_spa_wrong_method_on_a_real_api_route_is_still_405 (test_error_envelope.py ~709) asserts against /api/health only. Gets worse at s-stop-crud, where DELETE on a registered stops path must be 405 and will silently stay 404. ba is scoping the fix, including whether to detect via public route.matches() rather than private _IncludedRouter internals, and whether the catch-all should exist at all. ba decisions: (1) FIX BY route.matches() PROBING, not by traversing _IncludedRouter. getattr(route,'methods',None) is not a wrong attribute — it is a public-looking one that is not universally present, and its absence fails SILENTLY and toward 404, the direction the contract calls a lie. Traversing private internals reproduces that exact failure mode one layer deeper and would regress silently on the upgrade that renames the lazy wrapper. matches() returns PARTIAL/FULL but NOT the verb set, and Allow is an RFC 9110 15.5.6 MUST, so the set is derived by probing each candidate verb against a COPY of the scope and collecting FULL matches. Note getattr(route,'path',...) can be None, not merely missing. (2) THE CATCH-ALL STAYS. Removing it fixes 405s and reinstates 200 text/html for unknown GET /api/* once frontend/dist exists — a success status carrying HTML to a Kubb client that will parse it as ErrorEnvelope, worse than a wrong code. The narrower construction (exclude /api from the SPA fallback rather than shadow it) needs a hand-assigned Route.path_regex or routing-duplicating middleware — private internals or a drifting second copy of the routing table — and it makes /api correctness depend on whether frontend/dist exists, the environment-conditional coupling Entry 7b forbids. The task requires this be written into _methods_allowed_elsewhere's docstring, because a future reader WILL re-derive 'just delete it': the current comment says why the route exists but not that the route is also what breaks 405s. (3) AC4 is the criterion that matters most: every route registered today is GET-only, so a hardcoded Allow: GET would satisfy a naive assertion. The probe app must be built in production shape with a GET+POST path and a PATCH-only path, and must register the PRODUCTION catch-all rather than a copy. ba also proposed this fix under a new id t-api-405-method-detection; NOT added — same bug, and two ids for one bug is worse than either. The decisions above are that scope.

COMPLETED 2026-09-14. The fix: _methods_allowed_elsewhere probes each verb in _ALL_METHODS against a copy of the request scope and collects Match.FULL, deriving the Allow set that route.matches() does not return. It replaces the getattr(route,'methods',None) scan, which was blind to every route registered through api_router because this FastAPI version represents an included router as a single lazy _IncludedRouter with path=None and methods=None. Defect confirmed present at HEAD before the fix: POST /api/trips/whatever -> 404 with no Allow header, while POST /api/health -> 405 Allow: GET — that second result is precisely why the broken guard looked correct, since /api/health is the one route registered directly on app. QA verified every acceptance criterion on real uvicorn with ZERO SURVIVING MUTANTS ACROSS SIX MUTATIONS, including one isolating only the FastAPI-key write; baseline independently reconstructed from the HEAD tree, 170 -> 186 tests. Zero cost on the happy path: one caller, on the unknown-path/405 branch only — QA instrumented the probe and measured 0 invocations on a 200 and 1 on a 405. Allow on /api/trips/{slug} is exactly GET with no HEAD, because FastAPI's APIRoute does not auto-add HEAD the way Starlette's plain Route does; t-head-on-get-routes changes that and must flip the affected Allow assertions.

CORRECTION 2026-09-15: the sentence immediately above expired on 2026-09-14 and is left readable because it records what was true when this task shipped. It did happen. `t-head-on-get-routes` landed (`ec34a13`): all three Allow assertions now assert `{"GET", "HEAD"}`, and `qa` verified `allow: GET, HEAD` on real uvicorn rather than in the test client. So `Allow` on `/api/trips/{slug}` is no longer "exactly GET with no HEAD" — it is GET+HEAD, via the second schema-excluded registration of the same handler (decision-log Entry 11). The reasoning in that sentence was correct: FastAPI's `APIRoute` still does not auto-add HEAD the way Starlette's plain `Route` does, which is precisely why an explicit second registration was needed.

DEEP-COPY PROPOSAL — CONSIDERED AND CLOSED, DO NOT RE-PROPOSE, NO TASK ID. The proposal was to deep-copy the request scope for probing instead of the one-level {**request.scope, ...}. Its plausible half — "a future FastAPI stops restoring the bookkeeping key, so the shallow copy leaks a probe write onto the live scope" — is now covered by test_probing_leaves_the_live_request_scope_unchanged, proven non-vacuous by disabling the restore. Its other half — "this probe loop stays synchronous, so no other task observes the intermediate state" — is uncovered and STRUCTURALLY UNCOVERABLE by a post-condition test: a state that is restored before the call returns is invisible to any assertion made after it. That half is a code-review invariant, not a test gap, and the invariant is simply that there is no await in that function. If the proposal is ever revived it MUST be a one-level dict() copy and NEVER copy.deepcopy — the scope's values are _IncludedRouter instances and route contexts that FastAPI reads back by identity, so deepcopy would clone live routing objects and the probe would then match against copies rather than the real routing table. See decision-log.md Entry 10 for why the copy exists at all (it is not the reason dev's first docstring gave).

## t-session-dep-alias

QA: app/api/routes/trips.py is the only file under app/api/ importing SQLAlchemy, for the DI annotation only — not a violation in substance (identical to what security.py does, no statement built, no column named), but db.py's own header says routes depend on repositories 'never on SQLAlchemy directly', and seven more routes are about to copy the import. Proposed: SessionDep = Annotated[AsyncSession, Depends(get_session)] in app/data/db.py, so routes write session: SessionDep. Keeps DI wiring in the layer allowed to know SQLAlchemy exists and makes it impossible for one route to wire a different session callable. ba is deciding whether this folds into t-405-router-route-collapse or stands alone. Cheap now, seven diffs later. ba decided: separate task, NOT folded into the 405 fix and not declined. Reasons: it changes app/data/db.py, which every data task depends on, from inside a task scoped to main.py; the 405 patch's value is a reviewable diff of a subtle routing change and an unrelated DI-convention change muddies it; and it needs a decision that task cannot make — core/security.py has two Annotated[AsyncSession, Depends(get_session)] sites, so adopting the alias in routes only leaves the convention applied to some of its sites, which is Entry 5's failure mode. SEQUENCING IS THE POINT: MUST land BEFORE the first s-stop-crud handler, since that is where the copying starts — same shape of constraint as t-route-dependency-audit. When scoped, enumerate every session-taking site (routes plus core/security.py) and state which adopt the alias and which do not, deliberately rather than by omission. Be honest about what it buys: the alias hides the IMPORT, not the coupling — the session is still a SQLAlchemy object handed to a repository — so the justification is greppability and making db.py's header true, not a portability fix.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`. **THIS TRIGGER IS FIRING NOW.**
- Finding: routes annotate `Annotated[AsyncSession, Depends(get_session)]` directly, against `db.py`'s own header saying routes depend on repositories and never on SQLAlchemy directly; seven more routes are about to copy the import.
- Evidence: one file does it today (`app/api/routes/trips.py`), for the DI annotation only — no statement built, no column named. `ba` already ruled it is not a violation in substance.
- Gate classification: TRIGGERED DEBT (Gate 3) — and the trigger is firing as this is written, with `s-stop-crud` being scoped concurrently.
- Current consumer: none in the defect sense; the one existing site works.
- Promotion trigger, reused verbatim from the note above: "MUST land BEFORE the first s-stop-crud handler, since that is where the copying starts."
- A firing trigger is a promotion candidate, not a licence: it is still not implemented in this patch, and whoever promotes it must first do what the note above requires — enumerate every session-taking site (routes plus `core/security.py`) and state which adopt the alias and which do not, deliberately rather than by omission.

## t-bike-order-collation

QA: the local Postgres is Alpine/musl, where en_US.utf8 is unimplemented so the default collation is byte order, identical to C and to Python's sorted(). Neon runs glibc and orders differently — default gives 'ALEX, Alex, Ana, Zoe, alex, Ana-with-accent'; en-US-x-icu gives 'alex, Alex, ALEX, Ana, Ana-with-accent, Zoe'. test_bikes_are_ordered_by_rider_name_then_id compares against Python's sorted(), which agrees with musl by construction. Today the seeded_bikes fixture masks it because rider names differ in their first letter. The moment anyone adds a case- or accent-differing rider name, the test passes locally and fails on Neon. Determinism itself is unconditional (bikes_pkey on id alone makes ORDER BY rider_name, id a total order under any collation) — only the concrete sequence differs. Not urgent; nothing may depend on order per the contract. Note for whoever next edits seeded_bikes.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: `test_bikes_are_ordered_by_rider_name_then_id` compares against Python's `sorted()`, which agrees with the local Alpine/musl byte-order collation by construction and disagrees with glibc.
- Evidence: QA measured both orderings. The `seeded_bikes` fixture currently masks it because every rider name differs in its first letter, and determinism itself is unconditional — `bikes_pkey` on `id` makes `ORDER BY rider_name, id` a total order under any collation. Only the concrete sequence differs.
- Gate classification: TRIGGERED DEBT (Gate 3).
- Current consumer: none — nothing may depend on bike order per the contract.
- Promotion trigger, reused verbatim from the note above: running against Neon/glibc, or anyone adding a case- or accent-differing rider name to `seeded_bikes`.

## t-head-on-get-routes

Every GET route also answers HEAD, declared explicitly as methods=["GET", "HEAD"], so HEAD /api/health returns 200 rather than 405 (RFC 9110 section 9.3.2 — HEAD is GET without a body, and a server that registers GET is expected to answer HEAD on the same path). Scope: backend/app/main.py (/api/health) and backend/app/api/routes/trips.py (GET /trips/{slug}). The SPA fallback is DELIBERATELY EXCLUDED. MECHANISM CHOSEN BY THE USER; a global HEAD->GET middleware was considered and REJECTED — decision-log Entry 7b already rejected routing-duplicating middleware (a drifting second copy of the routing table), and stripping the body from a HEAD response is the server's job, not the application's. Do not re-propose the middleware.

MANDATORY SEQUENCING: only after t-405-router-route-collapse is COMMITTED. Same file, same assertions — the two patches collide on main.py and on the Allow assertions, and reviewing them as one diff hides which change caused which status flip.

ba's AC8 TRAP, the reason this is not a two-line change: fastapi/openapi/utils.py::get_openapi_path loops `for method in route.methods:` with NO HEAD exclusion, and operation_id is per-route rather than per-operation. So a naive methods=["GET","HEAD"] declaration emits a SECOND `head:` operation into the OpenAPI document, from which Kubb generates a duplicate frontend hook, plus a "Duplicate Operation ID" warning at generation time. The OpenAPI document feeds the generated client and every agent's context, so polluting it is a real cost, not cosmetic. IF DEV CANNOT MEET THE ACCEPTANCE CRITERIA WITHOUT POLLUTING THE SPEC IT MUST STOP AND ESCALATE, NOT IMPROVISE (Entry 6b — a gap left visible beats a gap papered over).

Also in scope as recorded decisions: HEAD is adopted by the remaining seven contract endpoints AS THEY LAND, not retrofitted in a sweep. And three exact Allow assertions must flip from {"GET"} to {"GET", "HEAD"} — backend/tests/test_error_envelope.py around lines 910, 945 and 955; the middle one is now test_probing_leaves_the_live_request_scope_unchanged. Those line numbers are from the t-405 patch and will drift; match on the assertion, not the line.

COMPLETED 2026-09-14. WHAT SHIPPED IS NOT WHAT WAS SPECIFIED, AND THAT IS THE HEADLINE. methods=["GET", "HEAD"] on one route is impossible on FastAPI 0.141.1 without polluting the OpenAPI document — ba's AC8 trap above is real: get_openapi_path loops `for method in route.methods` with no HEAD exclusion while operation_id is per-route, so one route with both verbs emits a second `head:` operation sharing the GET's operationId, plus a Duplicate Operation ID warning, and Kubb generates a duplicate identical hook from it. Each path instead carries a SECOND REGISTRATION OF THE SAME HANDLER: add_api_route(..., methods=["HEAD"], include_in_schema=False) — app.add_api_route in main.py for /api/health, router.add_api_route in trips.py for GET /trips/{slug}. Still a route-level declaration, still not the rejected middleware: the path's route set is GET+HEAD, which is what _methods_allowed_elsewhere probes and reports in Allow, and body-stripping remains uvicorn's job. The user's intent survived; only the literal spelling did not. See decision-log.md Entry 11 — and note the title of this task was reworded to match what landed, because a task title asserting the rejected spelling is the next author's instruction.

QA VERIFIED ALL TEN CRITERIA ON REAL UVICORN against the production session, with the pre-patch tree reconstructed via `git archive HEAD` so AC4's "byte-identical" was MEASURED, NOT EYEBALLED — md5-identical GET bodies for viewer slug, rider slug and unknown slug. Access control confirmed directly rather than inferred: the GET and HEAD routes resolve IDENTICAL dependency trees, require_trip_access genuinely runs on HEAD, and an unknown slug is 404 not 403, so the slug-space oracle stays closed. 186 -> 191 tests. Five mutations run (A, B, C plus qa's own D: HEAD pointed at a stub handler with no access dependency — caught). Mutation C is the one with a future: collapsing the two registrations back into one methods=["GET","HEAD"] route leaves 189 of 191 GREEN, failing only test_openapi_document_declares_no_head_operation and test_generating_the_openapi_document_emits_no_duplicate_operation_id_warning. The obvious simplification is invisible to every test that sends a request and its damage lands in a generated client — those two guards are the only thing catching it, which is why they must not be deleted as redundant.

QA raised two findings, both filed rather than fixed here: t-head-route-kwarg-divergence (Finding 1) and t-api-healthcheck-wiring (Finding 2 remainder).

## t-head-route-kwarg-divergence

From qa's Finding 1 on t-head-on-get-routes. The two registrations on a path SHARE A HANDLER, so dependencies declared in the HANDLER SIGNATURE stay in sync automatically — that is what makes the second-registration mechanism safe and it is not in question. Dependencies declared as a route-level dependencies=[...] KWARG do NOT: that kwarg belongs to the decorator, not the handler, so it reaches one route of the pair. QA demonstrated it, it did not reason about it — adding Depends(require_rider_access) to the @router.get decorator in app/api/routes/trips.py yields GET -> route_level_dependencies = ['require_rider_access'], HEAD -> [], and ALL 5 TESTS IN test_head_method.py STILL PASS. GET would enforce rider-only while HEAD kept serving viewers.

Why the existing tests structurally cannot see it: the per-path guard (test_every_documented_get_path_also_answers_head) only compares HEAD against GET on an UNKNOWN slug, where both are 404 regardless of any guard; and test_head_on_a_trip_slug_still_runs_the_access_dependency asserts a viewer slug SUCCEEDS — which it still would, on the HEAD route that lost the guard. Neither assertion is wrong; both are blind to divergence by construction. Same exposure applies to responses=, status_code= and summary=, which ALREADY DIFFER between the pair today — harmless only because the HEAD route is include_in_schema=False, i.e. harmless by accident of a different decision.

PROPOSED SHAPE: for every path carrying both a GET and a HEAD route, assert the two resolved `dependant` trees and route-level dependency lists are EQUAL. RELATE IT TO t-route-dependency-audit: that task guards a write route declaring the wrong guard (one route, wrong dependency); this one guards two routes on one path drifting apart (right dependency, reaching one of two). Adjacent, not the same — and neither subsumes the other, so do not fold one into the other. Whoever lands the second should state whether they share a file; both enumerate routes off app.routes and would otherwise grow two near-identical traversals. This is ACCESS CONTROL, spec Section 12's top priority, and the demonstrated failure is silent — it should not sit indefinitely.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: a route-level `dependencies=[...]` kwarg belongs to the decorator, not the handler, so on a path carrying a GET/HEAD pair it reaches one route of the two — GET could enforce rider-only while HEAD kept serving viewers.
- Evidence: QA demonstrated it (GET resolved `['require_rider_access']`, HEAD resolved `[]`, and all 5 tests in `test_head_method.py` still passed). But no route in the codebase uses that kwarg today; every dependency is declared in the shared handler signature, where the pair stays in sync automatically.
- Gate classification: TRIGGERED DEBT (Gate 3). Access control, so it leads the ordering within the debt — but there is no divergence to catch until the kwarg is used.
- Current consumer: none.
- Promotion trigger: the first route-level `dependencies=[...]` kwarg on any path carrying a GET/HEAD pair.
- `ba`'s RISK NOTE ON THE TRIGGER ITSELF, and it is the reason this entry is not just filed and forgotten: the trigger fires on a **spelling** choice, not on a behaviour. `s-stop-crud` could land a rider-only POST with the guard in the handler signature and never fire it — correctly, since that spelling is safe. Review this at the `s-stop-crud` boundary regardless of whether the trigger fires, because it shares an `app.routes` traversal with `t-route-dependency-audit` and the two would otherwise grow near-identical traversals independently.

## t-access-log-slug-exposure

Split out of `t-error-log-parameter-redaction`'s re-scope on 2026-09-15 as the third of its three slug sinks — the one that is **not application code**. The uvicorn access log records the trip slug for the ordinary reason that the slug is in the request URL: `GET /api/trips/<live-slug>/stops` is logged verbatim, and the slug is the credential.

NOT FIXABLE FROM `backend/`, WHICH IS WHY IT IS A SEPARATE TASK WITH A DIFFERENT AGENT. The logger is `uvicorn.access`, configured by `uvicorn.config.LOGGING_CONFIG` before any application module is imported. The `Dockerfile` passes no log flags and compose overrides no command, so closing this is a uvicorn CLI flag (or a logging-config override supplied at startup) — `devops` scope, not `dev`.

GATE: `triggered`. 
- Finding: the uvicorn access log records live trip slugs via the request URL.
- Evidence: the slug is in the URL by construction; every request to a trip-scoped route logs it. Not disputed and not new — it is the "already in the access log" half of decision-log Entry 13's losing argument, now filed as its own item rather than left implicit.
- Current consumer: **none.** stdout only — no log shipper, no error tracker, no aggregator. Nothing reads the stream, nothing retains it, nothing forwards it off the host.
- Promotion trigger, reused verbatim from Entry 13: **"first error tracker or log shipper configured."** Most likely fired by `s-deploy-cutover` — Azure Container Apps ships container stdout to Log Analytics by default, which turns "stdout only" into "retained and queryable" without anyone choosing it as a logging change.

DECIDED, NOT OVERLOOKED — the part worth keeping. Silencing access logs wholesale is the obvious fix and it is a bad trade: it costs status, method and latency on **every** request, permanently, to remove a URL that is already on the wire and already in any intermediary. The alternative shape — an application module reconfiguring a third-party logger at import time — is worse in a specific recorded way: it is the silently-inert construction Entry 7(b) covers, working or not depending on import order and on whether uvicorn re-applies `LOGGING_CONFIG` after the app loads, with no failure signal either way. So this is filed against a concrete promotion event instead. `backend/app/core/errors.py` carries the same reasoning beside the code, so a reader there does not re-derive "just turn the access log off" and implement it.

## t-api-healthcheck-wiring

From qa's Finding 2 remainder on t-head-on-get-routes. LOW PRIORITY. /api/health exists and NOTHING PROBES IT: docker-compose.yml has healthcheck blocks on postgres and minio but none on the api service, Dockerfile has no HEALTHCHECK instruction, and nothing in infra/ references the path. Filed under s-deploy-cutover rather than s-local-dev-env because the latter is closed and this needs devops plus approval; the compose half is local-dev work and could land earlier if that is preferred.

NEEDS EXPLICIT HUMAN APPROVAL before anything in infra/ is touched (CLAUDE.md off-limits list). The compose and Dockerfile halves are ordinary devops scope; the infra/ half is not.

CARRIES A DOC EXPIRY THAT IS PART OF THE WORK, NOT A NICE-TO-HAVE. dev re-worded the /api/health description= under t-head-on-get-routes so it describes intent rather than asserting configuration — it now says the endpoint is intended as the liveness probe and that NOTHING IS WIRED TO IT YET (no HEALTHCHECK, no compose healthcheck on api). True today, FALSE THE MOMENT THIS TASK IS DONE, and because it is a route description= it feeds the OpenAPI document, Kubb's client and every agent's context. The stale text lives in backend/app/main.py — application code, outside devops's scope (Dockerfile, docker-compose.yml, infra/ only) and outside the docs agent's docs/-only tree. So this task needs a dev follow-up in the same patch for that string, or devops stops and hands it back; it is not something whoever does the wiring can just edit in passing.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: `/api/health` exists and nothing probes it — no `HEALTHCHECK` in the Dockerfile, no healthcheck on the compose `api` service, nothing in `infra/` referencing the path.
- Evidence: QA enumerated all three. No requirement mandates a probe before deployment, and the route's own `description=` was already re-worded to say that nothing is wired to it yet, so the documentation is true rather than stale.
- Gate classification: TRIGGERED DEBT (Gate 3).
- Current consumer: none — that is the finding.
- Promotion trigger: `s-deploy-cutover`.
- BOTH EXISTING CONDITIONS ABOVE SURVIVE THIS CLASSIFICATION UNCHANGED. (1) The `infra/` half needs explicit human approval per the CLAUDE.md off-limits list — **a gate classification is not clearance**, and a fired trigger is not approval either. (2) The `/api/health` `description=` in `backend/app/main.py` becomes false the moment this lands, and it feeds the OpenAPI document and the generated client, so it needs a `dev` follow-up in the same patch or `devops` stops and hands it back.

## t-add-endpoint-head-convention

One line in .claude/skills/add-endpoint/SKILL.md step 4: a GET route declares ["GET", "HEAD"]. That is the whole task. Its point is that the convention reaches ALL of its sites rather than only the two t-head-on-get-routes touches — the skill is the recipe every future endpoint is built from, so a convention absent from it is a convention that stops at whichever routes existed on the day it was decided. That is Entry 5's failure mode (a convention applied to one file out of six reads as significant when it is merely incomplete). Filed under s-data-layer-foundation alongside t-head-on-get-routes because it is the same decision's second half. Sequence it with or after that task; writing the recipe before the pattern exists in the codebase leaves the skill pointing at nothing.

FLAGGED 2026-09-14, NOT FIXED — THIS TASK CANNOT BE EXECUTED BY ITS ASSIGNED AGENT. It is recorded with agent: "docs", but its target is .claude/skills/add-endpoint/SKILL.md, which is OUTSIDE docs/ — the only tree the docs agent can write to. Whoever picks this up must reassign it rather than rediscovering the wall; the roster is not being changed here, and the docs agent did not attempt the edit. Also note the CONTENT changed under it: t-head-on-get-routes did NOT ship methods=["GET", "HEAD"] (decision-log Entry 11), so the line to add to step 4 is a HEAD SIBLING REGISTRATION — add_api_route(..., methods=["HEAD"], include_in_schema=False) alongside the GET — and must say WHY include_in_schema=False is load-bearing, not just that it is there. Writing the original spelling into the skill would propagate the exact construction Entry 11 rejected to all seven remaining endpoints, which is this task's own stated failure mode in reverse. The task title in progress.json was corrected accordingly.

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`.
- Finding: the `add-endpoint` skill's step 4 does not mention the HEAD sibling registration, so the convention stops at whichever routes existed on the day it was decided.
- Evidence: the skill is the recipe every future endpoint is built from, and seven contract endpoints remain unbuilt. Nothing is currently wrong — the two routes that exist already carry the sibling registration.
- Gate classification: TRIGGERED DEBT (Gate 3).
- Current consumer: none — no endpoint has been built from the skill since the convention was decided.
- Promotion trigger: the next endpoint built from the skill (`s-stop-crud`). The recipe must be right *before* `dev` follows it, not after — a skill corrected afterwards has already propagated the wrong construction.

UNBLOCKED 2026-09-15 (`t-backlog-retro-triage`). `agent` changed `"docs"` -> `"orchestrator"` and the blocker emptied. Nothing about the task's content changed — the correction recorded above still stands in full. What unblocked it is `CLAUDE.md`'s governance-ownership rule (landed by `t-claude-md-triage-gate`, decision-log Entry 12b): `.claude/skills/` has no agent owner and the orchestrator writes it, so the "Needs reassignment" blocker now has somewhere to point. The docs agent's refusal to edit outside `docs/` was correct and is not being reversed here; the owner was missing, not the permission.

DONE ALL ALONG — TRACKER CORRECTED 2026-09-16, NO NEW WORK. The status field read `not_started` while the work sat in the tree. `.claude/skills/add-endpoint/SKILL.md` step 4 carries the convention as a sub-bullet under the route-handler step: a `GET` route gets a second registration of the *same handler* via `add_api_route(..., methods=["HEAD"], include_in_schema=False)`, explicitly NOT `methods=["GET", "HEAD"]` on one route, with the `get_openapi_path`-loops-over-`route.methods` / per-route-`operation_id` reasoning for why `include_in_schema=False` is the part doing the work, the note that reusing the handler object is what keeps the access dependency in sync across the pair, and a citation of decision-log Entry 11. So the CONTENT correction recorded above was honoured — the skill teaches the spelling that shipped, not the one Entry 11 rejected.

The promotion trigger fired as predicted: `t-stops-list-endpoint` landed at `3ebded7` and does carry a HEAD sibling registration, so the recipe was right *before* `dev` followed it rather than corrected afterwards.

WHAT THIS ENTRY IS ACTUALLY RECORDING is the bookkeeping failure, not the convention. A task whose output lives outside every agent's writable subtree is exactly the one that gets done in passing by the orchestrator and never checked off — and this task had already been reassigned `docs` -> `orchestrator` for that same structural reason. The reassignment fixed who could do it; nothing fixed who would mark it done.

## t-finding-triage-gate-doc

TASK A of four. Scope: docs/ ONLY — docs/finding-triage-gate.md (new, the verbatim rule), docs/decision-log.md (Entry 12 plus its index row), and these tracker entries. The rule text is USER-AUTHORED AND FIXED: transcribed, not edited. If this task appears to need CLAUDE.md or .claude/**, it does not — those are tasks B and C, and the reason they are separate is Entry 12b, not convenience. Entry 12 records BOTH contested points in one entry because the second is why the first could not simply be written into CLAUDE.md.

The evidence in Entry 12(a) was measured against progress.json as it stood, not asserted: 13 of the 15 open tasks were in s-data-layer-foundation while all four m2-core-api endpoint stories (s-stop-crud, s-photo-upload-onedrive-sync, s-bike-management, s-map-geojson-endpoint) were still not_started; and t-head-on-get-routes spent a full dev->test-writer->qa->docs cycle on behaviour no client exercises, spawning two further tasks in the process. If those numbers are re-checked later they will have moved — they are a dated reading, not a live claim.

## t-claude-md-triage-gate

TASK B. Orchestrator-executed: CLAUDE.md is outside every agent's write scope, and that is a recorded decision (decision-log Entry 12b), not a gap to route around. Two edits. (1) Cite docs/finding-triage-gate.md as the rule for classifying any discovered issue before implementing it — CITE, do not restate the rule, because a second copy of fixed text is a copy that drifts from the canonical one. (2) Record in the agent-roster section that CLAUDE.md, .claude/agents/ and .claude/skills/ are owned by the orchestrator, with the one-line reason (an agent that can write .claude/agents/ can rewrite its own and its peers' tools: grants). That second edit is Entry 4's rule applied to Entry 12b: the rationale must sit where the "just widen docs by one glob" fix would be attempted. Also note that Session 0's carve-out in CLAUDE.md now has a different justification than the one written there — the original (nothing existed to dispatch to) has expired; the durable one is that delegating these writes delegates the permission boundary.

## t-agent-defs-triage-gate

TASK C. Orchestrator-executed, same reason as B. Puts the gate where findings are actually produced and acted on: qa (classify every finding in the report, with the five-field evidence template — Finding / Evidence / Gate classification / Current consumer / Promotion trigger), dev (do not silently re-triage a QA classification; use the four-field disagreement protocol, and apply the Scope Rule to anything discovered mid-task), ba (a finding is a candidate task only after it passes the gate; a Gate 3 item is filed WITH its trigger). CITE the canonical file, do not paste the rule into three agent definitions — three copies of fixed text is three things to keep in sync, which is Entry 5's failure mode wearing a different hat. Sequence after B so the agent definitions point at a CLAUDE.md that already references the gate.

## t-backlog-retro-triage

TASK D, LAST. ba analyses, docs writes — ba is read-only, so its classification comes back as a report and docs records it in the tracker. Applies the gate retroactively to every open task in progress.json, producing for each: gate classification, current consumer (specific or none), promotion trigger (specific or none). Expect most of s-data-layer-foundation's 13 open tasks to land in Gate 3 or Gate 4; that is the point of running it.

THE STOP CONDITION APPLIES TO THIS TASK'S OWN OUTPUT, AND THIS IS THE INSTRUCTION MOST LIKELY TO BE VIOLATED IN GOOD FAITH. NO TASK CLASSIFIED HERE MAY BE IMPLEMENTED IN THIS PATCH OR THE NEXT. Re-reading a backlog item closely enough to classify it is exactly the state in which "while I'm here" happens, and a retro-triage that ends with three fixes has demonstrated the gate does not bind its own author. Classification is the deliverable; the fixes are whatever the next story boundary promotes. If the analysis turns up something that genuinely satisfies Gate 1 or Gate 2 — a live contract violation or a current consumer — it is still filed and reported here, not fixed in this patch, because the evidence for CURRENTLY BROKEN should survive being written down and reviewed before anyone edits production code on the strength of it.

COMPLETED 2026-09-15. `ba` classified all 15 open tasks; `docs` recorded them. **NOTHING WAS IMPLEMENTED, INCLUDING THE ONE ITEM CLASSIFIED FIX NOW** — the Stop Condition binds this patch, and the warning above was the instruction most at risk, so it is worth recording that it held rather than assuming it.

RESULT: 3 ORDINARY DEBT, 11 TRIGGERED DEBT, 1 CURRENTLY OBSERVABLE, **zero CURRENTLY BROKEN, zero needing more evidence.**

`gate` VALUES IN `progress.json` — the allowed set is exactly four, one per gate: `"broken"` (Gate 1, CURRENTLY BROKEN), `"observable"` (Gate 2, CURRENTLY OBSERVABLE), `"triggered"` (Gate 3, TRIGGERED DEBT), `"ordinary"` (Gate 4, ORDINARY DEBT). Every Gate 3 item's note names a concrete **event**, never a date — "revisit at the next story boundary" is not a trigger, and a calendar review is explicitly ruled out by the gate's Debt Promotion clause.

ZERO NEEDS-EVIDENCE IS A RESULT, NOT A DEFAULT. It would have been legitimate — and expected — for several items to come back "cannot classify, needs evidence." None did, because the existing notes already carried what the gate asks for: what was observed, by whom, and whether anything reaches it. Two came closest, and how each resolved is the useful part:
- `t-openapi-error-responses` — the question was whether the generated frontend client counts as a consumer. It resolved on a checkable fact rather than a judgment call: all of `m3-frontend-pwa` is `not_started`, so no component imports a generated type. `Could consume ≠ currently consumes`. TRIGGERED DEBT.
- `t-validation-message-offset` — the question was whether a defect that is already rendered counts as observable. Resolved the same way: every registered route is a GET and none takes a request body, so no client can reach the branch. The defect is real and unreachable. TRIGGERED DEBT.

ONE CONTESTED CALL: `t-error-log-parameter-redaction`, decision-log **Entry 13**. Classified CURRENTLY OBSERVABLE against its own note's argument for ORDINARY DEBT. Its scope also changed (it closes one of two sinks) — that is a scope change, not a reclassification.

FLAGGED FOR THE USER, NOT RESOLVED HERE: adding `gate` extends the `progress.json` shape sketched in **spec Section 11**, which lists `{ "id", "storyId", "title", "status", "agent", "blockers": [] }` and no `gate`. `CLAUDE.md` already says a finding-derived task carries the field, so the two now disagree and the spec is the one that is stale. The spec was not edited — it is the user's document and outside this task's scope. Either the sketch gains the field or `CLAUDE.md` is wrong; that is a user call.

## t-stops-405-doc-revisit

Both documents currently state that DELETE on that path returns 404 because the stops router is an empty stub with no methods to mismatch against. True today, false the moment GET/POST /trips/{slug}/stops register — at which point it becomes 405, assuming t-405-router-route-collapse has landed. Entry 6's claim is load-bearing there (it records QA catching a false illustration the orchestrator introduced), so it gets a dated footnote rather than a rewrite. FOLDED IN 2026-09-14 (no new id — this task already owns a dated revisit of the same paragraph). t-head-on-get-routes falsifies the "GET-only" wording in TWO places, and this note names both deliberately: a revisit task that lists one of two sites gets exactly that one site fixed and is then closed, which is Entry 5's failure mode (a convention reaching only some of its sites).

SITE 1 — docs/api-contract.md:91: 'POST /api/health hits a real, GET-only route and returns a 405'.
SITE 2 — docs/decision-log.md Entry 6, around line 214, in the 'The contradiction ba found' paragraph: 'a 405 (POST /api/health hits a real, GET-only route)'.

Same construction, same falsifier. Once t-head-on-get-routes lands, /api/health is GET+HEAD and the words "GET-only" are false at both sites.

THIS IS WORDING-ONLY AT BOTH SITES — the conclusions are untouched and must not be rewritten. POST is in neither method set (GET-only or GET+HEAD), so POST /api/health still returns 405, the 405 is still reachable, and METHOD_NOT_ALLOWED is still a necessary ErrorCode member. Entry 6's decision — five codes, escalated rather than invented — is unaffected in every part. Re-word, do not re-argue: the risk here is a reader seeing a falsified sentence inside a load-bearing entry and over-correcting a conclusion that was never wrong.

HOW each site gets fixed differs, because the two files have different conventions. Site 1 is a plain contract document: re-word in place. Site 2 is the decision log, whose own convention — established by Entry 6's correction and followed by Entries 7 and 8 — is that corrections are APPENDED AND DATED, never silently merged into the original text. So Entry 6 gets a dated footnote recording that its illustration's "GET-only" clause expired when t-head-on-get-routes landed, appended below the existing 2026-09-14 footnote. Do not edit Entry 6's body.

DO NOT PRE-EMPTIVELY EDIT EITHER SITE. Both are correct as they stand until t-head-on-get-routes actually lands; editing now would make them describe a codebase that does not exist, which is the mistake Entry 6 exists to record. All three edits owned by this task — the original DELETE /trips/{slug}/stops illustration plus these two — land together at s-stop-crud, or earlier if t-head-on-get-routes lands first.

TRIGGER FIRED 2026-09-14: t-head-on-get-routes LANDED, so /api/health is now GET+HEAD and the "GET-only" wording at SITE 1 and SITE 2 is false as of today. This task is unblocked and the two wording edits can be made now rather than waiting for s-stop-crud; the third (the DELETE illustration) still waits for the stops router. Nothing above was pre-emptively edited. Decision-log Entry 6's INDEX ROW has been updated to say the first expiry fired — the index is a live summary the docs agent owns — but Entry 6's BODY is untouched and still gets an appended dated footnote, never an in-place rewrite, per its own convention. Re-word, do not re-argue: POST is in neither GET-only nor GET+HEAD, so the 405 remains reachable and every conclusion stands.

TWO OF THREE EDITS LANDED 2026-09-15 (commit `30d2e35`). SITE 1 (`docs/api-contract.md:91`) was re-worded in place, and SITE 2 (decision-log Entry 6) received a dated appended footnote below the existing 2026-09-14 one — Entry 6's body untouched, per its own convention. **THE TASK STAYS OPEN.** The third edit — the `DELETE /trips/{slug}/stops` -> 404 illustration at both its sites — still waits for the stops router to register. Per Entry 6's own footnote, the observed status at that point must be **verified** rather than assumed: the claim is that it becomes 405 once the router registers, and `t-405-router-route-collapse` has landed, but that is a prediction until someone sends the request. Closing this task on the two wording edits is exactly the failure mode the note above names (a revisit task that lists one of two sites gets one site fixed and is then closed).

GATE TRIAGE 2026-09-15 (`t-backlog-retro-triage`) — `gate: triggered`. **TRIGGER ALREADY FIRED, PARTIALLY DISCHARGED.**
- Finding: two documents state that `DELETE` on the stops path returns 404 because the stops router is an empty stub, and one illustration described `/api/health` as GET-only.
- Evidence: the GET-only half was falsified when `t-head-on-get-routes` landed 2026-09-14, and both its sites are now fixed (above). The DELETE half is still true today — the stops router genuinely has no methods to mismatch against.
- Gate classification: TRIGGERED DEBT (Gate 3), with one trigger fired and discharged and one outstanding.
- Current consumer: none — both documents are correct as they stand for the part that remains.
- Promotion trigger (outstanding): the stops router registering `GET`/`POST` on `/trips/{slug}/stops` at `s-stop-crud`. Do not pre-emptively edit the DELETE illustration before then; editing early makes it describe a codebase that does not exist, which is the mistake Entry 6 exists to record.

STATUS CORRECTED 2026-09-15: `not_started` -> `in_progress`. Two of this task's three edits shipped in commit `30d2e35`; a task that has landed two thirds of its work is not un-started, and leaving it `not_started` invited someone to pick it up and redo SITE 1 and SITE 2. It is not `done` either — the DELETE illustration is the third edit and still waits on the stops router, which `t-stops-list-endpoint` is registering now.

THIRD EDIT LANDED — TASK `done` 2026-09-15. The outstanding trigger fired: `t-stops-list-endpoint` registered `GET` (and its `HEAD` sibling) on `/trips/{slug}/stops` in `3ebded7`, so the `DELETE` -> `404` illustration became false and both its sites are now fixed. `docs/api-contract.md` was re-worded in place — the paragraph *and* the 2026-09-14 forward-note beneath it, which had fired and was still reading as a future warning — and decision-log Entry 6 received a second dated 2026-09-15 footnote with its body untouched, per its own append-only convention. Entry 6's index row updated to record both expiries fired.

THE OBSERVED STATUS WAS VERIFIED, NOT ASSUMED — which is the condition this note set for closing. `test_spa_delete_on_the_registered_stops_path_is_405` asserts `405` with `Allow` exactly `{"GET", "HEAD"}`, and it passes; the set becomes `{"GET","HEAD","POST"}` when `t-stops-create-endpoint` lands. The verification mattered because `t-405-router-route-collapse` landing is what stops a registered path silently staying `404` for the wrong reason, and a stale-for-the-wrong-reason `404` is indistinguishable from a correct one from outside. Wording only at all three sites: `qa`'s original `404` measurement was correct for the codebase it was taken against, and the rule (a `405` requires a registered path with a different method) did not expire — only the example did.

## t-trip-metadata-endpoint

First endpoint in the app; sets patterns seven more will copy. Bike repository lands HERE, not separately: t-slug-access-dependency ruled a bike join out of the slug dependency because TripOut.bikes serves one endpoint of eight — that reasoning was about the dependency, which runs on all eight, and does not transfer to the one endpoint that needs it. TripOut.bikes has no default, so the alternative is a hardcoded [] that passes only while the bikes table is empty and silently lies once POST /bikes lands (decision-log Entry 7b). snake_case->camelCase mapping is explicit keyword construction in two places: repositories/bikes.py builds BikeOut at the SELECT site (riderName=row.rider_name), the handler builds TripOut from TripContext (startDate=context.trip.start_date). Ruled out deliberately so later endpoints don't re-derive: no alias_generator/populate_by_name (the contract models already spell startDate, an alias layer is a second encoding of the wire name), and no **asdict(record) spread. Folded in from t-trip-context-slug-exposure: the route-local assertion that neither slug appears in this endpoint's 200 body — the existing test_no_slug_appears_in_any_response_body covers probe routes only. That task STAYS OPEN for the generalised all-routes guard landing with t-route-dependency-audit at s-stop-crud; do not close it on this patch. Folded in from t-openapi-error-responses: responses={404: ErrorEnvelope} on this route only, as this endpoint's own contract completeness — one route referencing ErrorEnvelope also makes components.schemas non-empty so Kubb can see the type. That task stays open for the shared constant across eight routes and the decision on FastAPI's auto-injected, here-unreachable 422 on the path param; dev must NOT improvise that (Entry 5). t-route-dependency-audit confirmed NOT triggered: it guards against a write route declaring require_trip_access, not representable on a read-only route. The inverse (this GET declaring require_rider_access) is loud, not silent, and is covered by AC4. Also closes the missing description= on TripOut's four fields and all of BikeOut; BikeCreate/BikePatch stay with s-bike-management. Eight mutations enumerated in the task must each be shown to fail a named test per Entries 2 and 7a. seeded_bikes fixture must INSERT rows on both trips rather than relying on an empty table (Entry 7b). Completed. QA verified all 10 criteria end-to-end over real uvicorn with the production get_session (not the test override) — route -> require_trip_access -> session -> trips repo -> bikes repo -> response. Two queries per request confirmed correct, not an N+1: FastAPI's dependency cache resolves Depends(get_session) once across the dependency and the handler (verified by object identity), the bikes query never runs on the 404 path, and EXPLAIN at 200 bikes is 0.3ms. A join was explicitly rejected — it would make seven routes pay for bikes they discard, or force the handler to re-resolve the slug and fork the 403/404 rule. 15 hostile slug inputs (NUL, invalid UTF-8, 65000 chars, encoded slashes) all returned clean 404 envelopes. Concurrent trip deletion between the two queries yields a self-consistent 200 with bikes:[] and no cross-trip leak.

## t-conflict-code-contract

Contract-first half of the user's `CONFLICT`/`409` ruling: `docs/api-contract.md` + decision-log Entry 14 + these tracker entries. No code. Written and reviewed BEFORE `dev` opens `common.py`, which is the build-order gate working as intended rather than a scheduling accident — Entry 6 exists because `ba` refused to make a contract change by writing it into a task description, and Entry 14 is the same refusal a second time.

NO `gate` FIELD ON ANY OF THE FOUR `t-conflict-*` TASKS, DELIBERATELY. The gate governs a finding an agent discovered and might self-authorise into work (`docs/finding-triage-gate.md`). This did not originate as a finding: it is a contract gap the user ruled on directly. That is AUTHORISATION, not triage — there is nothing to classify, and stamping a gate on it would misrepresent a user decision as a promoted backlog item. Do not "fix" the missing field.

SCOPE COVERS ALL THREE CREATE ENDPOINTS, NOT JUST STOPS. `stops`, `photos` and `bikes` each carry a global Text primary key plus a separate parent FK (`tables.py:72-73`, `:101-102`, `:115-116`), so all three can collide across parents and the contract's Idempotency section already named all three. The rule is stated ONCE. Only `POST /stops` gets an implementation task now; photos and bikes land with their own endpoints. One wording note: the lookup pair is `(trip_id, id)` for stops and bikes but `(stop_id, id)` for photos, since a photo's parent is the stop — the contract says "parent-and-id pair" and names both spellings rather than asserting `trip_id` for all three.

The contract's old sentence "Do not return a conflict error" was not careless when written — it was true of the replay scenario it was reasoned about, and it went wrong by being stated unqualified. Recorded in Entry 14 as a failure mode of contract prose, not as an error to apportion.

COMPLETED 2026-09-15 (commit `a58bec2`). `docs/api-contract.md` carries the `CONFLICT`/`409` rule and the cross-parent create branch, decision-log Entry 14 and its index row are in, and the tracker entries are filed. Contract only, no code — `t-conflict-409-test-repoint`, `t-conflict-code-impl` and `t-conflict-code-tests` remain open in that order, and until the impl lands a `409` still falls through to `INTERNAL_ERROR`.

## t-conflict-409-test-repoint

RUNS BEFORE THE IMPLEMENTATION, AND THAT ORDER IS THE WHOLE POINT. Two shipped assertions use 409 as a stand-in for "a status the contract does not name", expecting it to fall through to INTERNAL_ERROR: `backend/tests/test_error_envelope.py:394` (`test_direct_construction_still_allows_every_contract_pair`) and `:424` (`test_http_exception_maps_to_contract_code`). Both go red the moment 409 enters `_STATUS_TO_CODE`. Re-pointing them to 418 is green BEFORE the impl (418 is unnamed today) and green AFTER (418 is still unnamed), so the tree works at every step — CLAUDE.md's commit rule. Doing it in the same patch as the impl, or after it, means a commit that leaves a red suite. The tests are not wrong and are not being deleted: what they assert — an unnamed status falls through to INTERNAL_ERROR — stays true and stays covered; only the example changes.

COMPLETED 2026-09-16 (commit `f2f12ce`), and it landed FIRST of the four as required. The commit precedes `7ca82ab` (the impl), so the mandated ordering held in the history and no commit in the sequence left a red suite.

## t-conflict-code-impl

`backend/app/models/common.py` + `backend/app/core/errors.py` ONLY. Add `CONFLICT = "CONFLICT"` to `ErrorCode`, add `HTTPStatus.CONFLICT: ErrorCode.CONFLICT` to `_STATUS_TO_CODE`, and fix the two prose claims that count to five: the `ErrorCode` docstring ("five, no more") and the `_STATUS_TO_CODE` comment ("the contract defines exactly five codes"). No repository or route work — `POST /stops` is `t-stops-create-endpoint`.

Adding the mapping row is also what makes `ApiError(409, CONFLICT, ...)` constructible: `__init__` checks the pair against `code_for_status`, so without the row every 409 construction raises. That backstop is a `ValueError`, DELIBERATELY NOT AN `assert` — `assert` is stripped under `python -O`, silently disabling the check in exactly the optimised build where a contradictory body goes unnoticed. Decision-log Entry 7's body calls it "an `__init__` assertion"; the code and `t-error-envelope-handlers` above are the accurate record. Do not follow Entry 7's wording. An `ApiError.conflict(message)` classmethod belongs with this task if it is added at all, per errors.py's own "raise through the classmethod constructors" rule.

CO-LOCATION IS PART OF THE WORK, NOT TIDY-UP (Entry 4's rule). Entry 14's rationale must reach three files the `docs` agent cannot write: beside the new member in `common.py` (why not VALIDATION_ERROR; never-retry for the queue), beside `_STATUS_TO_CODE` in `errors.py` (409 is endpoint contract, not framework-level like 405/500), and — when the create endpoint lands — beside the create path in `repositories/stops.py`, which is THE file where the rejected readings would actually be retried by someone collapsing a two-step check into one insert.

COMPLETED 2026-09-16 (commit `7ca82ab`), after the test re-point as sequenced. From this commit on a `409` renders a `CONFLICT` envelope instead of falling through to `INTERNAL_ERROR`. That is also what produced `t-bare-409-envelope-bypass`: the mapping is keyed on status, so a bare `HTTPException(409)` now receives the code without passing through `ApiError.conflict` — the envelope is well-formed and the leak boundary was never consulted. That is a consequence of the design, not a defect in this patch; the mapping row is the contract.

## t-conflict-code-tests

Covers the sixth member and its mapping: 409 -> CONFLICT through `code_for_status`, through a raised `StarletteHTTPException`, and through `ApiError`; the transposed-pair backstop still raising `ValueError` for a 409 carrying any other code; and the exhaustiveness assertion updated to six. Per Entries 2 and 7a each new assertion must be shown to FAIL against a mutant — a mapping test that passes with the row deleted is the vacuous-guard failure mode. The message leak boundary (a CONFLICT message containing no value from the conflicting record) is enforced at the raise site, so it is tested with the create endpoint under `t-stops-create-endpoint`, not here — there is no raise site in this patch.

COMPLETED 2026-09-16 (commit `13540ac`), closing the four-task sequence in the order it was specified: contract `a58bec2` -> test re-point `f2f12ce` -> impl `7ca82ab` -> tests `13540ac`. The message leak boundary is still untested, by design and not by omission — it is enforced at a raise site that does not exist yet. `qa` raised two findings on this task, both filed rather than fixed: `t-bare-409-envelope-bypass` and `t-errors-docstring-dangling-line`.

## t-stops-list-endpoint

GET /api/trips/{slug}/stops — either slug, `StopOut[]`, plus the stops repository. IN FLIGHT at the time this note was written and NOT PREVIOUSLY IN THE TRACKER — added retroactively on 2026-09-15 so the tracker matches reality; `backend/app/data/repositories/stops.py` was already untracked in git. Follows `t-trip-metadata-endpoint`'s patterns by design: explicit keyword snake_case->camelCase construction at the SELECT site, no alias_generator, no `**asdict()` spread, `responses={404: ErrorEnvelope}` on the route, and a HEAD sibling registration for the GET (Entry 11). Registering this route is what makes the stops path exist for method-mismatch purposes, which fires `t-stops-405-doc-revisit`'s outstanding edit.

COMPLETED 2026-09-15 (commit `3ebded7`), 28 tests, suite green at 219. The predicted downstream effect happened as described: registering the route made `/trips/{slug}/stops` a real path for method-mismatch purposes, so `DELETE` on it now returns `405` with `Allow: GET, HEAD` instead of `404` — pinned by `test_spa_delete_on_the_registered_stops_path_is_405` rather than left as a prediction. That fired and discharged `t-stops-405-doc-revisit`'s third and last edit, closing it.

## t-stops-create-endpoint

POST /api/trips/{slug}/stops — rider slug only. NO LONGER BLOCKED: the cross-parent id question that `ba` escalated is ruled (decision-log Entry 14, contract §Idempotency). Three-way branch on the client-generated id: unseen -> 201; exists under THIS trip -> 200 replay with the stored row; exists under a DIFFERENT trip -> 409 CONFLICT, nothing created. Replay lookup is on `(trip_id, id)`, never `id` alone (that returns another trip's stop through this trip's slug). The cross-trip branch is found BY A CHECK, never by letting the INSERT fail — a driver PK violation renders 500 INTERNAL_ERROR and the offline queue retries that forever. The 409 message must carry no value from the conflicting record (not the other trip's slug/id/name, not the other stop's name/notes/coordinates/timestamp), enforced at the raise site. This is the FIRST WIRED WRITE ENDPOINT, so it is the promotion trigger for `t-route-dependency-audit` and `t-trip-context-slug-exposure` — both must land with it.

SEQUENCING ADDED 2026-09-16: `t-stopcreate-field-contract` lands BEFORE this task. It changes `StopCreate` — the request model this endpoint binds — so taking it afterwards means shipping the endpoint against a model whose `arrivedAt` accepts a naive datetime and whose five fields carry no `description=`, then immediately reopening the same file. `t-stopcreate-tz-tests` follows the contract task; it may land either side of this one.

## t-arrivedat-tz-contract

Contract half of the `arrivedAt` timezone-awareness question — the `api-contract.md` section plus decision-log Entry 15. No code, same shape as `t-conflict-code-contract`, and the same build-order gate: the contract is written and reviewed before `dev` opens `backend/app/models/stop.py`. WRITTEN AND AGREED, COMMIT PENDING at the time of this entry (2026-09-16) — it is `done` in the sense that the text exists and is settled, alongside the other uncommitted doc work in this pass. The ENFORCEMENT half is `t-stopcreate-field-contract`; this task deliberately touches no model.

## t-stopcreate-field-contract

MERGED FROM TWO PREVIOUSLY SEPARATE PIECES, DELIBERATELY. (1) `StopCreate.arrivedAt` becomes `AwareDatetime`. (2) The five bare fields — `name`, `lat`, `lng`, `arrivedAt`, `notes` — get real `description=` values. Same file, same class, and for `arrivedAt` the same field. The stronger reason is that the old description criterion INVERTS: it used to be that the description must NOT claim timezone-awareness, because the type did not enforce it; it now must STATE it. Landing the claim and its enforcement in separate diffs guarantees one commit where the prose and the type disagree, in whichever order they arrive. One diff.

`lat` and `lng` need the GeoJSON `[lng, lat]` reversal warning that `StopOut` already carries — the wire order on this model is not the GeoJSON order the map endpoint emits. Copy the existing wording rather than re-deriving it; two spellings of one warning is Entry 5's failure mode.

`StopOut` IS UNCHANGED BY THIS TASK. Its fields are already described, and its `arrivedAt` is a response field rather than a parse boundary.

CO-LOCATION OWED IN `backend/app/models/stop.py` (Entry 4's rule; outside the `docs` agent's tree, so whoever implements carries it). The rationale that must live beside the field: the emitted JSON Schema is IDENTICAL whether `arrivedAt` is `AwareDatetime` or a bare `datetime`, so a future reader diffing the OpenAPI document will conclude the aware type buys nothing and relax it. That reading is TRUE AND BESIDE THE POINT. The difference is at parse time, not in the document — `AwareDatetime` rejects a naive value, a bare `datetime` accepts it and hands it to a `timestamptz` column, where the session `TimeZone` decides what hour it meant. A stop filed at the wrong hour is the failure, and it is invisible in the schema. That is precisely why the note belongs in the file and not only here.

COMPLETED 2026-09-16 (commit `459b26f`). `arrivedAt` is `AwareDatetime`; all SEVEN fields carry a `description=` — the scoping note above said five, counting only the bare ones and overlooking that `id` and `locationSource` were already described. Seven is the number the ratchet test enumerates, so the task title now says seven. The co-location note landed in `backend/app/models/stop.py` above the field, as owed.

THE 422 REACHES THE WIRE, AND THE ENDPOINT TABLE GENUINELY DOES NOT CHANGE. QA confirmed the model-layer rejection surfaces as `422` / `VALIDATION_ERROR` inside the contract's error envelope, not as a 500 or a bare pydantic body. The evidence that settles it is the CONTROL: a payload missing `lat` — a plain required-field failure that predates this task and that nobody claims changes the contract — produces the SAME `422` envelope shape. Naive `arrivedAt` is therefore not a new response class, it is an existing one with a new cause, which is why `docs/api-contract.md`'s endpoint table needed no edit. Without the `lat` comparison the `422` observation alone would not have shown that.

MUTATION-CHECKED, BOTH GUARDS (Entries 2 and 7a). Flipping `AwareDatetime` back to a bare `datetime` turns `test_naive_arrived_at_is_rejected` red — the exact relaxation the co-located comment warns against is the one the suite catches. Adding an eighth field with no `description=` turns the ratchet red, so it guards the NEXT field rather than today's seven.

CONSIDERED AND DECLINED, DELIBERATELY NOT FILED AS A TASK: `arrivedAt` accepts a bare JSON number as epoch seconds. That is neither ISO 8601 nor offset-carrying on the wire, so it contradicts the field's own description — QA raised it, recommended against making it work, and the orchestrator agreed. It is not work for two independent reasons. (1) Epoch seconds are UTC BY DEFINITION, so there is no ambiguous hour: the wrong-hour data-integrity failure Entry 15 exists to prevent is not reachable through this door, which is the only thing the aware-datetime rule is defending. (2) Kubb types the field `string`, so the generated client cannot emit a number even if someone wanted to. NO TASK WAS CREATED ON PURPOSE — there is no honest promotion trigger to attach (nothing observable would fire), and a triggerless backlog entry reads as coverage that is not real. Same reasoning as `t-docs-agent-unscoped-grant`'s deliberately-absent trigger. This note exists so the observation is not rediscovered and re-triaged as if it were new; if it is ever reopened, the burden is to show one of the two reasons above no longer holds.

## t-stopcreate-tz-tests

Test half of `t-stopcreate-field-contract`, blocked on it. CONTRACT-FIRST rather than implementation-following, because a stop recorded at the wrong hour is a data-integrity failure — spec Section 12's top priority tier. The assertions come from the contract text (`t-arrivedat-tz-contract`), not from whatever `stop.py` ends up doing. Per Entries 2 and 7a every assertion must be shown to fail against a mutant, and the mutation that matters is named in the co-location note above: relaxing `AwareDatetime` back to a bare `datetime` must turn a test red. If it does not, the guard is vacuous against the exact change someone will one day make for a real and plausible-sounding reason.

COMPLETED 2026-09-16 (commit `a4bdb94`). New file `backend/tests/unit/test_stop_model.py`, 5 tests, suite green at 239. The named mutation was run and both guards fail against their mutant: `AwareDatetime` → bare `datetime` kills the naive-rejection test; an added undescribed field kills the description ratchet. So neither assertion is vacuous against the change it exists to catch.

PLACEMENT — `tests/unit/`, NOT the root. `test-writer` cited `backend/tests/README.md:3`, which reserves `unit/` for "stop/photo/bike creation and validation"; this is stop creation and validation. That is the first use of the directory and it discharges most of `t-tests-readme-stale` — see that entry.

TWO ASSERTIONS DELIBERATELY NOT MADE, both recorded in the test module's own docstring so they are not read as gaps: the OpenAPI-document half of the ratchet (no route references `StopCreate` yet, so it is absent from `components.schemas` — that belongs to `t-stops-create-endpoint`), and timezone-awareness via the JSON Schema (`AwareDatetime` and a bare `datetime` emit identical schema, so such a test would be GREEN against the exact regression this file exists to catch).

## t-takenat-tz-question

FILED, NOT SCOPED — no acceptance criteria, per the gate's rule for an open question. Created urgently for a bookkeeping reason rather than an implementation one: `docs/api-contract.md` and `docs/decision-log.md` both already cite this id, so until the entry existed those were dangling references.

- Finding: `PhotoCreateForm.takenAt` accepts naive datetimes and carries no `description=`, while the `photos.taken_at` column is `timestamptz`.
- Gate classification: TRIGGERED DEBT (Gate 3).
- Current consumer: NONE. No route imports `PhotoCreateForm` — the model exists and nothing binds it.
- Promotion trigger: when `POST /trips/{slug}/stops/{id}/photos` is scoped under `s-photo-upload-onedrive-sync`.

WHY THIS MAY NOT GET THE SAME ANSWER AS `arrivedAt` — the reason it is filed separately instead of folded into Entry 15. `arrivedAt` is supplied by the app at capture, so "reject naive" is a satisfiable rule. `takenAt` comes from EXIF, where `DateTimeOriginal` IS NAIVE BY DESIGN: the UTC offset lives in a separate `OffsetTimeOriginal` tag that is frequently absent on imported, edited or re-encoded images. So "reject naive" may be unsatisfiable for a large share of real photos — the offset genuinely is not in the file — and the answer may instead be that the FRONTEND SUPPLIES THE DEVICE OFFSET AT CAPTURE, with the contract saying so. Do not close this by pointing at Entry 15 and copying the `arrivedAt` ruling across; the input is a different kind of input.

## t-photoout-field-descriptions

TRIGGERED DEBT, filed not scoped. 4 of `PhotoOut`'s 6 fields carry no `description=`, against CLAUDE.md's rule that every Pydantic field gets a real one — the descriptions feed the OpenAPI document, which feeds Kubb's generated frontend types and every agent's context.
- Current consumer: none. No route publishes `PhotoOut` into OpenAPI yet.
- Promotion trigger: the first route that publishes the model into the OpenAPI document — the photo endpoints under `s-photo-upload-onedrive-sync`. Nothing is wrong until the model is published; an undescribed field in an unpublished model reaches no client.

NOT FILED, AND DELIBERATELY SO: `TripOut`, `BikeOut` and `StopOut` are complete and already guarded by ratchet tests. Do not open tasks for them — a task to fix something already fixed and pinned reads to the next person as if the ratchet is not trusted.

## t-photocreateform-field-descriptions

TRIGGERED DEBT, filed not scoped. 1 of `PhotoCreateForm`'s 3 fields carries no `description=`. Same model as `t-takenat-tz-question` but a different concern: that task is about what the field ACCEPTS, this one about what the document SAYS. They will likely be taken in one patch when the photo endpoints land, but the tz question is a contract ruling that must be made first and this one is not blocked on it.
- Current consumer: none — no route imports `PhotoCreateForm`.
- Promotion trigger: `POST /trips/{slug}/stops/{id}/photos` publishing the model into OpenAPI.

## t-bikecreate-field-descriptions

TRIGGERED DEBT, filed not scoped. 3 of `BikeCreate`'s 6 fields carry no `description=`.
- Current consumer: none — no route publishes the model.
- Promotion trigger: `POST /trips/{slug}/bikes` under `s-bike-management`.
- `t-trip-metadata-endpoint` closed `BikeOut`'s descriptions and explicitly left `BikeCreate`/`BikePatch` with this story; that was the right call then and this entry is the follow-through, not a reopening.

## t-bikepatch-field-descriptions

TRIGGERED DEBT, filed not scoped. NONE of `BikePatch`'s 5 fields carries a `description=` — the worst-covered model in the contract.
- Current consumer: none — no route publishes the model.
- Promotion trigger: `PATCH /trips/{slug}/bikes/{id}` under `s-bike-management`.
- Worth a sentence when it is taken: on a PATCH model the description is where the partial-update semantics get stated (omitted means unchanged), which is exactly the thing a generated client cannot infer from the type.

## t-bare-409-envelope-bypass

`qa` finding from `t-conflict-code-tests`. Since `7ca82ab` the `409` row in `_STATUS_TO_CODE` is keyed on status, so a bare `HTTPException(409)` renders a well-formed `CONFLICT` envelope with `message: "Conflict"` — Starlette's default phrase — without ever passing through `ApiError.conflict`. THE LEAK BOUNDARY LIVES AT THE CLASSMETHOD: the contract's rule that a conflict message carries no value from the conflicting record is enforced where the message is built, and a bare raise skips it. The envelope looks correct, which is the whole difficulty.
- Gate classification: TRIGGERED DEBT (Gate 3).
- Current consumer: none. Nothing raises a 409 today, and the only `HTTPException` raise site anywhere in `backend/app/` is the 405 at `main.py:189`.
- Promotion trigger: the first 409 raised anywhere other than `ApiError.conflict`.
- DO NOT "FIX" THIS BY REMOVING THE MAPPING ROW. The row is the contract (Entry 14) and it is also what makes `ApiError(409, CONFLICT, ...)` constructible at all — `__init__` checks the pair against `code_for_status`. Whatever this becomes, it is a guard at the raise site, not a retreat from the mapping.

## t-errors-docstring-dangling-line

Cosmetic. The constructor-list reflow left a dangling line in `backend/app/core/errors.py`'s module docstring.
- Gate classification: ORDINARY DEBT (Gate 4). A docstring has no runtime path — nothing imports, executes or serves it, so no behaviour depends on it.
- Current consumer: none. Promotion trigger: none.
- Fold it into the next edit that touches that docstring rather than spending a patch on it. Note `t-errors-docstring-tense` was a DIFFERENT defect in the same docstring — it described future `security.py` behaviour as present — and is closed; this one is purely a stray line left by formatting.

## t-qa-mutation-scratch-tree

THE TRIGGER, STATED PRECISELY BECAUSE IT IS EASY TO MISREAD: an orchestrator-instructed mutation experiment on `backend/app/data/db.py`. `qa` mutated the file as dispatched, restored it correctly, and reported plainly what it had done. THE RULE EXISTS BECAUSE THE GUARANTEE WAS HELD BY DISPATCH WORDING RATHER THAN BY ANYTHING STRUCTURAL — NOT BECAUSE AN AGENT MISBEHAVED. Nobody is being corrected here; the process is. Anyone reading this later should not infer a trust problem with the `qa` agent, because there was not one.

- Gate classification: CURRENTLY OBSERVABLE (Gate 2). The mutation happened, in the real working tree, on a real file. `qa` is described throughout the roster as having no edit access, but its grant is Read/Bash/Grep and Bash writes files — so the only thing standing between "no edit access" and an edited file was how the dispatch happened to be worded.
- Resolution: `.claude/agents/qa.md` now says mutation experiments run in a scratch tree — a `git archive HEAD` copy or equivalent — never in the working tree. Written, uncommitted at the time of this entry.

DROPPING BASH FROM THE GRANT WAS CONSIDERED AND REJECTED. It is the one construction that would make the guarantee structural rather than textual, and it is unaffordable: `qa`'s entire job is independent verification, which means running `pytest`, standing up real uvicorn, and running every other validation command. An agent that cannot execute anything cannot verify anything — it would be reduced to reading the same code `dev` read and agreeing with it, which is the exact failure mode a separate `qa` agent exists to prevent. So the boundary stays textual on purpose, and this story exists because a textual boundary is worth writing down rather than assuming.

THE HOOK — the third option, neither prose nor removing Bash — was deliberately left OUT of this patch and filed as `t-qa-mutation-hook`. It lives in settings, not in `qa.md`, so it is a different file and a different patch; this task shipped the prose rule only.

## t-qa-mutation-hook

The structural version of the rule `t-qa-mutation-scratch-tree` shipped as prose. `.claude/agents/qa.md` now forbids mutating the live working tree (committed `0b5bcde`), but that guarantee is words — a `PreToolUse` hook matching write-shaped Bash commands against the project path would make it ENFORCED rather than REQUESTED. It is not an edit to `qa.md`: hooks live in settings, which is why it was left out of that patch rather than overlooked.

GATE TRIAGE 2026-09-16 — `gate: triggered`.
- Finding: the no-live-tree-mutation guarantee for `qa` is carried entirely by prose in `qa.md`. `qa`'s grant is Read/Bash/Grep, and Bash writes files.
- Evidence: the prose rule IS the control today and it has not yet failed. It was followed correctly on its first outing — the `qa` run verifying `t-conflict-code-impl` used detached worktrees and scratch copies, removed them, and reported a clean `git status --porcelain`.
- Gate classification: TRIGGERED DEBT (Gate 3). The control exists and is working; replacing a working control is not a fix.
- Current consumer: none — no run has breached the prose rule.
- Promotion trigger: the first time a `git status --porcelain` check after a `qa` dispatch comes back non-empty, OR a QA report admits a live-tree mutation. **ONE such failure is sufficient — do not wait for a second, and do not treat the first as an anomaly worth re-testing.** Prose either holds or it doesn't; a single breach is the whole evidence this classification was waiting on.

WHY PROSE WAS TRIED FIRST, WHICH IS THE PART WORTH KEEPING. The rule permits an experiment that is genuinely valuable: deliberately breaking a fix to prove a test catches its absence. That is mutation testing, and Entry 7 is the record of it catching two real defects a green suite missed — it is not a practice to design a control against. A hook that is too blunt blocks the legitimate case along with the illegitimate one, and a `qa` agent that cannot mutate anything anywhere cannot do the thing it is most valuable for. So the hook has to distinguish "writes inside the project directory" from "writes in a scratch tree" — and that distinction is easy to state in prose and hard to match on a command line (a path can be relative, constructed in a variable, reached after a `cd`, or written by a tool invoked with its own `-C`/`--work-tree`). THAT is the reason the cheaper control went first: the expensive one is not merely more work, it is harder to make CORRECT, and an incorrect version fails closed against the useful case. Not reluctance, and not "prose is good enough" — a judgment that the precise control is the harder one to build.

## t-docs-agent-unscoped-grant

The `docs` agent is described everywhere as `docs/`-only, but its grant is Edit/Write with no path scoping — the same class of prose-only boundary as `t-qa-mutation-scratch-tree`, through a different channel. `qa`'s gap is "no edit access, but Bash writes files"; this one is "edit access, but nothing bounds where."

THE DOCUMENTATION HALF IS ALREADY CLOSED, THE GRANT HALF IS NOT. `CLAUDE.md`'s roster row was corrected in `0b5bcde` to match `docs.md` and spec Section 11, so there is no longer a contradiction between the three places that describe this agent's scope — they now agree that it is `docs/` plus doc comments co-located with code. What remains is only that the tool grant does not encode that agreement.

GATE TRIAGE 2026-09-16 — `gate: ordinary`.
- Finding: `docs`'s Edit/Write grant is not scoped to `docs/`; the boundary is prose in three now-consistent places.
- Evidence: no `docs` run has written outside `docs/`. The boundary has held every time it has been exercised, and the documentation contradiction that might have caused a good-faith breach is resolved.
- Gate classification: ORDINARY DEBT (Gate 4). `Could break ≠ is broken`.
- Current consumer: none.
- **Promotion trigger: none, deliberately.** Not "the first out-of-scope write" — unlike `t-qa-mutation-hook`, where a live-tree mutation is detectable by a `git status --porcelain` check that is already part of the dispatch loop, nothing currently watches `docs`'s writes, so a trigger phrased as "the first breach" names an event no one would observe. Inventing an unobservable trigger to make the item look actionable is worse than recording none: it reads as coverage the backlog does not have. If this is ever promoted it will be by a decision to scope agent grants generally, not by this item firing.

RELATED, NOT DUPLICATE: decision-log Entry 12 records that widening `docs` to `.claude/**` was REJECTED as self-modifying permissions. That is the opposite direction — Entry 12 refused to make this grant wider; this item observes it is not explicitly narrow. Do not read Entry 12 as having settled the scoping question, and do not reopen its ruling on the way to closing this one.
