# Azure deployment (DevOps agent scope)

Deploys the single Docker image (built from the repo-root `Dockerfile`) to
Azure Container Apps, free tier. Filled in by the `devops` agent per the
`deploy` skill (`.claude/skills/deploy/SKILL.md`), Week 1 (initial environment)
and Week 4 (production cutover).

Contents (format: `az` CLI script blocks in this README — no Bicep/Terraform/YAML
files; the one unavoidable YAML fragment, for probes, is inline in section 4).
Sections are in the order the owner runs them:
1. [Budget alert + resource group](#1-budget-alert--resource-group) — spec
   Section 13, a low-threshold tripwire, not a hard limit; created first
2. [Container Apps environment](#2-container-apps-environment) — Consumption
   (free tier), with Log Analytics
3. [Container App](#3-container-app) — the app definition: GHCR image,
   HTTPS-only external ingress on 8000, multiple-revision mode, scale to zero
4. [Probes](#4-probes) — `/api/health` liveness + startup probe
   (`t-infra-container-apps-probe`)
5. [Custom domain](#5-custom-domain-optional) — optional, not needed
6. [Verification](#6-verification)
7. [OneDrive sync job](#onedrive-sync-job) — the scheduled archive job

Secrets wiring covers `DATABASE_URL` (Neon) and `S3_ENDPOINT_URL`,
`S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME` (Cloudflare R2) on
both the app and the Job. `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` and
`GRAPH_REFRESH_TOKEN` (Microsoft Graph) are on the **Job only**, for least
privilege: the web app never reads them (see section 3). This README holds
names and structure only. Real values are typed by the owner at create time, or
set later with `az containerapp secret set`, and are never committed.

**Everything here is a definition only.** Nothing is provisioned until the owner
runs it, and every block that creates a resource needs the owner's explicit
approval (CLAUDE.md "Off-limits"). Replace every `<placeholder>`. Use the same
`<resource-group>`, `<containerapps-environment>`, `<app-name>` and
`<owner>/<repo>` values everywhere, including `docs/deploy-cutover-runbook.md`
§6. `<owner>/<repo>` must be **lower case** (GHCR rejects upper case).

## 1. Budget alert + resource group

The budget alert comes **first**, before any other Azure resource exists (spec
Section 13). Create it at **subscription** scope so it covers everything in the
subscription, including resources created later and any created by mistake
outside `<resource-group>`. It is a tripwire, not a cap: Azure budgets notify
but never stop resources.

**Use the portal.** `az consumption budget create` is a preview command and, as
far as this draft can tell, takes no email/contact flags. A budget without
notifications warns nobody. Check `az consumption budget create --help` on your
CLI version; if it has no notification or contact-email options, use these
portal steps:

1. Azure portal → **Cost Management + Billing** → **Cost Management** →
   **Budgets** → scope: `<subscription>` → **+ Add**.
2. Reset period **Monthly**. Name `<budget-name>`. Amount `<low-amount>` in the
   billing currency. Pick a small number whose only purpose is to fire if
   anything costs real money; the free-tier grants should keep the real bill at
   or near zero.
3. Alert conditions:
   - **Actual**, `<first-threshold-percent>` (e.g. 50%)
   - **Actual**, 100%
   - **Forecasted**, 100%
4. Alert recipients: `<your-email>`. Save.

Cost data lags by several hours or more, so the alert fires after the spend,
not during it.

Then the resource group. This is free.

```sh
# Once per machine: CLI extension and resource providers (free).
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights

az group create \
  --name <resource-group> \
  --location <region>
```

## 2. Container Apps environment

Uses the Consumption plan, which is serverless and pay-per-use. The monthly free
grant covers this app's traffic. `--enable-workload-profiles false` makes it a
**Consumption-only** environment, so no dedicated workload profile (billed
while it exists) can be added by accident.

```sh
az containerapp env create \
  --name <containerapps-environment> \
  --resource-group <resource-group> \
  --location <region> \
  --enable-workload-profiles false \
  --logs-destination log-analytics
```

**Log Analytics.** With no `--logs-workspace-id` given, the command creates a
Log Analytics workspace in `<resource-group>`. The app's and the sync job's
stdout/stderr go there. That is where the runbook §5 "Check the Job's logs" step
reads from, so keep it: `--logs-destination none` would leave no logs.
- **Trip slugs are already redacted** from request-path log lines by the
  image's `--log-config` (`backend/log_config/uvicorn.json`, see the repo-root
  `Dockerfile` and `t-access-log-slug-exposure`). Don't override the container
  command in section 3. That would drop `--log-config` and ship raw slugs, the
  link credential, to Log Analytics.
- **Ingestion is billed per GB beyond the free monthly allowance.** This app's
  volume, including one probe line every 30 s while a replica is up, is far
  below the allowance. It is still spend, so the budget alert covers it.

## 3. Container App

First read the secret values into shell variables without echoing them. Paste
each one at its prompt and press Enter. They stay out of shell history because
only the variable names appear in the commands below.

```sh
read -rsp 'database-url: ' database_url; echo
read -rsp 's3-endpoint-url: ' s3_endpoint_url; echo
read -rsp 's3-access-key-id: ' s3_access_key_id; echo
read -rsp 's3-secret-access-key: ' s3_secret_access_key; echo
read -rsp 's3-bucket-name: ' s3_bucket_name; echo
read -rsp 'ghcr-read-packages-token: ' ghcr_token; echo   # private package only
```

```sh
az containerapp create \
  --name <app-name> \
  --resource-group <resource-group> \
  --environment <containerapps-environment> \
  --image ghcr.io/<owner>/<repo>:<tag> \
  --registry-server ghcr.io \
  --registry-username <github-user> \
  --registry-password "$ghcr_token" \
  --ingress external \
  --target-port 8000 \
  --transport auto \
  --revisions-mode multiple \
  --min-replicas 0 \
  --max-replicas 1 \
  --cpu 0.5 --memory 1.0Gi \
  --secrets \
    database-url="$database_url" \
    s3-endpoint-url="$s3_endpoint_url" \
    s3-access-key-id="$s3_access_key_id" \
    s3-secret-access-key="$s3_secret_access_key" \
    s3-bucket-name="$s3_bucket_name" \
  --env-vars \
    DATABASE_URL=secretref:database-url \
    S3_ENDPOINT_URL=secretref:s3-endpoint-url \
    S3_ACCESS_KEY_ID=secretref:s3-access-key-id \
    S3_SECRET_ACCESS_KEY=secretref:s3-secret-access-key \
    S3_BUCKET_NAME=secretref:s3-bucket-name \
    S3_REGION=auto \
    ENVIRONMENT=<non-local-environment-name>
```

If you're creating the Job (below) in the same shell session, keep the
variables until it's created too, because it takes the same five values. Then
clear them:

```sh
unset database_url s3_endpoint_url s3_access_key_id s3_secret_access_key s3_bucket_name ghcr_token
```

**Secrets must exist at create time.** Each `secretref:` has to name a secret
that exists when the app is created, so `--secrets` can't be left out and
filled in later. Later changes go through
`az containerapp secret set --name <app-name> --resource-group <resource-group> --secrets <secret-name>="$var"`
(use the same `read -rsp` pattern). Running replicas don't pick up a changed
secret, so restart the active revision or deploy a new one afterwards.

How it works:
- **Image and registry.** The same GHCR image as the Job, tagged with the
  commit hash (runbook §6; never `latest`). The `--registry-*` flags are needed
  only if the GHCR package is **private**. Use a GitHub token with
  `read:packages` only, which is not the `write:packages` token used to push.
  The CLI stores it as a Container App secret. A public package needs no
  credential, so drop the three `--registry-*` flags.
- **Ingress.** `external` with target port **8000**, the port uvicorn listens
  on in the image `CMD`. `--transport auto` lets ingress pick HTTP/1.1 or
  HTTP/2. **HTTPS only:** `allowInsecure` defaults to `false`, so plain
  `http://` is redirected to `https://` and never serves the app. HTTPS is
  required because the service worker, `navigator.geolocation` and
  `crypto.randomUUID()` need a secure context (runbook §7). Section 6 checks it.
- **Multiple-revision mode.** Each deploy creates a new revision next to the
  old one, and traffic moves by explicit weight. The runbook §6 deploy and §8
  rollback both depend on this.
- **Scale.** `--min-replicas 0` means scale to zero. With no traffic the app
  costs nothing, and the first request after idle waits for a cold start. That
  is why the SPA shows "Waking up the server…". `--max-replicas 1` because:
  - A handful of riders and viewers is well within one replica.
  - Each replica holds its own SQLAlchemy connection pool against Neon.
  - Every extra replica burns the free vCPU-second grant in parallel.
  - The default HTTP scale rule (about 10 concurrent requests) could add a
    second replica for a short burst, such as a rider's queue flushing several
    photos, with no user-visible gain.
  - The cost of one replica is a short gap if it crashes, until the platform
    restarts it. The offline queue already absorbs that: failed requests retry
    under the same client id. During a deploy the old and new revisions run
    side by side regardless of this setting, because it is per revision.
- **CPU and memory.** 0.5 vCPU / 1.0 GiB is the CLI default and a safe size
  for FastAPI plus in-memory photo uploads. A smaller size such as `0.25` /
  `0.5Gi` stretches the free grant further, but makes cold starts slower.
- **Env/secret split** matches `docs/deploy-cutover-runbook.md` §2 and the Job
  below. Everything is a secret except `S3_REGION` and `ENVIRONMENT`, which are
  plain env vars. Secret names are the env var name in lower kebab case, the
  same names the Job uses. The Job has its own secret store, so the five shared
  secrets are set on both.
- **No `GRAPH_*` on the app (least privilege).** `GRAPH_CLIENT_ID`,
  `GRAPH_CLIENT_SECRET`, `GRAPH_REFRESH_TOKEN` and `GRAPH_ONEDRIVE_FOLDER` go
  on the Job only. The web app process never reads them: only
  `app/storage/onedrive_sync.py` (the Job) and `app/storage/get_refresh_token.py`
  (the owner's one-time laptop helper) do, and no route imports either module.
  Their settings default to empty, so the app starts without them. Leaving
  them off means a compromised app replica doesn't also expose the OneDrive
  credential, and a token re-mint only touches the Job. `ENVIRONMENT` is a
  harmless plain var and is set on both.
- **`S3_PUBLIC_ENDPOINT_URL` stays unset.** It only matters when the API and
  the browser reach storage at different addresses, as with local MinIO
  (`minio:9000` inside compose vs `localhost:9000` in the browser). R2's
  endpoint is the same for both, and unset means presigned photo URLs are
  signed for `S3_ENDPOINT_URL`, which is correct here. Setting it to anything
  else would break every photo link.
- **`STATIC_FILES_DIR`** is baked into the image (`/app/frontend/dist`) and is
  not set here.
- **No `--command`/`--args`.** The image `CMD` is what applies `--log-config`
  slug redaction (section 2).

## 4. Probes

Azure Container Apps **ignores the Dockerfile `HEALTHCHECK`**. Without a probe
declared here, nothing in production checks `/api/health`
(`t-infra-container-apps-probe`).

**This is the one place YAML can't be avoided.** `az containerapp create` and
`update` have no probe flags, so probes can only be set through
`az containerapp update --yaml`. The YAML lives inline here, not as a file in
the repo.

The probes:
- **Startup.** `httpGet /api/health` on port 8000. Liveness is held off until
  it succeeds. It allows about 105 s (5 s delay + 10 failures × 10 s) for a
  cold start. 10 is the maximum `failureThreshold` Container Apps accepts.
- **Liveness.** `httpGet /api/health` on port 8000: `initialDelaySeconds` 10,
  `periodSeconds` 30, `timeoutSeconds` 5, `failureThreshold` 3. Three misses in
  a row (about 90 s) restart the container.
- **No readiness probe.** `/api/health` checks neither Postgres nor storage. It
  only says the process is accepting requests. A readiness probe on it would
  claim "dependencies are up" when that was never tested.
- **`scheme: HTTP`.** The probe goes straight to the container on port 8000.
  TLS ends at ingress, so the probe does not use HTTPS.

**Steps.** Probes live inside the container entry of `properties.template`.
Array entries are replaced whole on update, so the YAML must carry the
complete container (name, image, env, resources), not just the probes. Start
from the live definition rather than retyping it:

```sh
az containerapp show --name <app-name> --resource-group <resource-group> -o yaml > app.yaml
```

Edit `app.yaml` outside the repo (it contains resource IDs; it has no secret
values, because `show` doesn't return them):
1. Keep only the top-level `properties:` → `template:` subtree and delete
   everything else. Leaving `configuration:` out keeps secrets, registries and
   ingress as they are.
2. Delete `template.revisionSuffix` if it's there. Reusing an existing suffix
   fails, and without it Azure generates a new one.
3. Add the `probes:` block below to the single entry under
   `template.containers`. Don't change anything else in that entry.

The result has this shape. The `env` list and `resources` are what `show`
printed, with all seven env entries left as they are:

```yaml
properties:
  template:
    containers:
      - name: <app-name>
        image: ghcr.io/<owner>/<repo>:<tag>
        env:
          - name: DATABASE_URL
            secretRef: database-url
          # ... the other six entries exactly as `show` printed them
        resources:
          cpu: 0.5
          memory: 1Gi
        probes:
          - type: Startup
            httpGet:
              path: /api/health
              port: 8000
              scheme: HTTP
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 10
          - type: Liveness
            httpGet:
              path: /api/health
              port: 8000
              scheme: HTTP
            initialDelaySeconds: 10
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 3
    scale:
      minReplicas: 0
      maxReplicas: 1
```

Then apply it and delete the local copy:

```sh
az containerapp update --name <app-name> --resource-group <resource-group> --yaml app.yaml
rm app.yaml
```

A template change creates a **new revision**. On first setup no one is using
the app yet, so let it take traffic. On a live app, follow runbook §6: pin
traffic first, then move it once the new revision is `Healthy`. Probes carry
forward to later revisions made by `az containerapp update --image`, because
those copy the current template.

## 5. Custom domain (optional)

Not needed. The default `https://<app-name>.<unique-id>.<region>.azurecontainerapps.io`
already serves HTTPS with a managed certificate, and the rider and viewer links
work on it. Add a custom domain (`az containerapp hostname add` / `bind`) only
if you want a nicer URL. Links already shared keep pointing at the old host, so
decide before the links go out.

## 6. Verification

```sh
# FQDN (the host part of the public URL)
az containerapp show --name <app-name> --resource-group <resource-group> \
  --query properties.configuration.ingress.fqdn -o tsv

# HTTPS-only check: expect "false"
az containerapp ingress show --name <app-name> --resource-group <resource-group> \
  --query allowInsecure -o tsv

# Revisions, their image, health and traffic
az containerapp revision list --name <app-name> --resource-group <resource-group> \
  --query "[].{name:name, image:properties.template.containers[0].image, active:properties.active, health:properties.healthState, traffic:properties.trafficWeight}" -o table

# Probes are on the running template (expect Startup and Liveness)
az containerapp show --name <app-name> --resource-group <resource-group> \
  --query "properties.template.containers[0].probes[].type" -o tsv

# Liveness endpoint: expect HTTP 200 and {"status":"ok"}.
# The first call after idle can take a while (cold start from zero).
curl -i https://<fqdn>/api/health

# Plain HTTP must not serve the app: expect a redirect to https://
curl -sI http://<fqdn>/ | head -n 1
```

Then continue with runbook §7 (SPA loads, rider and viewer links, offline cold
open).

## OneDrive sync job

A scheduled Container Apps Job runs one OneDrive archive pass
(`python -m app.storage.onedrive_sync`) every 30 minutes, from the **same GHCR
image** as the app. It is one-way background work: the app never waits on it.
A pass with `GRAPH_REFRESH_TOKEN` empty logs "not configured" and exits 0.

Definition only — nothing is provisioned until the owner runs it. Replace every
`<placeholder>`; secret values are never written here.

Secrets are passed at creation, because each `secretref:` env var must name a
secret that already exists or the create fails. Values are read without
echoing, as for the app (section 3), so they stay out of shell history. If the
app's five shared variables and `ghcr_token` are still set from section 3,
reuse them and read only the Graph values:

```sh
# Skip the first five and ghcr_token if they are still set from section 3.
read -rsp 'database-url: ' database_url; echo
read -rsp 's3-endpoint-url: ' s3_endpoint_url; echo
read -rsp 's3-access-key-id: ' s3_access_key_id; echo
read -rsp 's3-secret-access-key: ' s3_secret_access_key; echo
read -rsp 's3-bucket-name: ' s3_bucket_name; echo
read -rsp 'ghcr-read-packages-token: ' ghcr_token; echo   # private package only
# Job only:
read -rsp 'graph-client-id: ' graph_client_id; echo
read -rsp 'graph-client-secret: ' graph_client_secret; echo
read -rsp 'graph-refresh-token: ' graph_refresh_token; echo
```

```sh
az containerapp job create \
  --name <job-name> \
  --resource-group <resource-group> \
  --environment <containerapps-environment> \
  --image ghcr.io/<owner>/<repo>:<tag> \
  --registry-server ghcr.io \
  --registry-username <github-user> \
  --registry-password "$ghcr_token" \
  --trigger-type Schedule \
  --cron-expression "*/30 * * * *" \
  --parallelism 1 \
  --replica-completion-count 1 \
  --replica-retry-limit 0 \
  --replica-timeout 900 \
  --cpu 0.25 --memory 0.5Gi \
  --command "sh" "-c" "cd /app/backend && exec .venv/bin/python -m app.storage.onedrive_sync" \
  --secrets \
    database-url="$database_url" \
    s3-endpoint-url="$s3_endpoint_url" \
    s3-access-key-id="$s3_access_key_id" \
    s3-secret-access-key="$s3_secret_access_key" \
    s3-bucket-name="$s3_bucket_name" \
    graph-client-id="$graph_client_id" \
    graph-client-secret="$graph_client_secret" \
    graph-refresh-token="$graph_refresh_token" \
  --env-vars \
    DATABASE_URL=secretref:database-url \
    S3_ENDPOINT_URL=secretref:s3-endpoint-url \
    S3_ACCESS_KEY_ID=secretref:s3-access-key-id \
    S3_SECRET_ACCESS_KEY=secretref:s3-secret-access-key \
    S3_BUCKET_NAME=secretref:s3-bucket-name \
    S3_REGION=auto \
    GRAPH_CLIENT_ID=secretref:graph-client-id \
    GRAPH_CLIENT_SECRET=secretref:graph-client-secret \
    GRAPH_REFRESH_TOKEN=secretref:graph-refresh-token \
    GRAPH_ONEDRIVE_FOLDER=<onedrive-folder-path> \
    ENVIRONMENT=<non-local-environment-name>
```

Then clear the variables:

```sh
unset database_url s3_endpoint_url s3_access_key_id s3_secret_access_key s3_bucket_name \
  ghcr_token graph_client_id graph_client_secret graph_refresh_token
```

Later changes go through `az containerapp job secret set` with the same
`read -rsp` pattern. The next scheduled execution picks up the new value.

How it works:
- **Schedule.** Cron `*/30 * * * *` is evaluated in **UTC**.
- **Command.** The image `WORKDIR` is `/app` and the Python package and venv
  live in `/app/backend` (see the repo-root `Dockerfile`), hence the `cd`.
  `STATIC_FILES_DIR` is baked into the image and is not set here.
- **Env/secret split** follows `docs/deploy-cutover-runbook.md` §2: everything
  is a secret except `S3_REGION`, `GRAPH_ONEDRIVE_FOLDER` and `ENVIRONMENT`.
  Secret names are the env var name in lower kebab case, the same names the app
  uses for the five secrets they share.
- **`GRAPH_*` live here only.** The Job is the only deployed process that reads
  them, so the app doesn't carry them (least privilege, section 3).
- **No overlapping runs.** `--parallelism 1` only limits replicas *within one
  execution*; it does not stop the next scheduled execution from starting
  while the previous one is still running. What prevents overlap is the time
  budget: `replica-timeout × (replica-retry-limit + 1)` must stay **below
  1800 s** (the 30-minute interval). Here that is 900 × (0 + 1) = 900 s. Retry
  limit is 0 on purpose: a failed pass is retried by the next scheduled run,
  and the sync is idempotent (photos stay pending until archived). If you raise
  either value, re-check the invariant.

The job is a separate resource from the app:
- **Own secret store.** The job does not see the app's secrets. The five
  shared secrets (`database-url`, `s3-*`) are set on both with the same values,
  and rotated on both together. The three `graph-*` secrets exist on the Job
  only, so renewing `GRAPH_REFRESH_TOKEN` touches only the Job:
  `read -rsp 'graph-refresh-token: ' graph_refresh_token; echo`, then
  `az containerapp job secret set --name <job-name> --resource-group <resource-group> --secrets graph-refresh-token="$graph_refresh_token"`,
  then `unset graph_refresh_token`.
- **Registry credential.** If the GHCR package is private, the job needs the
  same GHCR credential as the app (`--registry-*` above, a GitHub token with
  `read:packages`). A public package needs none — drop the three
  `--registry-*` flags.
- **Every deploy updates both.** After updating the app to a new tag, also run
  `az containerapp job update --name <job-name> --resource-group <resource-group> --image ghcr.io/<owner>/<repo>:<tag>`
  with the **same tag**, so the sync code never drifts from the app's schema.

Nothing here is a hard Azure dependency for the *app* — see spec Section 4
"Portability principle." This directory is the one place that's allowed to be
Azure-specific, since it's the deploy target, not the app.
