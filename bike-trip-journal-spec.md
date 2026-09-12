# Bike Trip Journal — Technical Spec (v2 — portable/Docker)

## 1. Overview
- **Trip:** Melbourne → Coober Pedy (assumed — confirm SA via Stuart Hwy, not NT)
- **Purpose:** Shared travel journal for a group bike trip. Photo + location + notes per stop, plus a record of each rider's bike.
- **Scope for v1:** No LLM features. No native app. Free to run and free for friends to use.
- **Timeline:** ~4 weeks, solo build with agent-assisted dev (Claude Code).
- **v2 change:** Originally scoped Azure-native (Functions + Static Web Apps + Cosmos DB + Blob). Revised for local-first development and cloud-agnostic deployment — see Section 2.

## 2. Locked Decisions
| Decision | Choice | Why |
|---|---|---|
| Photo storage (archive) | OneDrive (Microsoft Graph API) | Cleaner read/write than Google Photos' 2025 API restrictions. This is fixed regardless of hosting provider — it's your personal archive destination, not part of the app's cloud stack. |
| Friend access | Two links per trip — rider link (write) and viewer link (read-only) | Riders capture memories live; non-riders just watch along |
| Serving photos | S3-compatible object storage (Cloudflare R2 in prod, MinIO locally) + background sync to OneDrive (archive) | R2 has a generous free tier with zero egress fees and a real S3 API — same client code runs against local MinIO for dev, no cloud dependency needed to develop |
| Backend | Python + FastAPI, containerized (Docker) | Runs identically local and in any cloud; no Functions-specific ASGI shim, no vendor-specific triggers |
| Database | PostgreSQL — Neon (managed, free tier) in prod, Postgres container locally via docker-compose | Real backups/durability from a managed free tier without self-hosting a stateful container; identical connection-string interface locally and in prod |
| Frontend framework | TanStack Router + TanStack Query, SPA mode (Vite build) | Type-safe routing/data-fetching, builds to static files, served directly by FastAPI — no separate static host or Node server needed |
| Map | Leaflet + OpenStreetMap | Free, no API key required |
| Compute hosting | Docker container on Azure Container Apps (free tier: ~180k vCPU-s + 2M requests/mo) | Free where Azure makes sense today, but the image is the unit of portability — same container runs on Fly.io/Cloud Run/Render/a VM unchanged if Azure ever stops making sense |
| Frontend hosting | Bundled into the same container — FastAPI serves the built SPA as static files | One container, one deploy, simplest ops for a solo 4-week build |

## 3. Data Model
```
Trip
  id, name, riderSlug (write-enabled, unguessable), viewerSlug (read-only, unguessable), startDate
  (rider identity lives on Bike.riderName and Photo.uploadedBy — no separate riders list needed)

Stop
  id, tripId, name, lat, lng, locationSource ("gps" | "manual" — how lat/lng were
  obtained, Section 6), arrivedAt (datetime), notes

Photo
  id, stopId, objectKey, oneDriveFileId (nullable until synced),
  uploadedBy (display name only, no auth), takenAt

Bike (i.e. motorcycle — this trip has 3)
  id, tripId, riderName, make, model, year, specs (free text: engine, suspension, tyres, etc.)
```

## 4. Architecture
- **Frontend:** PWA built with TanStack Router + TanStack Query in SPA mode (Vite build, no SSR/server functions). Installable, works like an app without an App Store. Service worker + IndexedDB queue so photo capture works with no signal (expect dead zones on the Stuart Hwy) and syncs when back online. Photos are resized/compressed client-side before queuing (e.g. cap ~1600px, convert HEIC→JPEG for iPhone shots) — keeps uploads small on weak signal and stays under request payload limits. Built output is served directly by the backend container as static files.
- **Backend:** Python + FastAPI, packaged as a single Docker image (uvicorn/gunicorn, no cloud-specific runtime shim). Same image runs via `docker-compose` locally and deploys to Azure Container Apps (or any container host) unchanged.
- **Database:** PostgreSQL. Locally, a Postgres container via `docker-compose`. In production, a managed free-tier Postgres (Neon) for real backups/durability without self-managing a stateful container. Access goes through a thin `data/` module — app code never talks to a specific DB provider's SDK, just SQL/an ORM against a Postgres connection string.
- **Storage:** S3-compatible object storage is the source of truth the app always reads from, so nothing user-facing depends on OneDrive being up to date. Locally this is MinIO (S3 API, runs in `docker-compose`); in production it's Cloudflare R2 (S3-compatible, free tier, zero egress fees). A timer-triggered background job periodically pushes new photos to a OneDrive folder via Microsoft Graph, using a refresh token stored as a container secret/env var, not committed to source. On sync failure, retry with backoff; if it keeps failing, the photo just stays pending-archive and tries again later — never lost, never blocking. Do a real end-to-end sync test before departure to confirm the token's healthy; if it lapses mid-trip, only the archive step pauses. Access goes through a thin `storage/` module using a single S3-compatible client (e.g. boto3 or an S3-compatible SDK) pointed at either MinIO or R2 via config — same code path both places. Confirmed: Microsoft Graph *can* read file metadata back from OneDrive if ever needed, but the app deliberately never depends on that — the read path never touches Graph, so there's no runtime dependency on OneDrive being reachable, only a background write.
- **Uploads — chunked and resumable:** photo uploads use S3 multipart upload — core S3 API, not a provider feature, so it behaves identically against MinIO and R2. A photo that fails partway through (weak signal mid-trip) keeps its uploaded-parts state in the offline queue and resumes the multipart upload on the next attempt rather than restarting from byte zero; it's only marked "uploaded" once the completion call succeeds.
- **System design priority — availability and eventual consistency over strong consistency:** this app has a handful of concurrent users and the goal is fast, visible updates, not transactional correctness. The offline-queue architecture already assumes this (a write lands locally first, syncs when possible). The one place two riders could race — editing the same Bike's specs — resolves with simple last-write-wins; no need for merge logic or optimistic-lock conflict UI at this scale.
- **Change management principle:** the things that make this easy to change later already exist by design, not as extra work — the `data/`/`storage/` thin modules (portability principle, below), a hand-designed API contract that Kubb turns into generated frontend types (Section 5) so a contract change shows up as a type error instead of a silent runtime bug, and the one-task-one-reviewable-patch discipline (Section 10). Named here so it stays a deliberate practice, not an accident of how the first version happened to get built.
- **Map:** Leaflet, rendering stop pins from a `/map` GeoJSON endpoint. The trail is a polyline auto-connecting stops in chronological order — no background GPS tracking needed, it just builds itself as stops get added and forms the full route by trip's end.
- **Access control:** Two unguessable slugs per trip. The rider slug unlocks "Add stop" / photo upload; the viewer slug shows the exact same map, timeline, gallery and bikes but with no write UI, and the API rejects POSTs made against a viewer slug. No passwords, no accounts — just which link someone was given. Optional: a write-PIN on top of the rider link, in case it gets forwarded past the trip group.
- **Hosting:** Single Docker image (FastAPI backend + built SPA as static files) deployed to Azure Container Apps, which scales to zero on the free tier — expect a cold start on the first request after a quiet spell; worth a loading indicator on first load rather than trying to eliminate it (an always-on plan is a paid tier). Because it's a plain container, this is not an Azure-only deployment — the same image works on any container host (Fly.io, Cloud Run, Render, a VM) with no code changes, only a redeploy.
- **Portability principle:** since more trip journals are planned beyond this one, isolate all external-service code behind thin modules — a `data/` layer for Postgres, a `storage/` layer for the S3-compatible client — so app logic and API routes never call a specific cloud provider's SDK directly. This is now load-bearing, not aspirational: local dev runs the exact same code path (MinIO + Postgres container) as production (R2 + Neon), so there's no "works on my machine, breaks in the cloud" gap, and a future platform migration touches config/env vars, not app code.

## 5. API (rough)
```
GET  /trips/{slug}                     → trip metadata + this trip's bikes (either slug)
GET  /trips/{slug}/stops               → list of stops (either slug)
POST /trips/{slug}/stops               → create a stop (rider slug only — 403 on viewer slug)
POST /trips/{slug}/stops/{id}/photos   → multipart photo upload (rider slug only)
GET  /trips/{slug}/stops/{id}/photos   → list photos for a stop (either slug)
POST /trips/{slug}/bikes               → add a bike + specs (rider slug only)
PATCH /trips/{slug}/bikes/{id}         → edit a bike's specs (rider slug only)
GET  /trips/{slug}/map                 → GeoJSON of stops (+ route if drawn) (either slug)
```
No update/delete endpoints for photos in v1 — upload is the only write path, everything else is read-only display.

**Frontend types generated from the backend, not hand-duplicated:** FastAPI's auto-generated OpenAPI spec is the single source of truth for request/response shapes. [Kubb](https://kubb.dev) generates the TanStack Query hooks and TypeScript types in `frontend/src/api/` directly from that spec, rather than either side hand-writing a shadow copy of the contract. This only works if the OpenAPI spec is actually complete — every route and every Pydantic field needs a real `description`, not just a type — so that requirement is not optional polish, it's what makes Kubb's output (and every agent's context) trustworthy.

**Documentation format — required on every endpoint and every frontend module**, so an agent can act on a piece of the system without re-reading and re-parsing the code that implements it every time:
- **API docs** (co-located as docstrings/FastAPI `description=`, compiled into human-readable docs by the `docs` agent — Section 11): **Context** (what it's for and why it exists) → **How it works** (enough that an agent doesn't need to trace the implementation) → **Related APIs** (what calls it, what it calls, which build task introduced it)
- **Frontend docs** (co-located as component-level comments, same compilation step): **Design feature** (what UI behavior this is) → **Design format** (layout/interaction pattern used) → **APIs called** (which generated Kubb hooks, and why)

## 6. Frontend Screens
1. **First open** — no login. Prompts for a display name only, saved locally on the device (localStorage). This name tags everything they upload — it's identity-by-label, not auth. The actual OneDrive write always happens under your single stored token, regardless of who's uploading.
2. **Trip home** — map with stop pins + auto-generated trail (polyline connecting stops in order) + a chronological timeline feed below it. Clicking a pin opens that stop's photo gallery (thumbnail grid → tap to enlarge), plus its notes and timestamp.
3. **Add stop** — camera capture, auto-filled GPS + timestamp, notes field. If location permission is denied or unavailable, fall back to tapping a point on the map manually. Either path records which one happened (`Stop.locationSource`, Section 3) — not just the coordinates — so a viewer can tell an exact GPS fix from an approximate manual tap, and `test-writer` has something concrete to assert the fallback path actually ran.
4. **Bikes** — this trip's 3 motorcycles with make/model/specs. Scoped to the trip, so a future trip's bike lineup can be totally different.
5. **Offline indicator** — shows queued/pending syncs

**Photos are view-only in v1** — uploaded once via "Add stop," then just displayed (map click, timeline, gallery). No in-app edit or delete, which keeps the API and permissions simpler.

## 7. Build Sequence (4 weeks)
- **Week 1:** Session 0 architecture layout with Claude Code (produces CLAUDE.md). Local dev environment via `docker-compose` (FastAPI + Postgres + MinIO). Neon project + Cloudflare R2 bucket setup (prod). Microsoft Graph app registration + OneDrive OAuth consent (one-time, your account). Azure Container Apps environment setup (free tier). Seed the one trip record directly in Postgres. Data model + API skeleton.
- **Week 2:** Core API — stops CRUD, photo upload → object storage → background OneDrive sync job, map GeoJSON endpoint.
- **Week 3:** Frontend PWA — map view, add-stop flow (camera + geolocation), offline queue via service worker.
- **Week 4:** Bikes page, write-PIN if wanted, polish, real-device testing with friends, buffer for bugs before departure.

## 8. Open Items Before Week 1
- ~~Confirm destination~~ — **Coober Pedy, SA — confirmed**
- ~~Cloud/hosting architecture~~ — **Resolved: local-first via docker-compose, Postgres (Neon) + S3-compatible storage (R2), Docker deploy to Azure Container Apps free tier — see Section 2**
- Full route/stop list — not a blocker, can be added as the trip unfolds
- Map style: plain Leaflet/OSM vs. a nicer free-tier Mapbox theme
- Write-PIN or trust the rider link as-is

## 9. Deferred to v2 / Future Work

**v2 (same product, next iteration):**
- **Trip picker** — a home screen listing multiple trips. Not needed for v1 (one trip only), and no backend rework required later since Trip is already a first-class entity with its own slugs — v2 just adds a picker UI in front of it.

**Future work / learning extensions (explicitly out of scope for this trip's build — captured here so nothing in v1 accidentally forecloses them, not to provision for them now):**
- **Data visualization / free GIS** — once a trip's stops/photos exist as real data, a later pass could render richer views than Leaflet pins: elevation profile along the route, a heatmap of stop density, a proper post-trip map export. Worth a feasibility look at free/open tools (e.g. QGIS for offline analysis, or a JS viz layer like deck.gl/Kepler.gl for a shareable web view) once there's actual trip data to point them at — research item, not a v1 dependency.
- **Personal learning sandbox on the collected data** — once the trip is over, the Postgres data (stops, bikes, photo metadata) is small and well-structured enough to be a low-stakes practice dataset: an ETL export into Databricks or Snowflake for batch/analytics practice, or into Neo4j to model rider–bike–stop as a graph. Deliberately decoupled from the live app — this would be a one-way export job reading from Postgres, never a dependency the trip journal itself relies on, so it can be picked up or dropped without touching v1.
- **Live telemetry during the trip** — showing real-time position/speed/bike specs while riding, not just after-the-fact stops. This is a materially different feature (continuous time-series ingest vs. discrete Stop records) and needs its own spec pass before any build decision. Open research questions to resolve first: which phone apps or motorcycle GPS/OBD sensors can actually stream this data today (rather than just log it locally for later export), and whether any expose a webhook/API this app could ingest from. Not investigated yet — flagged here so it's not forgotten, not because a path is already known to exist.

## 10. How This Gets Built With Claude Code

This spec is the "what and why." A second, shorter file — **CLAUDE.md** — belongs at the project root as the "how": stack, conventions, exact build/test commands, and what's off-limits (e.g. "never modify OneDrive token handling without explicit approval"). Claude Code auto-loads it every session. Keep it under ~200 lines; point to this spec for detail rather than duplicating it.

Build order is a strict gate, not a suggestion: **architecture → API definition → build.** Nothing in Section 5 gets implemented until the API contract for it is nailed down in writing; no contract gets written until the architecture session has fixed where it lives.

**Architect — you and the orchestrator together, not a subagent.** Every other role in Section 11 either defines scoped tasks against an existing contract (`ba`) or implements a task already scoped (`dev`, and the rest of the per-task pipeline). None of them are the one who invents the contract's *shape* in the first place — that's cross-cutting design judgment spanning the whole system at once, which is exactly the thing that shouldn't be delegated to a subagent or decided unilaterally by the orchestrator. Sessions 0, 1, and the final Handover session are Architect work.

The Architect checkpoint is **propose, then agree — in conversation, not in files.** The orchestrator drafts the design (folder structure, a contract shape, a diagram) as discussion — tables, examples, options with tradeoffs — and nothing gets written to disk until you've agreed to it. Architect work produces a decision, not code. Once agreed, *writing it down* goes through the same pipeline as everything else — `ba` scopes it as a task, `dev` implements it — the orchestrator does not finalize architecture decisions as files directly, even for Sessions 0/1/Handover. (Session 0 itself is the one unavoidable exception: its output *is* the agent definitions, so there's no `dev` agent yet to dispatch to the first time through. Every session after that has no such excuse.)

**Session 0 — Architecture layout (first thing, before any feature code or endpoints):**
Hand Claude Code this spec and have it lay out the concrete architecture within the now-fixed stack (Python/FastAPI in Docker, TanStack Router/Query SPA bundled into the same image, Postgres via a `data/` module, S3-compatible storage via a `storage/` module, Leaflet) — folder structure, `docker-compose.yml` for local dev, how the offline queue is wired up. Its output for this session should be the CLAUDE.md file itself. No endpoint logic, no request/response schemas yet. Session 0 also scaffolds the build agent roster and procedural skills (Section 11) as `.claude/agents/*.md` and `.claude/skills/*/SKILL.md` files, so every later session has the right agent with the right context and scope available from the start, plus a `docs/architecture-diagram.md` (Mermaid) that every agent must read before starting any session or task, and the `docs/progress.json` checkpoint dashboard (Section 11) that every task updates going forward.

**Session 1 — API contract definition (after architecture, strictly before any endpoint is implemented):**
Turn Section 5's rough endpoint list into a real contract: Pydantic request/response models for every endpoint, status codes (including the 403-on-viewer-slug behavior), a consistent error envelope, and how the offline queue's retried/duplicate uploads are handled idempotently. Architect work, per above — proposed and reviewed with you before the model files are finalized, recorded as `docs/api-contract.md` alongside the actual Pydantic models. This becomes the reviewable artifact both frontend (TanStack Query hooks, generated by Kubb — Section 5) and backend (route handlers) are built against — FastAPI's auto-generated OpenAPI docs are the living, always-in-sync version of it once code exists, but the models are hand-designed here, not an afterthought discovered mid-implementation. Frontend and backend tasks in Weeks 2–3 should not start until this is settled, since a contract change after both sides are built is much more expensive than one caught here. `dev`'s work starts at Week 2, implementing route logic against a contract the Architect has already fixed — not inventing the shape itself.

**Final session — Handover (after Week 4, once the app is live and working):**
A working app isn't the same as a handed-off one. This session produces two documents, written by the `docs` agent from what actually got built (not from this spec's intentions):
- `docs/architecture-handover.md` — how the system actually works, for two audiences at once: a plain-language architecture explanation a non-technical stakeholder could follow, and a "where do I make a change" map for a future developer (or future-you in six months), keyed to the real folder structure and module boundaries, not the spec's original plan.
- `docs/user-guide.md` — how to actually use the app: what the two links (rider/viewer) do, how to add a stop, what happens offline, written for the friends using it, not for a developer.

**Per-task prompt structure** (one task = one reviewable patch, not a whole week at once):
- **Role** — e.g. "act as a backend engineer on this project"
- **Goal** — one scoped outcome (e.g. "add the POST /stops endpoint")
- **Constraints** — tech already locked in this spec; anything explicitly off-limits
- **Scope** — which files/areas it should touch
- **Acceptance criteria** — concrete, testable conditions for "done"
- **Validation** — the exact command to run to prove it works; instruct it to say so explicitly if something can't be verified rather than assuming success

**Guardrails to set up before starting:**
- Permission allowlist/deny-list in Claude Code settings — block commits touching secrets, `.env`, or deployment config without explicit approval
- Require the validation command to actually run before a task counts as done
- Review each task's patch before moving to the next — don't queue up multiple unreviewed tasks

## 11. Agent Architecture

Purpose-built agents, not one generic assistant grinding through every task — each one gets only the context and tools its job needs, which is both cheaper (no full-project context reloaded for a one-line fix) and more reliable (a dev agent doesn't second-guess test coverage, a QA agent doesn't rewrite the code it's checking). Defined as `.claude/agents/*.md` during Session 0, then used for every task in Weeks 1–4. Scope for each agent lives in its own definition file, not a shared document — that's what makes it authoritative for that agent every time it's invoked.

**PM / Orchestrator — merged into the main Claude Code session, no separate agent file.** Owns the plan, is the one talking to you, decides what the next task is, dispatches to the agents below, and — per the guardrails above — surfaces every resulting patch to you for review before starting the next task. This session already has full spec + CLAUDE.md + conversation context, so splitting orchestration into its own subagent would only add cold-start overhead for pure coordination. Distinct from Architect (Section 10): the orchestrator dispatches and reviews *tasks* against a contract that already exists; the Architect (you + the orchestrator, together) is who fixes that contract's shape in the first place, and does so by proposing it for your agreement before it's finalized as files.

**BA agent** (`ba`) — works at two levels, not just one task at a time. First, breaks the whole spec into **Stories** (feature-sized units — e.g. "Stop CRUD," "Photo upload + OneDrive sync," "Offline queue") grouped into **Milestones** (roughly the existing Week 1–4 structure, Section 7), recorded in `docs/progress.json` (below). Then, as the orchestrator reaches each Story, breaks it into individual tasks using the per-task prompt structure above (Role/Goal/Constraints/Scope/Acceptance criteria/Validation), against the API contract (Section 5, post–Session 1) and CLAUDE.md. Read-only (Read/Grep/Glob) — it defines and tracks work, it doesn't touch code.

**Dev agent** (`dev`) — implements exactly one task as scoped by the BA agent or orchestrator, following CLAUDE.md conventions and the API contract exactly. Full edit access (Read/Edit/Write/Bash/Grep/Glob), but scoped to the task's declared file/area — not free rein over the repo.

**Test agent** (`test-writer`) — writes tests for what the Dev agent just built, weighted per Section 12's priorities. For the three priority failure modes (access control, data integrity, offline queue), it writes tests from the API contract and the BA task's acceptance criteria, not from reading the Dev agent's implementation — so a bug born from a shared misunderstanding of the contract can't slip through both the code and the test that's supposed to catch it. Everywhere else, ordinary implementation-following tests are fine — cheaper, and proportionate to the risk. Read/Edit/Write/Bash (needs to run what it writes).

**QA agent** (`qa`) — adversarial verifier, not a fixer: independently re-derives the BA task's acceptance criteria and checks actual behavior against them directly (not just "the test suite is green," which only proves the tests as written pass, not that they were the right tests), runs the validation command, checks for regressions, and explicitly flags anything it can't verify rather than assuming success. Can invoke the built-in `code-review` skill against the Dev agent's diff as an extra adversarial pass. Read/Bash/Grep only — no Edit, so it can't paper over a failure by silently patching it; if it finds a problem, the orchestrator routes it back to the Dev agent with QA's findings attached.

**DevOps agent** (`devops`) — owns infrastructure and deployment, not application code: Dockerfile, `docker-compose.yml`, Container Apps deployment config, wiring Neon/R2/Microsoft Graph credentials (names and structure, never committing actual secret values), the budget alert, and the Week 4 production cutover. Kept separate from the Dev agent because this work carries a different risk profile — real cloud spend, secrets, deployment config — matching the existing guardrail that changes here always need your explicit review, more strictly than an application-code patch does. Read/Grep/Glob/Bash/Edit/Write, but scoped to infra files only — never application source. Deployment itself is last in priority order — get the app working end-to-end locally first (Section 7); Container Apps cutover happens once there's something worth deploying.

**Documentation agent** (`docs`) — runs once QA signs off on a task, writing the required API/frontend documentation (Section 5's format) for what was just built, co-located with the code so it can't drift out of sync. Also owns `docs/progress.json` — updates it at task start ("in progress") and task end ("done"/"blocked"), so if an agent fails abruptly mid-build, the next session can read the dashboard and know exactly which task was interrupted and what state it was left in, rather than re-deriving that from git log and guesswork. Read/Grep/Glob/Write/Edit, scoped to `docs/` and the co-located doc comments — never application logic.

`docs/progress.json` is a flat, agent-readable checkpoint file, not a real project-management tool — just structured enough that every agent can read "what's the current state" in one file read:
```
{
  "milestones": [{ "id", "title", "status" }],
  "stories": [{ "id", "milestoneId", "title", "status" }],
  "tasks": [{ "id", "storyId", "title", "status", "agent", "blockers": [] }],
  "currentTask": "<task id or null>",
  "lastUpdated": "<ISO timestamp>"
}
```

**Skills** (`.claude/skills/*/SKILL.md`) — where agent *definitions* fix who does what and with which tools, skills capture repeatable *procedures* so they don't drift across ~15–20 similar tasks over the build:
- `add-endpoint` — the recipe from one API-contract entry (Section 5) to a working, tested endpoint: Pydantic model → route → `data/`/`storage/` module wiring → test → QA validation command. Keeps BA/Dev/Test/QA consistent task after task instead of re-deriving conventions each time.
- `deploy` — the DevOps agent's repeatable Container Apps deploy/rollback steps.

**Execution mode:** manual per-task dispatch, not a scripted pipeline — the orchestrator invokes BA→Dev→Test→QA→Docs (or the DevOps agent, for infra/deployment tasks) as needed for each task and stops for your review before moving on, preserving the "one task = one reviewable patch" guardrail above. A fully scripted multi-agent pipeline (via the Workflow tool) was considered and set aside for now since it loosens that review gate and costs more per task in agent-spawn overhead; revisit if the manual cadence turns out to be the bottleneck.

**Deferred research — offloading to a local model:** Claude Code's subagents (above) all run on Claude; there's no built-in mechanism to route a task to a local model (e.g. via Ollama) instead — that would need a separate custom tool outside this subagent system, not a `.claude/agents/*.md` definition. Plausible candidates if it's ever worth building: the Documentation agent's write-up formatting, or boilerplate CRUD test scaffolding — low-stakes, high-volume, low-reasoning work. Not pursued for this build; flagged as a standalone research spike, independent of Session 0.

## 12. Testing Approach

Solo hobby project on a 4-week clock — full coverage isn't the goal, protecting against the failure modes that would actually ruin the trip's memories is.

**Prioritize tests for:**
- **Access control** — viewer slug genuinely can't write (403 on every POST endpoint), rider slug can
- **Data integrity** — a photo that's been uploaded is never silently lost (this is the one that matters most; a failed OneDrive sync should retry or surface an error, not just drop the photo)
- **Offline queue** — capture while offline, close the app, reopen, confirm it still syncs when back online (this is the trickiest part of the whole build and deserves its own dedicated test pass)

**Evaluation approach — closing the loop between "tests exist" and "it actually works":**
- A task isn't done because a validation command exited 0. It's done when QA has independently re-derived the acceptance criteria from the BA task and confirmed actual behavior against them (Section 11) — "tests are green" is necessary, not sufficient.
- For the three priority failure modes above, Test-agent authorship is contract-first (written from Section 5's API contract and the task's acceptance criteria), not implementation-following — otherwise a misunderstanding shared between Dev and Test can pass QA undetected.
- The offline queue and OneDrive sync don't wait until Week 4's real-device day to get evaluated for the first time — Weeks 2–3 include a scripted test that simulates network loss mid-upload, an app restart, and confirms resume/sync on reconnect. Week 4's manual pass then confirms real-device behavior rather than discovering a fundamental design flaw for the first time a week before departure.

**Lighter touch elsewhere:**
- Unit tests around stop/photo/bike creation and validation — enough to catch regressions, not exhaustive edge-case coverage
- No need for full frontend E2E automation given the timeline — Week 4's real-device test day (Section 7) covers this manually, but explicitly test: airplane-mode capture, multiple riders uploading at once, GPS permission denied

**Skip for v1:** load testing, cross-browser matrix testing, accessibility audit — not proportionate to a friends-and-family trip journal.

**Manual/exploratory endpoint testing:** FastAPI's auto-generated OpenAPI/Swagger UI (always in sync with the code, zero extra tooling) is sufficient at this scale — no separate Postman collection to maintain. Automated coverage is the Test/QA agents' pytest suite; Swagger UI is just for poking at an endpoint by hand during development.

## 13. Open Items for Session 0

Language/framework is now decided (Python + FastAPI backend in Docker, TanStack Router/Query SPA frontend bundled into the same image — see Sections 2 and 4), so Session 0 becomes purely architecture layout: folder structure, `docker-compose.yml` for local dev, how the offline queue is wired up, the `.claude/agents/*.md` roster and `.claude/skills/*/SKILL.md` (Section 11), the `docs/architecture-diagram.md` and `docs/progress.json` checkpoint dashboard, and the CLAUDE.md write-up — not open decisions. The BA agent's first real task once Session 1 (API contract) lands is populating `progress.json` with the initial Stories/Milestones breakdown.

Two operational tasks, not spec decisions:
- Set a low-threshold Azure budget alert before Week 1 starts, as a free tripwire against surprise costs on Container Apps (the free-tier limits themselves are far beyond what this trip will use)
- **Trip creation** doesn't need an admin UI for v1 — since there's only one trip, seed it directly in Postgres (via script) during Week 1 setup rather than building a "create trip" flow nobody else will use
