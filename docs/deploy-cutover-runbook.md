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

devops runs these steps after you have confirmed the cutover.

Replace every `<placeholder>`. `<owner>/<repo>` is the GitHub path and must be **lower case**
(GHCR rejects upper case). It is the same image path as `infra/azure/README.md`.

**Images are built by CI, not by hand.** `.github/workflows/ci.yml` publishes
`ghcr.io/<owner>/<repo>:<full-commit-sha>` on every merge to `main`, after the backend and frontend
checks pass. It never deploys: every step below stays manual. See `infra/azure/README.md`,
"Image publishing (GitHub Actions to GHCR)", for package visibility and access.

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
