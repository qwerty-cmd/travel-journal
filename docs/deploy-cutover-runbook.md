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

- [x] **Fixed (`t-neon-sslmode-url`). The Neon `sslmode` trap.** Neon's console connection string ends in
  `?sslmode=require&channel_binding=require`. asyncpg rejects both parameters. `normalize_database_url` in
  `backend/app/data/db.py` now turns `sslmode` into asyncpg's `ssl` and drops `channel_binding`, so
  **paste the Neon console string into `DATABASE_URL` as-is.** Don't edit it by hand. Caveats:
  - A live Neon connection has still not been tested. Step 4 is the first real connect.
  - If you ever set or rotate the password by hand, it must not contain a raw `?` or `#`.
    Percent-encode those characters (`t-neon-sslmode-url` filed debt).
  - If you still see `TypeError: connect() got an unexpected keyword argument ...`, that is a parameter
    problem, not a credentials problem.
- [ ] **You. The migration runner has never run against Neon**, only against local Postgres 16. Treat the
  first run in step 4 as the test.
- [x] **Decided (`t-onedrive-sync-scheduler`). The OneDrive sync runs as a scheduled Container Apps Job.**
  `python -m app.storage.onedrive_sync` does one pass and exits. It is scheduled by the job defined in
  `infra/azure/README.md` ("OneDrive sync job"): every 30 minutes (UTC), from the same image as the app,
  one run at a time. The job is **defined but not yet provisioned**, and step 1 creates it. Until it
  exists nothing is archived. That does not block the app, because S3/R2 is the source of truth.
- [x] **Decided: the container registry is GitHub Container Registry (GHCR)**, by your choice. Azure
  Container Registry (ACR) is not used, because it has no free tier. If the GHCR package is private, the
  Container App needs a GHCR registry credential: a GitHub token with `read:packages`, stored as a secret
  and never pasted into an agent session. A public package needs no credential.

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
  - Register an app with supported account type **"Accounts in any organizational directory and personal
    Microsoft accounts"**. The sync and the helper below use the `/common` endpoint, which only accepts a
    personal account under this setting.
  - Grant the delegated `Files.ReadWrite` and `offline_access` permissions.
  - Add a platform of type **Web** and register the redirect URI `http://localhost:8765` on it.
  - Create a client secret.
  - Get the refresh token with the one-time helper (`t-graph-refresh-token-helper`). **You run this
    yourself in your own terminal, never an agent.** It opens a real Microsoft sign-in and prints a
    live credential.
    1. Make sure `backend/.env` is complete. `GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET` must be set, and
       so must `DATABASE_URL` and the `S3_*` vars, because settings are validated as a whole. If
       something is missing, the helper lists the missing field names (never their values) and stops.
    2. Run `cd backend && uv run python -m app.storage.get_refresh_token`.
    3. Sign in with the Microsoft account whose OneDrive gets the archive, and accept the consent
       prompt. If the browser doesn't open, visit the URL the helper prints.
    4. Copy the refresh token it prints into your password manager and into the Azure secret
       `GRAPH_REFRESH_TOKEN` (step 2). The helper writes no file.
    5. Never paste the token into chat, including an agent session, and never commit it.
  - **When to mint, and how long it lasts** (decision-log Entry 22). The sync reuses this one token for
    the whole trip. It works until about **90 days after you mint it** (Microsoft publishes no exact
    number for personal accounts, so treat 90 as approximate).
    - Mint it **as close to departure as practical**.
    - Record two dates in your password manager, next to the secrets: the **mint date**, and the
      **client secret's expiry date** (shown in the app registration when you created the secret).
      An expired client secret also stops the token working.
    - Check that **mint date + 90 days falls after the trip ends**, and that the client secret expires
      after the trip ends too. If either doesn't, re-mint closer to departure or create a longer-lived
      client secret.
    - The token can also die early if you revoke your Microsoft sessions, remove the app's consent, or
      reset your password.
    - **Re-minting** means re-running the helper above, then updating `GRAPH_REFRESH_TOKEN` on **both**
      the Container App and the OneDrive sync Job. They have separate secret stores.
  - The exact scope and request shape have not been checked against Graph. Step 5 checks them.
- [ ] **You (devops drafts it). Container Apps environment.** Use the free tier, with scale-to-zero
  (min replicas 0). Point external ingress at target port **8000** and allow HTTPS only
  (`allowInsecure: false`). Use **multiple-revision mode**, because the rollback in step 8 depends on it.
  The definition goes in `infra/azure/`, which needs your approval.
- [ ] **You (devops drafts it; needs your approval at deploy time). OneDrive sync job.** Create the
  Container Apps Job described in `infra/azure/README.md` ("OneDrive sync job"). The job has its **own
  secret store**, separate from the app's, so set its secrets too. Use the same names and the same values
  as the app (step 2).

## 2. Secrets and env vars (names only)

Set these with `az containerapp secret set`. Reference them as env vars from the app. The names must
match `.env.example` (see `infra/azure/README.md` and `.claude/skills/deploy/SKILL.md`).

| Name | Kind | Source |
|---|---|---|
| `DATABASE_URL` | secret | Neon console string, pasted as-is (sslmode is handled, see step 0) |
| `S3_ENDPOINT_URL` | secret | R2 account endpoint |
| `S3_ACCESS_KEY_ID` | secret | R2 token |
| `S3_SECRET_ACCESS_KEY` | secret | R2 token |
| `S3_BUCKET_NAME` | secret | R2 bucket name |
| `S3_REGION` | plain env | `auto` |
| `GRAPH_CLIENT_ID` | secret | app registration |
| `GRAPH_CLIENT_SECRET` | secret | app registration |
| `GRAPH_REFRESH_TOKEN` | secret | printed by the `get_refresh_token` helper (step 1) |
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
- [ ] Run it again close to departure **with a freshly minted token** (step 1, "When to mint"), so the
  token is known to be healthy when the trip starts and its ~90-day life covers the whole trip. Make sure
  the app and the Job both carry that new token.
- [ ] **Caveat once the sync job exists (step 1).** Don't run this laptop preflight while a scheduled run
  could be active, because two passes at once can overlap. Either trigger the job itself with
  `az containerapp job start`, or run the laptop pass just after a scheduled run has finished.

### If archiving stops mid-trip

Nothing is lost. Photos stay in R2, which is the real copy, and OneDrive is only an archive. The next
successful run after the fix archives everything that was missed.

- [ ] **Check the Job's logs.** A token problem looks like `Graph token request rejected: HTTP <status>`
  followed by `No Graph access token -- run aborted, nothing archived`. The log does not yet say *why*
  the token was rejected (`t-graph-token-error-code-logging`), so if Microsoft itself is having an
  outage the lines look the same. If the rejection keeps happening across several runs, treat it as an
  expired or revoked token.
- [ ] **Re-mint** the refresh token with the helper (step 1), in your own terminal.
- [ ] **Update `GRAPH_REFRESH_TOKEN`** on both the Container App and the sync Job.
- [ ] If re-minting fails too, check whether the client secret has expired (step 1, the date you
  recorded). If it has, create a new one and update `GRAPH_CLIENT_SECRET` in both places first.

## 6. Deploy (following `.claude/skills/deploy/SKILL.md`)

devops runs these steps after you have confirmed the cutover.

- [ ] Build the image from the repo-root `Dockerfile` (from the fresh clone).
- [ ] Push it to GHCR (`ghcr.io/<owner>/<image>`) with a unique tag. Don't reuse `latest`.
- [ ] Update the Container App to use the new tag.
- [ ] Update the OneDrive sync job to the same image tag (`az containerapp job update --image`), so the
  archive never runs older code than the app.
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

This cutover fires the triggers on the items below. (`t-onedrive-main-untested` is ordinary debt, not
triggered debt: provisioning the Job is the point where it gets re-classified.) A fired trigger is not
approval: each still goes through `ba` and the pipeline, or you decide to accept it.

| Task | Why it matters now | Options |
|---|---|---|
| `t-api-healthcheck-wiring` | Its trigger is `s-deploy-cutover` itself. Nothing probes `/api/health`. | Configure the Container Apps liveness probe on `/api/health` (the `infra/` part needs approval). **In the same patch**, dev must update the route's `description=` in `backend/app/main.py`, which currently says nothing is wired to it. |
| `t-access-log-slug-exposure` | The uvicorn access log records trip slugs from request URLs. Container Apps sends stdout to Log Analytics by default, so the slugs become stored and searchable. | Accept it, turn off the Log Analytics destination, or add a uvicorn flag or logging config at startup (devops). **Don't** have the app reconfigure `uvicorn.access` at import time: that approach was rejected (decision-log Entries 7b and 13; `core/errors.py`). |
| `t-dockerignore-route-tree` | A build from a dev working tree carries a stale `routeTree.gen.ts` into the image. | Build from a fresh clone (step 3). Add the file to `.dockerignore` if any CI or build step reads `frontend/src/` before `vite build`. |
| `t-onedrive-preflight-check` | Nothing is known about whether Graph accepts the request until this runs. | Step 5. Scheduling is decided (step 0, `t-onedrive-sync-scheduler`), so once the job exists, follow step 5's caveat about overlapping runs. |
| `t-settings-error-hides-input` | If an app revision or the sync Job starts with a required env var missing, pydantic's `ValidationError` includes `input_value` tails of the other settings, **including the `GRAPH_*` secrets**. That stderr goes to the platform logs. | **Recommended before you provision the app or the Job (step 1):** have dev add `hide_input_in_errors=True` to `Settings.model_config` in `backend/app/core/config.py`. Until then, check that every secret in step 2 is set before the first revision or Job run starts. |
| `t-onedrive-per-photo-isolation` | Fires once the sync Job is actually scheduled (step 1). The S3 read and the read of Graph's returned `id` sit outside the per-photo `try`, so a missing S3 object or a 2xx body without an `id` aborts the whole run instead of skipping that one photo. No path today produces either case, and qa confirmed the traceback leaks no secret. | Accept it, or have dev move both statements inside the existing `try` and `continue`, the same way HTTP failures are handled. |
| `t-onedrive-main-untested` | `main()`'s configured path (real session, real repositories, real refresh token) has no automated test. qa verified it is correct by running it, so what's missing is regression protection. Once the Job is provisioned, `main()` has a real consumer. | Re-classify it when the Job is provisioned (step 1), not before. |
