# Azure deployment (DevOps agent scope)

Deploys the single Docker image (built from the repo-root `Dockerfile`) to
Azure Container Apps, free tier. Filled in by the `devops` agent per the
`deploy` skill (`.claude/skills/deploy/SKILL.md`), Week 1 (initial environment)
and Week 4 (production cutover).

Planned contents (format: `az` CLI script blocks in this README — no
Bicep/Terraform/YAML):
- `az containerapp` CLI script — Container Apps environment + app definition,
  external HTTPS ingress on port 8000, multiple-revision mode, and scale-to-zero
- `az containerapp job` CLI script — the scheduled OneDrive sync job (below)
- Secrets wiring for `DATABASE_URL` (Neon), `S3_*` (Cloudflare R2),
  `GRAPH_*` (on the sync Job only) — names/structure only, real values are entered privately and
  passed to Azure CLI without putting literal values in saved commands
- Budget alert setup notes (spec Section 13 — a low-threshold tripwire, not a
  hard limit; the free-tier ceilings are far beyond this trip's usage)

## Container Apps environment and app

Definition only — nothing is provisioned until the owner runs these commands.
Complete the Azure budget-alert step in `docs/deploy-cutover-runbook.md` §1
before creating the environment or any other Azure resource.
Replace every `<placeholder>`. The image path and tag must be the GHCR image
being deployed; `<owner>/<repo>` must be lower case. For a private GHCR package,
use a GitHub token with `read:packages`. A public package needs no registry
credential; omit the three `--registry-*` options.

The following prompts do not echo secret values or put their literal values in
the typed command history. Azure CLI receives the values as arguments, however,
so run this only in a trusted local session: local process inspection may expose
an argument while a command is running. Do not paste values into chat, a script,
or a saved command. The app and Job have separate secret stores; repeat the
private prompts in the Job section when creating or rotating its secrets.

```sh
az containerapp env create \
  --name <containerapps-environment> \
  --resource-group <resource-group> \
  --location <azure-region>

read -rp 'GitHub username: ' GHCR_USERNAME
read -rsp 'GHCR read:packages token: ' GHCR_READ_PACKAGES_TOKEN; printf '\n'
read -rsp 'DATABASE_URL: ' DATABASE_URL; printf '\n'
read -rsp 'S3_ENDPOINT_URL: ' S3_ENDPOINT_URL; printf '\n'
read -rsp 'S3_ACCESS_KEY_ID: ' S3_ACCESS_KEY_ID; printf '\n'
read -rsp 'S3_SECRET_ACCESS_KEY: ' S3_SECRET_ACCESS_KEY; printf '\n'
read -rsp 'S3_BUCKET_NAME: ' S3_BUCKET_NAME; printf '\n'

# For a public GHCR package, omit the GHCR prompt above and all three --registry-* options below.
az containerapp create \
  --name <app-name> \
  --resource-group <resource-group> \
  --environment <containerapps-environment> \
  --container-name <app-container-name> \
  --image ghcr.io/<owner>/<repo>:<tag> \
  --registry-server ghcr.io \
  --registry-username "$GHCR_USERNAME" \
  --registry-password "$GHCR_READ_PACKAGES_TOKEN" \
  --ingress external \
  --target-port 8000 \
  --allow-insecure false \
  --revisions-mode multiple \
  --min-replicas 0 \
  --max-replicas 1 \
  --secrets \
    database-url="$DATABASE_URL" \
    s3-endpoint-url="$S3_ENDPOINT_URL" \
    s3-access-key-id="$S3_ACCESS_KEY_ID" \
    s3-secret-access-key="$S3_SECRET_ACCESS_KEY" \
    s3-bucket-name="$S3_BUCKET_NAME" \
  --env-vars \
    DATABASE_URL=secretref:database-url \
    S3_ENDPOINT_URL=secretref:s3-endpoint-url \
    S3_ACCESS_KEY_ID=secretref:s3-access-key-id \
    S3_SECRET_ACCESS_KEY=secretref:s3-secret-access-key \
    S3_BUCKET_NAME=secretref:s3-bucket-name \
    S3_REGION=auto \
    ENVIRONMENT=<non-local-environment-name>

unset GHCR_USERNAME GHCR_READ_PACKAGES_TOKEN DATABASE_URL S3_ENDPOINT_URL \
  S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_BUCKET_NAME
```

**No `GRAPH_*` on the app** (least privilege, decision-log Entry 27): the web
app never reads them; only the OneDrive sync Job (and the owner's laptop
helper) does, so the long-lived OneDrive token stays off the internet-facing
app. **`--max-replicas 1`**: a handful of users fits one replica, and each
extra replica opens its own Neon connection pool; the offline queue covers a
short gap if the replica restarts.

`S3_PUBLIC_ENDPOINT_URL` is intentionally unset; the runbook says the R2
endpoint is used for both API and browser access. `STATIC_FILES_DIR` is already
baked into the image. The secret names and plain environment variables match
`.env.example` and `docs/deploy-cutover-runbook.md` §2.

The CLI does not expose custom Container Apps probes directly. After creating
the app, apply the startup and liveness probes with the Container Apps Update REST API
(`2026-07-01`) using JSON Merge Patch. It reads the existing app once and uses
Python's standard library to preserve the complete containers array (including
any sidecars and their settings), changing only `probes` on the named app
container. It fails instead of silently patching if that name is absent or
ambiguous. The probes are deliberately startup + liveness, not readiness: `/api/health`
does not check Postgres or object storage.

```sh
set -e
APP_CONTAINER_NAME=<app-container-name>
APP_JSON=$(az containerapp show \
  --name <app-name> \
  --resource-group <resource-group> \
  --output json)
PATCH_DATA=$(python -c '
import json
import sys

app = json.load(sys.stdin)
container_name = sys.argv[1]
containers = app["properties"]["template"]["containers"]
matches = [container for container in containers if container.get("name") == container_name]
if len(matches) != 1:
    raise SystemExit("expected exactly one container with the requested name")

for container in matches:
    probes = container.get("probes") or []
    container["probes"] = [
        probe for probe in probes if probe.get("type") not in ("Liveness", "Startup")
    ]
    # Startup: gives a scale-from-zero cold start ~105 s before liveness applies.
    container["probes"].append({
        "type": "Startup",
        "httpGet": {"path": "/api/health", "port": 8000, "scheme": "HTTP"},
        "initialDelaySeconds": 5,
        "periodSeconds": 10,
        "timeoutSeconds": 5,
        "failureThreshold": 10,
    })
    container["probes"].append({
        "type": "Liveness",
        "httpGet": {"path": "/api/health", "port": 8000, "scheme": "HTTP"},
        "initialDelaySeconds": 10,
        "periodSeconds": 30,
        "timeoutSeconds": 5,
        "failureThreshold": 3,
    })

patch = {
    "location": app["location"],
    "properties": {"template": {"containers": containers}},
}
print(app["id"])
print(json.dumps(patch, separators=(",", ":")))
' "$APP_CONTAINER_NAME" <<<"$APP_JSON")
APP_RESOURCE_ID=${PATCH_DATA%%$'\n'*}
PATCH_BODY=${PATCH_DATA#*$'\n'}
az rest --method patch \
  --uri "https://management.azure.com${APP_RESOURCE_ID}?api-version=2026-07-01" \
  --headers "Content-Type=application/merge-patch+json" \
  --body "$PATCH_BODY"
```

Changing the template creates a new revision. In multiple-revision mode, check
which revision has traffic rather than assuming this patch moved it. For later
deploys, follow `docs/deploy-cutover-runbook.md` §6: pin the current revision,
check the new revision's health, and move traffic explicitly only after it is
healthy. The first app revision receives initial traffic; the probe patch is a
separate template revision, so verify traffic and health after applying it.

## OneDrive sync job

A scheduled Container Apps Job runs one OneDrive archive pass
(`python -m app.storage.onedrive_sync`) every 30 minutes, from the **same GHCR
image** as the app. It is one-way background work: the app never waits on it.
A pass with `GRAPH_REFRESH_TOKEN` empty logs "not configured" and exits 0.

Definition only — nothing is provisioned until the owner runs it. Replace every
`<placeholder>`. The Job uses the same GHCR image and tag as the app, but it has
its own secret store. Repeat the private prompts here, using the same values as
the app; do not type literal secrets into a command or save them in shell
history. As above, command arguments may be visible to local process inspection
while Azure CLI is running.

```sh
read -rp 'GitHub username: ' GHCR_USERNAME
read -rsp 'GHCR read:packages token: ' GHCR_READ_PACKAGES_TOKEN; printf '\n'
read -rsp 'DATABASE_URL: ' DATABASE_URL; printf '\n'
read -rsp 'S3_ENDPOINT_URL: ' S3_ENDPOINT_URL; printf '\n'
read -rsp 'S3_ACCESS_KEY_ID: ' S3_ACCESS_KEY_ID; printf '\n'
read -rsp 'S3_SECRET_ACCESS_KEY: ' S3_SECRET_ACCESS_KEY; printf '\n'
read -rsp 'S3_BUCKET_NAME: ' S3_BUCKET_NAME; printf '\n'
read -rsp 'GRAPH_CLIENT_ID: ' GRAPH_CLIENT_ID; printf '\n'
read -rsp 'GRAPH_CLIENT_SECRET: ' GRAPH_CLIENT_SECRET; printf '\n'
read -rsp 'GRAPH_REFRESH_TOKEN: ' GRAPH_REFRESH_TOKEN; printf '\n'

# For a public GHCR package, omit the GHCR prompt above and all three --registry-* options below.
az containerapp job create \
  --name <job-name> \
  --resource-group <resource-group> \
  --environment <containerapps-environment> \
  --image ghcr.io/<owner>/<repo>:<tag> \
  --registry-server ghcr.io \
  --registry-username "$GHCR_USERNAME" \
  --registry-password "$GHCR_READ_PACKAGES_TOKEN" \
  --trigger-type Schedule \
  --cron-expression "*/30 * * * *" \
  --parallelism 1 \
  --replica-completion-count 1 \
  --replica-retry-limit 0 \
  --replica-timeout 900 \
  --cpu 0.25 --memory 0.5Gi \
  --command "sh" "-c" "cd /app/backend && exec .venv/bin/python -m app.storage.onedrive_sync" \
  --secrets \
    database-url="$DATABASE_URL" \
    s3-endpoint-url="$S3_ENDPOINT_URL" \
    s3-access-key-id="$S3_ACCESS_KEY_ID" \
    s3-secret-access-key="$S3_SECRET_ACCESS_KEY" \
    s3-bucket-name="$S3_BUCKET_NAME" \
    graph-client-id="$GRAPH_CLIENT_ID" \
    graph-client-secret="$GRAPH_CLIENT_SECRET" \
    graph-refresh-token="$GRAPH_REFRESH_TOKEN" \
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

unset GHCR_USERNAME GHCR_READ_PACKAGES_TOKEN DATABASE_URL S3_ENDPOINT_URL \
  S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_BUCKET_NAME GRAPH_CLIENT_ID \
  GRAPH_CLIENT_SECRET GRAPH_REFRESH_TOKEN
```

For a later secret rotation, prompt for the new value first and pass the
variable, not a literal, to the CLI. The Graph secrets live on the Job only, so
a token re-mint is this one command:

```sh
read -rsp 'New GRAPH_REFRESH_TOKEN: ' GRAPH_REFRESH_TOKEN; printf '\n'
az containerapp job secret set \
  --name <job-name> \
  --resource-group <resource-group> \
  --secrets "graph-refresh-token=$GRAPH_REFRESH_TOKEN"
unset GRAPH_REFRESH_TOKEN
```

For a database or R2 secret, which both hold, repeat with
`az containerapp secret set` for the app's separate store. The
prompt avoids echo and saved literal command history; it does not hide an
argument from local process inspection while the CLI is running.

How it works:
- **Schedule.** Cron `*/30 * * * *` is evaluated in **UTC**.
- **Command.** The image `WORKDIR` is `/app` and the Python package and venv
  live in `/app/backend` (see the repo-root `Dockerfile`), hence the `cd`.
  `STATIC_FILES_DIR` is baked into the image and is not set here.
- **Env/secret split** follows `docs/deploy-cutover-runbook.md` §2: everything
  is a secret except `S3_REGION`, `GRAPH_ONEDRIVE_FOLDER` and `ENVIRONMENT`.
  Secret names are the env var name in lower kebab case; the app must use the
  same secret names.
- **No overlapping runs.** `--parallelism 1` only limits replicas *within one
  execution*; it does not stop the next scheduled execution from starting
  while the previous one is still running. What prevents overlap is the time
  budget: `replica-timeout × (replica-retry-limit + 1)` must stay **below
  1800 s** (the 30-minute interval). Here that is 900 × (0 + 1) = 900 s. Retry
  limit is 0 on purpose: a failed pass is retried by the next scheduled run,
  and the sync is idempotent (photos stay pending until archived). If you raise
  either value, re-check the invariant.

The job is a separate resource from the app:
- **Own secret store.** The job does not see the app's secrets. Set the same
  database and R2 secret names with the same values on the job, and rotate
  those in both stores together. The `graph-*` secrets exist on the job only
  (Entry 27), so a Graph token re-mint touches only the job.
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
