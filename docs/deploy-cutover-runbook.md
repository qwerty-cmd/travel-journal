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

- [x] **You. Azure budget alert FIRST** (spec §13). **Set by you, 2026-09-30.** Set a low-threshold alert, which acts as a tripwire
  rather than a hard limit, before any other Azure resource exists. Do it in the portal: Cost Management → Budgets → Add, with an
  email alert. Create the resource group only after the alert exists.
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
       `GRAPH_REFRESH_TOKEN` on the OneDrive sync Job (step 2). The helper writes no file.
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
    - **Re-minting** means re-running the helper above, then updating `GRAPH_REFRESH_TOKEN` on the
      **OneDrive sync Job only**. The Container App doesn't carry `GRAPH_*` (least privilege: the web app
      never reads them; see step 2).
  - The exact scope and request shape have not been checked against Graph. Step 5 checks them.
- [ ] **You (devops drafts it). Container Apps environment.** Use the free tier, with scale-to-zero
  (min replicas 0). Point external ingress at target port **8000** and allow HTTPS only
  (`allowInsecure: false`). Use **multiple-revision mode**, because the rollback in step 8 depends on it.
  The definition is drafted in `infra/azure/README.md` (`t-owner-container-app-definition`); running it
  needs your approval. Section "Container Apps environment and app", in order: the environment, the
  Container App (the step 2 secrets marked "App + Job"; no `GRAPH_*`; `--max-replicas 1`), then the
  `/api/health` startup and liveness probes applied with the `az rest` script
  (`t-infra-container-apps-probe`, see the table at the end). **These probes have already been applied
  to the live app once (2026-09-30) and verified by a raw ARM read**, so on that app this step is done;
  the instructions below are what to do on a rebuilt app, or after any later template change. The script
  sends a plain `application/json` PATCH; the call is long-running, so it answers `202` with no body and
  the script proves the probes landed by re-reading the app until they appear (up to 3 minutes) rather
  than by reading the response. It generates a **fresh revision suffix on every run** — a PATCH merges,
  so reusing the stored suffix makes ARM refuse the whole update while still answering `202` — and it
  polls a raw `az rest` GET for `properties.deploymentErrors`, because `az containerapp show` drops that
  field and would hide exactly this refusal. Run it as a script file or inside `bash <<'EOF' ... EOF` —
  it exits non-zero on failure, which would end a shell you pasted it into. Then check traffic and health
  on the revisions, as that section says, and **deactivate the superseded revisions** (§8) — each one
  left active keeps a replica and its own Neon connection pool.
- [ ] **You (devops drafts it; needs your approval at deploy time). OneDrive sync job.** Create the
  Container Apps Job described in `infra/azure/README.md` ("OneDrive sync job"). The job has its **own
  secret store**, separate from the app's. Set the shared secrets with the same names and values as the
  app, plus the `GRAPH_*` values, which live on the Job only (step 2).

## 2. Secrets and env vars (names only)

Secrets are passed at create time (`--secrets`, see `infra/azure/README.md` "Container Apps environment and app" and "OneDrive sync
job") and changed later with `az containerapp secret set` / `az containerapp job secret set`. Each is
referenced as an env var. The names must match `.env.example` (see `infra/azure/README.md` and
`.claude/skills/deploy/SKILL.md`). **Set on** says which resource carries each one. `GRAPH_*` are on
the **Job only**, for least privilege: the web app never reads them, only the sync does.

| Name | Kind | Set on | Source |
|---|---|---|---|
| `DATABASE_URL` | secret | App + Job | Neon console string, pasted as-is (sslmode is handled, see step 0) |
| `S3_ENDPOINT_URL` | secret | App + Job | R2 account endpoint |
| `S3_ACCESS_KEY_ID` | secret | App + Job | R2 token |
| `S3_SECRET_ACCESS_KEY` | secret | App + Job | R2 token |
| `S3_BUCKET_NAME` | secret | App + Job | R2 bucket name |
| `S3_REGION` | plain env | App + Job | `auto` |
| `S3_PUBLIC_ENDPOINT_URL` | **leave unset** | neither | Only for local runs, where the API and the browser reach storage at different addresses (step 3). R2's endpoint works for both |
| `GRAPH_CLIENT_ID` | secret | **Job only** | app registration |
| `GRAPH_CLIENT_SECRET` | secret | **Job only** | app registration |
| `GRAPH_REFRESH_TOKEN` | secret | **Job only** | printed by the `get_refresh_token` helper (step 1) |
| `GRAPH_ONEDRIVE_FOLDER` | plain env | **Job only** | target folder path |
| `ENVIRONMENT` | plain env | App + Job | a non-`local` value |

- [ ] **You.** Set every value yourself. `STATIC_FILES_DIR` is already set inside the image, so leave it
  alone.

## 3. Pre-deploy checks (local)

- [ ] **You or devops. Build from a clean, committed working tree** (`git status --porcelain` prints
  nothing). `.dockerignore` already keeps host-generated files (`.env`, `.venv`, `node_modules`,
  `frontend/dist`, `routeTree.gen.ts`) out of the build context, so a separate clone is not needed.
  A clean tree matters because step 6 tags the image with the commit hash.
- [ ] **You. Backend tests:** `cd backend && uv run pytest`. This includes `test_openapi_snapshot.py`,
  the drift guard between the backend and the committed OpenAPI snapshot.
- [ ] **You. Frontend build and tests:** `cd frontend && npm run build && npm test`. Run the build first.
- [ ] **You. Client drift check:** run the two regenerate commands from CLAUDE.md "Commands", then
  `git status`. There should be **no diff** in `frontend/openapi.json` or `frontend/src/api/gen/`.
- [ ] **You. Local smoke test of the production image** (`t-owner-compose-smoke-test`). `docker compose up` is not this test: it
  bind-mounts `./backend` over the image and runs `--reload`. Run the built image as-is instead,
  with no bind mount, against compose's Postgres and MinIO. All commands run from the repo root.
  1. Start only the backing services (this also creates the MinIO bucket), and stop the compose `api`
     if it is running, because it holds port 8000:
     `docker compose up -d postgres minio minio-init && docker compose stop api`
  2. Migrate and seed the **local** database. Both commands use `backend/.env`, whose `DATABASE_URL`
     points at `localhost:5432`:
     - `cd backend && uv run python -m app.data.migrate`
     - `cd backend && uv run python -m app.data.seed_trip --name "Smoke trip" --start-date 2026-06-01`
       This prints the local rider and viewer links. If a local trip already exists it refuses; get the
       slugs with `docker compose exec postgres psql -U bike_trip -c "SELECT rider_slug, viewer_slug FROM trips"`.
  3. Build the image: `docker build -t bike-trip-journal:smoke .`
  4. Run it on compose's network (`docker network ls` shows the name; by default it is
     `<repo-directory>_default`). `--env-file` supplies the S3 credentials, bucket and the rest, and the
     `-e` flags replace the host-oriented values with compose service names, as the compose `api` does.
     Unlike compose, `docker run --env-file` does not strip quotes, so `backend/.env` values must be
     unquoted.
     ```sh
     docker run --rm -p 8000:8000 --network <repo-directory>_default \
       --env-file backend/.env \
       -e DATABASE_URL=postgresql://bike_trip:bike_trip@postgres:5432/bike_trip \
       -e S3_ENDPOINT_URL=http://minio:9000 \
       -e S3_PUBLIC_ENDPOINT_URL=http://localhost:9000 \
       bike-trip-journal:smoke
     ```
     **Keep the `S3_PUBLIC_ENDPOINT_URL` line whenever you run the image outside compose against a
     local MinIO.** The API reaches MinIO as `minio:9000`, but your browser can't resolve that name.
     Photo links are signed for `S3_PUBLIC_ENDPOINT_URL` when it is set, so without it every photo
     fails to load. Compose sets it for its own `api` service. In production it stays unset (step 2).
  5. Then:
     - Run `curl -i http://localhost:8000/api/health` and expect `200`.
     - Load `http://localhost:8000/` and expect the paste-link screen.
     - Load `http://localhost:8000/t/<local-rider-slug>` and expect the trip to render.
     - Use a trip in your local database only.
     - Check `docker ps`: after about 15 seconds the container shows `(healthy)`.

## 4. Seed the trip in production (story `s-seed-trip-record`, `t-owner-production-seed`)

**Done, 2026-09-30.** Production holds one trip, "test-trip". To rename it, `UPDATE` that row; `seed_trip`
refuses a second trip. The rider link was pasted into an agent chat, so you rotated `trips.rider_slug`
on 2026-09-30 (§8, "A leaked rider link"). Share only the new rider link.

Only you do this. Keep it out of any agent session, because it prints the permanent slugs.

- [ ] Point `DATABASE_URL` at Neon in your own shell, not in a committed file.
- [ ] Run the migrations: `cd backend && uv run python -m app.data.migrate`
- [ ] Seed the trip: `cd backend && uv run python -m app.data.seed_trip --name "<trip name>" --start-date YYYY-MM-DD`
- [ ] Save both printed links in your password manager straight away. That printout is the only copy.
  To recover them later, run `SELECT rider_slug, viewer_slug FROM trips` against Neon.
- [ ] If the script refuses because a trip already exists, **don't** delete the trip and re-seed.
  Recover the existing slugs with the query above instead.

## 5. OneDrive preflight (`t-onedrive-preflight-check`, owner task)

Only you do this, because it needs real `GRAPH_*` values.

- [ ] Upload at least one test photo, so that something is waiting to be archived. Run
  `cd backend && uv run python -m app.storage.onedrive_sync` with production env values in your shell.
- [ ] Expected result: exit code 0, and the file appears in `GRAPH_ONEDRIVE_FOLDER` named
  `<photo id>.<ext>`. A non-zero exit means one of these: the refresh token has lapsed (401 twice), Graph
  is throttling (429/503), or Graph has rejected the request shape. The URL form, token scope and
  `conflictBehavior` placement are all **unverified** until this passes.
- [ ] **Archive isolation.** Upload a few good photos plus one you expect to fail. Expected: the good
  photos archive and the bad one stays pending (it is retried on the next run), with one log line for it.
  The run exits non-zero, because a photo failed; that is expected here.
  One bad photo no longer aborts the run (`t-onedrive-per-photo-isolation`, done in `21d34ec`). A
  practical way to make one fail: delete its object from R2 by hand, then remove that test photo's row
  afterwards.
- [ ] **Encoded filenames on real Graph (`t-onedrive-graph-name-charset`).** Filenames are already
  percent-encoded (`t-onedrive-filename-url-encoding`, done in `21d34ec`), so a name with `#` or `?`
  no longer breaks the upload URL. What is still unverified is what real Graph does with an encoded `/`
  (`%2F`) and with characters OneDrive forbids in names (`? : / \ | " * < >`). Every real id is a UUID,
  so this only matters if a non-UUID id ever appears. Check the archived name matches `<photo id>.<ext>`,
  and record what Graph did in `t-onedrive-graph-name-charset`'s notes.
- [x] **Production run passed, 2026-09-30.** A run of the sync Job archived a photo to OneDrive.
- [ ] Run it again close to departure **with a freshly minted token** (step 1, "When to mint"), so the
  token is known to be healthy when the trip starts and its ~90-day life covers the whole trip. Make sure
  the Job carries that new token (the Container App has no `GRAPH_*`, step 2).
- [ ] **Caveat once the sync job exists (step 1).** Don't run this laptop preflight while a scheduled run
  could be active, because two passes at once can overlap. Either trigger the job itself with
  `az containerapp job start`, or run the laptop pass just after a scheduled run has finished.
- **If you ever recreate the sync Job**, use the create block in `infra/azure/README.md`; it carries
  the working command shape (`t-sync-job-command-dash-arg`). If your copy of the README still has
  `--command "sh" "-c" ...`, it is out of date: az swallows the bare `-c`, and every run fails with
  `sh: 0: cannot open cd /app/backend ...`.

### If archiving stops mid-trip

Nothing is lost. Photos stay in R2, which is the real copy, and OneDrive is only an archive. The next
successful run after the fix archives everything that was missed.

- [ ] **Check the Job's logs.** A token problem looks like
  `Graph token request rejected: HTTP <status> (error: <code>, codes: <AADSTS codes>)` followed by
  `No Graph access token -- run aborted, nothing archived`. The error code says why
  (`t-graph-token-error-code-logging`, done in `21d34ec`): `invalid_grant` (for example with
  `AADSTS700082`) means the token has expired or been revoked, so re-mint. If both read `none given` and
  the status is a 5xx, Microsoft is more likely having an outage; wait for the next runs.
- [ ] **Re-mint** the refresh token with the helper (step 1), in your own terminal.
- [ ] **Update `GRAPH_REFRESH_TOKEN`** on the sync Job only (`az containerapp job secret set`, see
  `infra/azure/README.md` "OneDrive sync job"). The Container App doesn't carry it.
- [ ] If re-minting fails too, check whether the client secret has expired (step 1, the date you
  recorded). If it has, create a new one and update `GRAPH_CLIENT_SECRET` on the sync Job first.

## 6. Deploy (following `.claude/skills/deploy/SKILL.md`; `t-owner-cutover` covers §6–§7)

Deploys run through `.github/workflows/deploy.yml`, approved by you (decision-log Entry 32). The `az`
steps further down are the manual fallback.

Replace every `<placeholder>`. `<owner>/<repo>` is the GitHub path and must be **lower case**
(GHCR rejects upper case). It is the same image path as `infra/azure/README.md`.

**Images are built by CI, not by hand.** `.github/workflows/ci.yml` publishes
`ghcr.io/<owner>/<repo>:<full-commit-sha>` on every merge to `main`, after the backend and frontend
checks pass. `ci.yml` only builds and publishes; `deploy.yml` deploys, and only when you dispatch and approve it.
See "Image publishing (GitHub Actions to GHCR)" at the end of this section for package visibility and access.

### Deploy with the workflow

1. **Pick the SHA.** The full 40-character SHA of the `main` commit to deploy. Its `ci.yml` run must be
   green (backend, frontend and publish).
2. **Dispatch.** GitHub → Actions → Deploy → Run workflow (branch `main`), or:
   `gh workflow run deploy.yml --ref main -f sha=<full-sha>`
   Optional inputs:
   - `attempt` (1..99, default 1). Attempt 1 names the revision `<app-name>--rel-<sha12>`; any other N names it
     `<app-name>--rel-<sha12>-a<N>`. A revision suffix can never be reused. If a revision already exists for the
     suffix and is failed or unhealthy, the run fails fast with a hint: re-dispatch with `attempt=N+1`, e.g.
     `gh workflow run deploy.yml --ref main -f sha=<full-sha> -f attempt=2`.
   - `xff_burst` (default off), see "Smoke checks" below.
3. **Approve.** The run waits in the `production` environment until you approve it (you are the required
   reviewer). Nothing starts before that. Only one deploy, rollback or cleanup runs at a time (`concurrency: deploy`).
   **Don't queue a second dispatch behind a pending one.** GitHub keeps one pending run per group, so a newer
   dispatch cancels the older pending run (e.g. a rollback queued behind a deploy). Cancel or wait instead.
4. **Watch it.** The run does these steps, and stops at the first failure:
   1. Verifies `main` contains the SHA, its CI run is green and the GHCR image exists, then checks out that SHA.
   2. Signs in to Azure with OIDC (no stored Azure secret).
   3. Records the serving revision and pins 100% of traffic to it, so the update cannot move traffic by itself.
   4. Migrates Neon: `job update --image` on the migrate Job, then `job start` with no options, and polls
      the execution to Succeeded. Traffic still sits on the old revision, so a migration must keep working with
      the previous image (step 8, "Schema is forward-only").
   5. Renders `infra/azure/app.yaml` (explicit `envsubst` list, which includes `PREV_REVISION`) and runs
      `az containerapp update --yaml`, creating revision `<app-name>--rel-<first 12 chars of SHA>`
      (`-a<N>` appended when `attempt` > 1) at 0% traffic. The YAML's ingress `traffic:` list pins
      `${PREV_REVISION}` at 100 and the latest revision at 0. The workflow asserts the revision name and image;
      if traffic moved anyway it re-pins, verifies, and fails if the pin did not hold.
      Redeploying the same SHA takes a traffic-only path and creates no revision.
   6. Warms the new revision through a `candidate` label until it is healthy, then shifts 100% of traffic.
   7. Updates the OneDrive sync Job to the same image and asserts the app and Job images are equal.
   8. Runs `infra/azure/smoke.sh` against `APP_BASE_URL`.
   9. Writes the run summary (always, even on failure).
5. **Read the summary.** It names the revision now serving traffic, the previous revision (the rollback
   target) and the exact `rollback.yml` command. There is no automatic rollback: if smoke fails after the
   traffic shift, you decide whether to roll back (step 8).

**Smoke checks (`infra/azure/smoke.sh <base-url> [--xff-burst]`).** `/api/health` is 200; `/` is the SPA;
plain `http://` does not serve the app; a same-origin sign-in with bad credentials is 401 (a 403 means the
ingress rewrote Host). `--xff-burst` is **not** the 121-request public-read check in §7a: it uses the
**signin** bucket (10 per 15 minutes per IP), expecting 9 x 401 then one 429. It locks the runner's IP
out of signin for about 15 minutes, which is why it is off by default. The §7a manual check and the
second-network half of it (`t-am-verify-aca-xff`) stay yours.

**What stays manual:** entering secret values in ACA, the GHCR PAT, `grant_leader` / `reset_account` in your
own shell (§7a), real-device tests (§7), and the second-network XFF check.

### One-time setup for the deploy workflows

Do these once, in your own shell and the GitHub UI. Use placeholders in anything you write down; never paste
real values into the repo or an agent chat.

- [ ] **Azure identity.** A user-assigned managed identity, a federated credential with subject
  `repo:<owner>/<repo>:environment:production`, and Contributor on the app's **resource group only**.
  Commands: decision-log Entry 32, "Owner setup" (step 1).
- [ ] **GitHub `production` environment** (Settings → Environments): you as required reviewer, and a
  deployment-branch rule restricting it to `main`. Add these 12 environment **variables** (names only here;
  none is a secret): `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
  `ACA_APP_NAME`, `ACA_APP_CONTAINER_NAME`, `ACA_MIGRATE_JOB_NAME`, `ACA_MIGRATE_CONTAINER_NAME`,
  `ACA_SYNC_JOB_NAME`, `ACA_SYNC_CONTAINER_NAME`, `GHCR_USERNAME`, `APP_BASE_URL`. Their meanings are listed in
  the header of `.github/workflows/deploy.yml`.
- [ ] **GHCR pull credential.** A classic PAT with `read:packages` only, stored in ACA as the secret
  `ghcr-read-packages` on the app **and both Jobs** (the YAML names that secret via `passwordSecretRef`,
  never the value). Entry 32 "Owner setup" (step 3) shows the commands; confirm the generated secret name with
  `az containerapp show ... --query properties.configuration.registries`. Record the PAT's expiry in the handover.
- [ ] **GHCR package access.** Package settings → Manage Actions access → add this repository with the
  **Write** role. Until this is done the first publish on `main` fails with `permission_denied: read_package`.
- [ ] **Create the migrate Job once** from `infra/azure/migrate-job.yaml`. The committed YAML has no `secrets:`
  block on purpose, but its env `secretRef`s (`database-url`, `s3-endpoint-url`, `s3-access-key-id`,
  `s3-secret-access-key`, `s3-bucket-name`) do not exist on a Job that does not exist yet, so a plain
  `job create --yaml` on the committed file fails. Instead:
  1. Render the YAML with `envsubst` (`IMAGE`, `GHCR_USERNAME`; see the file header) to a temp file **outside the repo**.
  2. In that temp file only, add a `properties.configuration.secrets` list with those five names plus
     `ghcr-read-packages`. Read each value at a no-echo prompt (`read -rs`) and write it in; never type it on a
     command line or into a chat.
  3. `az containerapp job create --name <migrate-job-name> --resource-group <resource-group> --environment <env-name> --yaml <temp-file>`
  4. Delete the temp file. Never commit or paste it.

  There is no separate `job secret set` step for this Job. The sync Job (`infra/azure/sync-job.yaml`, create-only)
  is created or re-created the same way, with the same five secrets plus `graph-client-id`, `graph-client-secret`,
  `graph-refresh-token` and `ghcr-read-packages`. Later updates to either Job use `job update --image`.
- [ ] **First-run verification** (owner checks from the build notes), on the first dispatch:
  - the YAML traffic pin holds (the new revision shows 0% and the previous one 100% after `update --yaml`);
  - the `candidate` label URL format and the revision state strings the workflow polls are as expected;
  - `GITHUB_TOKEN` can read the private package;
  - `update --yaml` kept the registry credential (diff `infra/azure/app.yaml` against `az containerapp show -o yaml`:
    transport, 0.5 CPU / 1Gi, `ENVIRONMENT`, container name, PAT secret name);
  - the migrate Job's secrets and registry secret name are right, and `job create --yaml` did not need
    `properties.environmentId` added;
  - the run summary names the new and previous revision, and the app and Job images match;
  - the main-only deployment-branch rule is in place on `production`.

### Manual fallback (if the workflow is unavailable)

devops runs these steps after you have confirmed the cutover. They are what `deploy.yml` automates, in the
same order.

- [ ] **devops. Pick the tag.** Use the full 40-character SHA of the `main` commit being deployed. Its
  CI run must be green, and that run's summary shows the image and digest. `$TAG` is used by every
  step below:
  `TAG=<full-commit-sha>`
  For a public package, `docker buildx imagetools inspect ghcr.io/<owner>/<repo>:$TAG` confirms that
  the image exists.
- [ ] **Fallback only: build and push by hand.** Use this only if CI cannot publish (see the
  `infra/azure/README.md` section named above). **You. Log in to GHCR** with a GitHub classic personal access token that has
  `write:packages`. This is a different token from the `read:packages` one the Container App pulls with.
  Paste it at the prompt so it stays out of shell history, then press Ctrl-D:
  `docker login ghcr.io -u <github-user> --password-stdin`
  **devops. Build, tag and push** from a clean tree (step 3), with the full commit hash as the tag,
  the same scheme CI uses. Never reuse a tag and never use `latest`: rollback depends on each revision
  naming a distinct image. `--platform linux/amd64` is what Container Apps runs, and it matters if you
  build on Apple Silicon.
  ```sh
  TAG=$(git rev-parse HEAD)
  docker build --platform linux/amd64 -t bike-trip-journal:$TAG .
  docker tag bike-trip-journal:$TAG ghcr.io/<owner>/<repo>:$TAG
  docker push ghcr.io/<owner>/<repo>:$TAG
  ```
- [ ] **devops. Record the rollback target** before anything changes. This is the revision serving
  traffic now:
  `az containerapp ingress traffic show --name <app-name> --resource-group <resource-group> -o table`
  If it shows `latestRevision: true`, pin it to that revision's name first, so the update below does not
  move traffic by itself:
  `az containerapp ingress traffic set --name <app-name> --resource-group <resource-group> --revision-weight <current-revision>=100`
- [ ] **You. Migrate Neon BEFORE any traffic moves.** Use your own shell with `DATABASE_URL` pointed at
  Neon, as in step 4, from the same commit as `$TAG`:
  `cd backend && uv run python -m app.data.migrate`. A second run reports nothing applied. The old
  revision keeps serving during and after this, so a migration must keep working with the previous image
  too (see step 8, "Schema is forward-only").
- [ ] **devops. Update the Container App** to the new tag. In multiple-revision mode this creates a new
  revision. The suffix makes its name `<app-name>--rel-<tag>`, where `<tag>` is the **first 12
  characters** of the SHA. The full 40 would push the revision name past Azure's length limit:
  ```sh
  az containerapp update --name <app-name> --resource-group <resource-group> \
    --image ghcr.io/<owner>/<repo>:$TAG --revision-suffix rel-${TAG:0:12}
  ```
- [ ] **devops. Read the new revision's name and check its health**:
  ```sh
  az containerapp revision list --name <app-name> --resource-group <resource-group> \
    --query "[].{name:name, image:properties.template.containers[0].image, active:properties.active, health:properties.healthState, traffic:properties.trafficWeight}" -o table
  ```
  When it shows `Healthy`, move traffic to it:
  `az containerapp ingress traffic set --name <app-name> --resource-group <resource-group> --revision-weight <app-name>--rel-<tag>=100`
- [ ] **devops. Update the OneDrive sync Job to the same tag**, so the archive never runs different code
  from the app. The Job has no revisions: its next scheduled run uses the new image.
  ```sh
  az containerapp job update --name <job-name> --resource-group <resource-group> \
    --image ghcr.io/<owner>/<repo>:$TAG
  ```
  Confirm that both of these print the same image:
  `az containerapp show --name <app-name> --resource-group <resource-group> --query "properties.template.containers[0].image" -o tsv`
  `az containerapp job show --name <job-name> --resource-group <resource-group> --query "properties.template.containers[0].image" -o tsv`
- [ ] Write down the new revision name (`<app-name>--rel-<tag>`) and the previous one from the
  "rollback target" step. The previous one is what step 8 rolls back to.

### Image publishing (GitHub Actions to GHCR)

`.github/workflows/ci.yml` builds the repo-root `Dockerfile` (`linux/amd64`)
and pushes it to GHCR on every push to `main`, and on a manual
`workflow_dispatch` run on `main`. It publishes only after the `backend` and
`frontend` jobs pass. Pull requests run those checks but never publish.

- **Tag.** Exactly one tag per image, the full commit SHA:
  `ghcr.io/<owner>/<repo>:<full-sha>`, lower-cased. Never `latest`, never a
  floating tag. The run's summary shows the image reference and digest.
  The deploy steps in this section use that tag for
  both the app and the OneDrive sync Job.
- **Build only.** The workflow never logs in to Azure, runs `az`, changes a
  revision or traffic, or updates the Job. Its only credential is the per-run
  `GITHUB_TOKEN`, with `packages: write` in the publish job alone. Deploying
  is the separate `deploy.yml` (see "Deploy with the workflow" above).
  Building and pushing by hand is the fallback.
- **Package visibility.** The first push creates the GHCR package as
  **private**. Choose one of these:
  - **Public.** On GitHub, open the package, then Package settings → Change
    visibility → Public. The app and Job then need no registry credential:
    drop the three `--registry-*` flags (`infra/azure/README.md`, app and Job create commands).
  - **Private.** Keep it private and give the app and the Job a separate
    GitHub **classic PAT with `read:packages` only** as their registry
    credential (the `--registry-*` flags in `infra/azure/README.md`). Never use the workflow's
    `GITHUB_TOKEN` for this: it expires when the run ends.
- **403 on a later push.** If a publish run fails with `403 Forbidden` on
  push, the package is not linked to this repository, or the repository lacks
  write access to it. This happens, for example, if the package was first
  created by a manual push. Open Package settings → Manage Actions access, add
  this repository, and give it the **Write** role. Then re-run the workflow.
  (The same setting fixes `permission_denied: read_package`, seen on the first publish on `main`.)

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

## 7a. Accounts and membership (Entry 29)

Applies from the first deploy of the accounts milestone (`m5-accounts-membership`) onward. Every
command here is **owner-only**: run it in your own shell with `DATABASE_URL` pointed at Neon, as in
step 4. Agents never run these CLIs against production. Use real values only in your terminal; never
paste a username, trip id or recovery code into an agent chat.

### Migration 0003

- [ ] `backend/migrations/0003_accounts_membership.sql` is applied by the ordinary migrate step in
  step 6 ("Migrate Neon BEFORE any traffic moves"): `cd backend && uv run python -m app.data.migrate`.
  It only adds and relaxes (decision-log Entry 29 §12), so the previous image keeps working while traffic
  is still on it. Record in the handover that 0003 shows as applied.

### Make the owner the trip's first leader

Writes are authorised by an active `trip_members` row, not by the slug, so the trip needs
a leader before anyone can manage it.

- [ ] **The trip already exists (production today, step 4).** `seed_trip` refuses a second trip, so
  don't re-seed. Sign up in the deployed app first, then grant yourself leadership:
  `cd backend && uv run python -m app.data.grant_leader --trip-id <trip-id> --username <username>`
  Read `<trip-id>` with `SELECT id, name FROM trips`. It is idempotent: a second run reports the
  account "was already an active leader", and a rider is upgraded to leader.
- [ ] **A fresh database whose owner already has an account.** Seed and grant in one transaction:
  `cd backend && uv run python -m app.data.seed_trip --name "<trip name>" --start-date YYYY-MM-DD --leader-username <username>`
  Without `--leader-username` no membership is created; use `grant_leader` afterwards as above.

### Reset, disable, revoke

- [ ] **Reset a rider who lost both password and recovery code:**
  `cd backend && uv run python -m app.data.reset_account --username <username>`
  This issues a new recovery code, signs the account out everywhere and clears any lockout. The code is
  **printed once, after the commit**, to stdout only. Give it to the rider **out of band** (in person,
  phone, a private message they own), never through an agent session or a shared channel. The rider
  uses it at sign-in recovery. If stdout fails, the reset is still committed and the CLI says so; run
  it again to issue another code.
- [ ] **Disable an account:**
  `cd backend && uv run python -m app.data.reset_account --username <username> --disable`
  This signs it out everywhere and clears its lockout. No recovery code is issued.
  - **Reset does not re-enable a disabled account.** Running `reset_account` without `--disable` on
    a disabled account still prints a recovery code, but the account stays disabled and recovery
    answers 401. That code is useless.
  - **There is currently no CLI to re-enable an account.** Treat disabling as one-way until one exists.
- [ ] **Remove someone from a trip (leaders included):**
  `cd backend && uv run python -m app.data.revoke_member --trip-id <trip-id> --username <username>`
  It refuses to remove the trip's last active leader; grant another leader first. A second run reports
  "was already revoked". The account itself stays usable for other trips.

### After deploy: `TRUSTED_PROXY_HOPS` and Host passthrough (`t-am-verify-aca-xff`)

Rate limits are keyed by client IP, taken as the **right-most** `X-Forwarded-For` hop
(`TRUSTED_PROXY_HOPS`, default `1`, on the premise that the Container Apps ingress appends exactly one
hop). If that premise is wrong, one client can mint a fresh bucket per request by spoofing the header.

- [ ] **You. Spoofed-XFF burst.** (`smoke.sh --xff-burst` is a different, signin-bucket check; see §6
  "Smoke checks". It does not replace this one.) From one network, send 121 requests to a public-read endpoint (120 per
  minute per IP) with a spoofed header, within a minute:
  `for i in $(seq 121); do curl -s -o /dev/null -w "%{http_code}\n" -H "X-Forwarded-For: 1.2.3.4" https://<app-host>/api/v2/trips; done | sort | uniq -c`
  Expected: 120 x `200` and 1 x `429`. Then, inside the same minute, from a **second network** (for
  example a phone hotspot) send the same spoofed request. Expected: `200`, which shows the bucket follows
  the real client IP, not the spoofed value. Record both results in the handover.
- [ ] If the first network never gets a `429`, or the second network gets one, the ingress appends a
  different number of hops. `TRUSTED_PROXY_HOPS` then has to change in the Container App env, which is
  `infra/` and needs your explicit approval.
- [ ] **You. Host passthrough.** The CSRF check compares `Origin` with `Host`, so the ingress must pass
  the browser's original `Host` through unchanged. Sign in (or sign up) in a real browser on the
  deployed URL. Expected: it succeeds. A `403` on every same-origin write means the Host is being
  rewritten. That fails closed (nothing is bypassed), but nobody can write until it is fixed.

### Rolling back past Entry 29 is security-degrading

> **Warning.** Rolling the image back to a build from before Entry 29 restores **slug-bearer writes**:
> anyone holding a rider link can write again, without an account or membership. No schema rollback is
> needed (0003 only adds and relaxes, and the old image works against it), and legacy trips keep their
> slugs, but trips created after the cutover (NULL slugs) become unreachable. It is reversible but
> **security-degrading**. Treat it as a last resort, and roll forward again as soon as you can.

## 8. Rollback

### Roll back with the workflow

- [ ] **Dispatch `rollback.yml`** (Actions → Rollback → Run workflow, or
  `gh workflow run rollback.yml --ref main -f revision=<app-name>--<suffix>`) and approve it in the `production`
  environment. The deploy summary prints this exact command with the previous revision filled in. It validates the
  name, activates the revision if it is inactive, warms it through a `rollback` label until healthy, shifts 100% of
  traffic, verifies it, and runs `smoke.sh`. Optional input `sync_job_image` (default false) also sets the sync Job to
  that revision's image. It never builds and never creates a revision. Don't rebuild forward under pressure.
- [ ] **Migrations are not undone.** The database stays at the newer schema (see "Schema is forward-only" below).
- [ ] **Pre-Entry 29 targets are security-degrading.** If the target image does not contain the Entry 29 merge
  (PR #8), or it cannot be resolved, the run raises a warning annotation and a summary banner. It still
  rolls back, since it may be your last resort: read the warning at the end of step 7a first.

### Clean up superseded revisions with the workflow

- [ ] **Dispatch `deactivate-revisions.yml`** with `dry_run` left at its default (`true`) first. Read the list it
  prints, then re-run with `dry_run` off and approve it. It keeps every revision with traffic, the rollback target
  and the optional `keep` input, and refuses to deactivate anything that gained traffic since it read the list.
  The rollback target is the **newest active healthy revision before the oldest serving one**. To be certain it
  keeps the right one, pass deploy's previous revision (named in the deploy summary) as `keep`. The reasoning is
  the second item of "Manual fallback" below.
- [ ] **Don't queue this behind another run.** It shares the `deploy` concurrency group, and a newer dispatch
  cancels an older pending run. Wait for a running deploy to finish before dispatching a rollback or cleanup.

### Manual fallback (if the workflow is unavailable)

- [ ] **devops, after your confirmation.** Send traffic back to the last known-good revision with
  `az containerapp ingress traffic set ... --revision-weight <good-revision>=100`, or reactivate that
  revision. Don't rebuild forward under pressure. **If the good revision predates Entry 29, this
  rollback is security-degrading**: read the warning at the end of step 7a first.
- [ ] **After any template change, deactivate the revisions it superseded — but keep one.** In
  multiple-revision mode a superseded revision **stays active until you deactivate it**; moving traffic
  off it is not enough. Each active revision keeps a replica running, and each replica opens its own Neon
  connection pool — the thing `--max-replicas 1` was chosen to avoid (`infra/azure/README.md`, beside that
  flag). Keep the revision serving traffic **and one known-good rollback target** for the step above, then
  deactivate the rest:
  `az containerapp revision deactivate --name <app-name> --resource-group <resource-group> --revision <old-revision>`
  This is a live write on the running app, so **you approve it**; devops can run it after that, never on
  its own initiative. Applying the probes in step 1 left three active revisions this way
  (`t-owner-deactivate-superseded-revisions` — done 2026-09-30; the rollback target is
  `bike-trip-journal--rel-1bbe81bcbec5`).
### Rules that apply either way

- [ ] **Schema is forward-only.** An image rollback does not undo a migration, so check that the older
  image still works with the current schema before you rely on it.
- [ ] **A leaked rider link is a different kind of incident.** Handle it by rotating `trips.rider_slug`
  with one SQL `UPDATE` (decision-log Entry 21), not by rolling back. Items already queued under the old
  slug will show as failed and can be dismissed; share the new link.

## Triggered debt to decide at cutover

Nothing is waiting on you. Both items below are done.

| Task | Why it matters now | Options |
|---|---|---|
| `t-infra-container-apps-probe` | **Applied and verified, 2026-09-30 — nothing to decide.** Container Apps ignores the Dockerfile `HEALTHCHECK`, so the startup and liveness probes on `/api/health`:8000 are set on the app itself, with the `az rest` PATCH in `infra/azure/README.md` ("Container Apps environment and app"). The live round trip **has** now been run, and the probes were confirmed by a raw ARM read rather than by the script's own assertion: `provisioningState: Succeeded`, `deploymentErrors: None`, app serving throughout. Taking three rounds to get the script right is written up under `t-probe-patch-script-defects`. | Nothing, on this app. On a rebuilt app, run the script after creating it (step 1), then check health and traffic — and deactivate the superseded revisions (§8). |
| `t-owner-deactivate-superseded-revisions` | **Done by you, 2026-09-30 — nothing to decide.** Applying the probes had left three active revisions; two remain: `bike-trip-journal--probes-20260929140739-718d` serving 100% of traffic, and the pre-probe `bike-trip-journal--rel-1bbe81bcbec5` kept as the rollback target. | Nothing, on this app. After a future template change, repeat §8's deactivation step. |

Every other item this table used to list is done: `t-api-healthcheck-wiring` (`f0a1d99`, local half),
`t-access-log-slug-exposure`, `t-dockerignore-route-tree` (`93a30bf`), `t-settings-error-hides-input`,
`t-onedrive-per-photo-isolation` (`21d34ec`) and `t-onedrive-main-untested` (`709da23`).
`t-onedrive-preflight-check` is not debt: it is your step 5.
