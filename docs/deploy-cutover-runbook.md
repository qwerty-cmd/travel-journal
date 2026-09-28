# Deploy cutover runbook

An ordered checklist for taking the app from "works locally" to "the friends have their links".
Task `t-deploy-cutover-runbook`, story `s-deploy-cutover`.

**Rules for this document.** No secret value appears here, and none should ever be pasted into an agent
session. That covers connection strings, keys, tokens and trip slugs. Everything that touches `.env`,
`infra/`, `GRAPH_*` or cloud billing needs your explicit approval (CLAUDE.md "Off-limits"). An agent can
draft the work, but it cannot approve it.

**Who does each step:**
- **You**: the owner. You create accounts, give consent, hold secrets and approve spend.
- **devops**: the devops agent, working only after your approval. It covers `Dockerfile`, `docker-compose.yml` and `infra/`.
- **dev**: the dev agent, for application code, through the normal pipeline.

Tick each box as you go.

---

## 0. Blockers to clear before starting

- [ ] **You → dev (scoped by ba). The Neon `sslmode` trap.** Neon's console connection string ends in
  `?sslmode=require`. asyncpg rejects that parameter, and `normalize_database_url` in
  `backend/app/data/db.py` passes the query string through unchanged. The result is
  `TypeError: connect() got an unexpected keyword argument 'sslmode'`, which looks like a credentials
  problem but isn't one. See the `s-cloud-service-setup` note in `progress-notes.md`. The fix belongs in
  `normalize_database_url`. Until it lands, the only workaround is to edit the secret value by hand. Neon
  also adds a `channel_binding=` parameter, and asyncpg is likely to reject that too. Neither workaround
  has been tested against Neon.
- [ ] **You. The migration runner has never run against Neon**, only against local Postgres 16. Treat the
  first run in step 4 as the test.
- [ ] **You → ba. Nothing schedules the OneDrive sync.** `python -m app.storage.onedrive_sync` does one
  pass and exits (see its module docstring), and no cron job, Container Apps job or other scheduler
  exists in the repo. Decide how it will run in production, for example a Container Apps scheduled job or
  a manual run from your laptop, and have `ba` scope that decision. This does not block the app itself,
  because S3/R2 is the source of truth. It does mean nothing is archived until it is decided.
- [ ] **You. Pick a container registry.** Azure Container Registry has no free tier. A free registry
  such as GHCR also works; a private one needs a registry credential on the Container App. Approve the
  choice before step 6.

## 1. Cloud service setup (story `s-cloud-service-setup`)

- [ ] **You. Azure budget alert FIRST** (spec §13). Set a low-threshold alert, which acts as a tripwire
  rather than a hard limit, before any other Azure resource exists.
- [ ] **You. Neon.** Create the project and database, then copy the connection string into your password
  manager, not into a file in the repo.
- [ ] **You. Cloudflare R2.**
  - Create one bucket.
  - Create an API token scoped to that bucket with read and write access.
  - Note the account's S3 endpoint (`https://<account-id>.r2.cloudflarestorage.com`), the access key ID and the secret.
  - **You don't need CORS.** Browsers never upload to R2: photos go through the backend
    (`POST /api/trips/{slug}/stops/{stopId}/photos`, one idempotent request each, decision-log Entry 20).
    Presigned GET URLs are loaded by `<img>` tags, which don't make CORS requests.
- [ ] **You. Microsoft Graph app registration (for the OneDrive archive).**
  - Register an app that allows personal Microsoft accounts.
  - Grant the delegated `Files.ReadWrite` and `offline_access` permissions.
  - Add a redirect URI and create a client secret.
  - Complete the one-time OAuth consent as yourself, then store the resulting refresh token.
  - The exact scope and request shape have not been checked against Graph. Step 5 checks them.
- [ ] **You (devops drafts it). Container Apps environment.** Use the free tier, with scale-to-zero
  (min replicas 0). Point external ingress at target port **8000** and allow HTTPS only
  (`allowInsecure: false`). Use **multiple-revision mode**, because the rollback in step 8 depends on it.
  The definition goes in `infra/azure/`, which needs your approval.

## 2. Secrets and env vars (names only)

Set these with `az containerapp secret set`. Reference them as env vars from the app. The names must
match `.env.example` (see `infra/azure/README.md` and `.claude/skills/deploy/SKILL.md`).

| Name | Kind | Source |
|---|---|---|
| `DATABASE_URL` | secret | Neon (see the sslmode blocker in step 0) |
| `S3_ENDPOINT_URL` | secret | R2 account endpoint |
| `S3_ACCESS_KEY_ID` | secret | R2 token |
| `S3_SECRET_ACCESS_KEY` | secret | R2 token |
| `S3_BUCKET_NAME` | secret | R2 bucket name |
| `S3_REGION` | plain env | `auto` |
| `GRAPH_CLIENT_ID` | secret | app registration |
| `GRAPH_CLIENT_SECRET` | secret | app registration |
| `GRAPH_REFRESH_TOKEN` | secret | OAuth consent |
| `GRAPH_ONEDRIVE_FOLDER` | plain env | target folder path |
| `ENVIRONMENT` | plain env | a non-`local` value |

- [ ] **You.** Set every value yourself. `STATIC_FILES_DIR` is already set inside the image, so leave it
  alone.

## 3. Pre-deploy checks (local)

- [ ] **You or devops. Build from a fresh clone, not your working tree.** This keeps the host-generated
  `frontend/src/routeTree.gen.ts` out of the build context (`t-dockerignore-route-tree`, below).
- [ ] **You. Backend tests:** `cd backend && uv run pytest`. This includes `test_openapi_snapshot.py`,
  the drift guard between the backend and the committed OpenAPI snapshot.
- [ ] **You. Frontend build and tests:** `cd frontend && npm run build && npm test`. Run the build first.
- [ ] **You. Client drift check:** run the two regenerate commands from CLAUDE.md "Commands", then
  `git status`. There should be **no diff** in `frontend/openapi.json` or `frontend/src/api/gen/`.
- [ ] **You. Local smoke test of the production image:** `docker compose up --build`. Then:
  - Run `curl -i http://localhost:8000/api/health` and expect `200`.
  - Load `http://localhost:8000/` and expect the paste-link screen.
  - Load `http://localhost:8000/t/<local-rider-slug>` and expect the trip to render.
  - Use a trip in your local database only.

## 4. Seed the trip in production (story `s-seed-trip-record`)

Only you do this. Keep it out of any agent session, because it prints the permanent slugs.

- [ ] Point `DATABASE_URL` at Neon in your own shell, not in a committed file.
- [ ] Run the migrations: `cd backend && uv run python -m app.data.migrate`
- [ ] Seed the trip: `cd backend && uv run python -m app.data.seed_trip --name "<trip name>" --start-date YYYY-MM-DD`
- [ ] Save both printed links in your password manager straight away. That printout is the only copy.
  To recover them later, run `SELECT rider_slug, viewer_slug FROM trips` against Neon.
- [ ] If the script refuses because a trip already exists, **don't** delete the trip and re-seed.
  Recover the existing slugs with the query above instead.

## 5. OneDrive preflight (`t-onedrive-preflight-check`)

Only you do this, because it needs real `GRAPH_*` values.

- [ ] Upload at least one test photo, so that something is waiting to be archived. Run
  `cd backend && uv run python -m app.storage.onedrive_sync` with production env values in your shell.
- [ ] Expected result: exit code 0, and the file appears in `GRAPH_ONEDRIVE_FOLDER` named
  `<photo id>.<ext>`. A non-zero exit means one of these: the refresh token has lapsed (401 twice), Graph
  is throttling (429/503), or Graph has rejected the request shape. The URL form, token scope and
  `conflictBehavior` placement are all **unverified** until this passes.
- [ ] If the uploaded filename looks wrong, check `t-onedrive-filename-url-encoding`. That one-line
  fix was meant to be folded into this task.
- [ ] Run it again close to departure, so the refresh token is known to be healthy when the trip starts.

## 6. Deploy (following `.claude/skills/deploy/SKILL.md`)

devops runs these steps after you have confirmed the cutover.

- [ ] Build the image from the repo-root `Dockerfile` (from the fresh clone).
- [ ] Push it to the registry you chose, with a unique tag. Don't reuse `latest`.
- [ ] Update the Container App to use the new tag.
- [ ] Note the revision name. It becomes the rollback target for the next deploy.

## 7. Post-deploy checks

- [ ] **You. HTTPS.** Open `https://<app>.<region>.azurecontainerapps.io`. HTTPS is required, not a nice
  extra: the service worker, `navigator.geolocation` and `crypto.randomUUID()` (queue ids) only work in a
  secure context. Also check that plain `http://` does not serve the app.
- [ ] **You.** Check that `GET /api/health` returns `200` and that `/` shows the paste-link screen.
- [ ] **You.** Check that the rider link loads the trip and shows the display-name prompt and the
  "Add stop" link, and that the viewer link loads the same trip with neither.
- [ ] **You. Offline cold open.** This is the manual check left open in `t-offline-app-shell`'s AC3.
  1. Open the rider link in a real browser.
  2. In DevTools, under Application, confirm the service worker is active.
  3. Go offline and reload. The trip name should render from the saved trip data.
  4. Do the same on a phone in airplane mode.
- [ ] **You.** Run `docs/real-device-test-plan.md` against this URL.
- [ ] **You.** Share the links: the **rider link** goes only to the riders, and the **viewer link** can
  go to family and friends. Tell iPhone riders to add the app to their home screen, paste the link
  there, and capture stops **from the installed app, not from Safari** (decision-log Entry 18).

## 8. Rollback

- [ ] **devops, after your confirmation.** Send traffic back to the last known-good revision with
  `az containerapp ingress traffic set ... --revision-weight <good-revision>=100`, or reactivate that
  revision. Don't rebuild forward under pressure.
- [ ] **Schema is forward-only.** An image rollback does not undo a migration, so check that the older
  image still works with the current schema before you rely on it.
- [ ] **A leaked rider link is a different kind of incident.** Handle it by rotating `trips.rider_slug`
  with one SQL `UPDATE` (decision-log Entry 21), not by rolling back. Items already queued under the old
  slug will show as failed and can be dismissed; share the new link.

## Triggered debt to decide at cutover

This cutover fires the triggers on the four items below. A fired trigger is not approval: each still
goes through `ba` and the pipeline, or you decide to accept it.

| Task | Why it matters now | Options |
|---|---|---|
| `t-api-healthcheck-wiring` | Its trigger is `s-deploy-cutover` itself. Nothing probes `/api/health`. | Configure the Container Apps liveness probe on `/api/health` (the `infra/` part needs approval). **In the same patch**, dev must update the route's `description=` in `backend/app/main.py`, which currently says nothing is wired to it. |
| `t-access-log-slug-exposure` | The uvicorn access log records trip slugs from request URLs. Container Apps sends stdout to Log Analytics by default, so the slugs become stored and searchable. | Accept it, turn off the Log Analytics destination, or add a uvicorn flag or logging config at startup (devops). **Don't** have the app reconfigure `uvicorn.access` at import time: that approach was rejected (decision-log Entries 7b and 13; `core/errors.py`). |
| `t-dockerignore-route-tree` | A build from a dev working tree carries a stale `routeTree.gen.ts` into the image. | Build from a fresh clone (step 3). Add the file to `.dockerignore` if any CI or build step reads `frontend/src/` before `vite build`. |
| `t-onedrive-preflight-check` | Nothing is known about whether Graph accepts the request until this runs. | Step 5. It also depends on the scheduling decision in step 0. |
