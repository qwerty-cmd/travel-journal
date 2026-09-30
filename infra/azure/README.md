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
(`2026-07-01`). ARM PATCH already merges, so `application/json` is the right
media type; `application/merge-patch+json` was tried and came back as
`UnsupportedMediaType`. The script reads the existing app once and uses
Python's standard library to preserve the complete containers array (including
any sidecars and their settings), changing only `probes` on the named app
container and dropping the empty `value` ARM reads back next to a `secretRef`
(an entry carrying both is not a valid write shape). It fails instead of
silently patching if that name is absent or ambiguous. The probes are
deliberately startup + liveness, not readiness: `/api/health` does not check
Postgres or object storage.

**The patch must carry a fresh `revisionSuffix`.** A revision is named
`<app-name>--<revisionSuffix>`, and `az containerapp create` stores the
`--revision-suffix` it was given in `properties.template.revisionSuffix`. A
PATCH merges, so *omitting* the field leaves that stored value in place: the RP
then tries to create a revision whose name is already taken and rejects the
whole update with `Field 'template.revisionsuffix' is invalid ... revision with
suffix ... already exists`. The PATCH is still accepted (`202`) and then applies
nothing — silently, as far as the CLI is concerned. This is documented
behaviour (microsoft/azure-container-apps#1278; Learn: "This value must be
unique as the runtime rejects any conflicts with existing revision name suffix
values"). So the script generates a suffix per run instead of reusing or
decorating the stored one, which would work once and collide on every re-run.

A `properties.template` change is a **long-running operation**
(`final-state-via: location`): ARM provisions a new revision and answers `202
Accepted` with **no body**, only `Location`/`Retry-After` headers. So the
response is not the success signal — an empty response is the normal case, and
`az` prints nothing at all. What is reliable is the exit code: `az rest` does
exit non-zero when ARM returns an error (an HTML `400 Bad Request` page from a
malformed URI included). Every `az` call whose failure matters is therefore
checked explicitly with `if ! ...`, not left to `set -e`, whose behaviour
differs between a script file and a pasted interactive session.

The proof that the patch took effect is the re-read, and the re-read is a raw
ARM GET via `az rest` rather than `az containerapp show`. That is the
non-obvious part: ARM reports a rejected template update in
`properties.deploymentErrors`, a field that is **not** in the `2026-07-01`
swagger's `ContainerAppProperties` and that **`az containerapp show` drops**
(verified: `'deploymentErrors' in properties` is `False` through the CLI, `True`
through a raw GET of the same resource). Polling the CLI's view therefore polls
something that structurally cannot contain the error, and a patch ARM refused
looks exactly like a patch that is merely slow.

Because the new revision takes seconds to provision, the probe assertion is
still polled — up to 18 attempts, 10 s apart — and the first pass wins; failing
attempts print what is still missing. What is new is the early exit: a reading
of `provisioningState: Failed` together with a non-empty `deploymentErrors`
stops the loop immediately and prints the error, turning three minutes of
silence into a 30-second diagnosis. Both fields still describe the *previous*
operation at the moment a fresh PATCH is accepted, so the script captures them
with the same raw GET **before** patching and only treats a reading that
*differs* from that baseline as this run's failure; an identical repeat error is
left to time out rather than risk blaming this run for the last one's. Success
is still the probe assertion alone — it deliberately does not wait on
`provisioningState` reaching a good value (a stale earlier failure would hang a
patch that worked) or on `latestReadyRevisionName` (observed to lag).

One more detail makes this
work under Git Bash on Windows as well as bash on Linux/macOS: Python's stdout
is forced to LF — text-mode stdout would otherwise emit CRLF and leave a stray
carriage return inside the resource id and the JSON body, which ARM answers
with an IIS `Invalid URL` page.

The block exits non-zero on failure, which ends a shell you paste it into; run
it as a script file, or inside `bash <<'EOF' ... EOF`, to keep your session.

```bash
set -e
APP_CONTAINER_NAME=<app-container-name>
if ! APP_JSON=$(az containerapp show \
  --name <app-name> \
  --resource-group <resource-group> \
  --output json); then
  printf 'reading the app failed; nothing was patched\n' >&2
  exit 1
fi
if ! PATCH_DATA=$(python -c '
import json
import secrets
import sys
import time

# Windows text-mode stdout emits CRLF, which would leave a carriage return in
# the resource id and the JSON body split out below. json.dumps escapes any
# real CR, so forcing LF is safe on every platform.
sys.stdout.reconfigure(newline="\n")

app = json.load(sys.stdin)
container_name = sys.argv[1]
containers = app["properties"]["template"]["containers"]
matches = [container for container in containers if container.get("name") == container_name]
if len(matches) != 1:
    raise SystemExit(
        "expected exactly one container named %r, found %d" % (container_name, len(matches))
    )

# ARM reads secret-backed env entries back carrying both "secretRef" and an
# empty "value"; writing both is invalid. Plain value-only entries stay as-is.
for container in containers:
    for env_var in container.get("env") or []:
        if env_var.get("secretRef"):
            env_var.pop("value", None)

for container in matches:
    probes = container.get("probes") or []
    # Case-insensitive, matching the verification below: ARM has been seen to
    # read probe types back in a different case than they were written, and a
    # case-sensitive filter would leave the stale pair in place and duplicate.
    container["probes"] = [
        probe
        for probe in probes
        if str(probe.get("type", "")).lower() not in ("liveness", "startup")
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

# A PATCH merges, so leaving revisionSuffix out keeps the value stored by
# "az containerapp create" and the RP rejects the whole update because the
# revision name "<app-name>--<that suffix>" already exists. This is built fresh
# and independently of the stored suffix, so a re-run never collides with its
# own previous run either.
#
# Value rules: the revision is named "<app-name>--<revisionSuffix>" and that
# whole name is capped at 63 characters; the suffix is lowercase alphanumerics
# and dashes, starting and ending alphanumeric, with no consecutive dashes.
# "probes-" + 14-digit UTC timestamp + "-" + 4 hex digits is 26 characters, and
# Azure caps a Container App name at 32, so the longest possible name here is
# 32 + 2 + 26 = 60 - inside the cap for any <app-name> the create step accepted.
# The random tail is what guarantees freshness: the timestamp alone repeats if
# the script is run twice within the same second.
revision_suffix = "probes-%s-%s" % (
    time.strftime("%Y%m%d%H%M%S", time.gmtime()),
    secrets.token_hex(2),
)

patch = {
    "location": app["location"],
    "properties": {"template": {
        "revisionSuffix": revision_suffix,
        "containers": containers,
    }},
}
print(app["id"])
print(json.dumps(patch, separators=(",", ":")))
' "$APP_CONTAINER_NAME" <<<"$APP_JSON"); then
  printf 'building the patch body failed; nothing was patched\n' >&2
  exit 1
fi
APP_RESOURCE_ID=${PATCH_DATA%%$'\n'*}
PATCH_BODY=${PATCH_DATA#*$'\n'}

# properties.deploymentErrors is where ARM reports a rejected template update,
# it is absent from the 2026-07-01 swagger, and "az containerapp show" drops it
# - hence the raw GET here and in the poll below. Both it and provisioningState
# still describe the *previous* operation when a fresh PATCH is accepted, so
# this pre-patch snapshot is what the poll compares against before blaming a
# failure on this run.
if ! BASELINE=$(az rest --method get \
  --uri "https://management.azure.com${APP_RESOURCE_ID}?api-version=2026-07-01" \
  | python -c '
import json
import sys

sys.stdout.reconfigure(newline="\n")
properties = json.load(sys.stdin)["properties"]
print(json.dumps([properties.get("provisioningState"), properties.get("deploymentErrors")]))
'); then
  printf 'reading the pre-patch deployment state failed; nothing was patched\n' >&2
  exit 1
fi

# Long-running operation: 202 with an empty body is the normal success case, so
# the response proves nothing and only the exit code is checked here.
if ! az rest --method patch \
  --uri "https://management.azure.com${APP_RESOURCE_ID}?api-version=2026-07-01" \
  --headers "Content-Type=application/json" \
  --body "$PATCH_BODY"; then
  printf 'PATCH failed; see the az error above\n' >&2
  exit 1
fi

# The new revision takes seconds to provision, so poll the assertion itself:
# the probes appearing on the template is the success condition. Up to 3 minutes,
# cut short by a new provisioning failure (exit status 2 below).
PROBES_OK=
for _ in {1..18}; do
  PROBE_STATUS=0
  az rest --method get \
    --uri "https://management.azure.com${APP_RESOURCE_ID}?api-version=2026-07-01" \
    | python -c '
import json
import sys

app = json.load(sys.stdin)
container_name = sys.argv[1]
baseline = sys.argv[2]
properties = app["properties"]

# Fail fast on a provisioning failure that is not simply the pre-patch one read
# back. Same text and state as the baseline is treated as stale and polled
# through; an identical repeat failure just times out as before.
deployment_errors = properties.get("deploymentErrors")
current = json.dumps([properties.get("provisioningState"), deployment_errors])
if (
    properties.get("provisioningState") == "Failed"
    and deployment_errors
    and current != baseline
):
    sys.stderr.write("deployment failed: %s\n" % (deployment_errors,))
    raise SystemExit(2)

matches = [
    container
    for container in app["properties"]["template"]["containers"]
    if container.get("name") == container_name
]
if len(matches) != 1:
    raise SystemExit(
        "probe check failed: expected exactly one container named %r, found %d"
        % (container_name, len(matches))
    )
probes = matches[0].get("probes") or []
missing = [
    kind
    for kind in ("Startup", "Liveness")
    if not any(
        str(probe.get("type", "")).lower() == kind.lower()
        and (probe.get("httpGet") or {}).get("path") == "/api/health"
        and (probe.get("httpGet") or {}).get("port") == 8000
        for probe in probes
    )
]
if missing:
    raise SystemExit(
        "probe check failed on container %r: missing %s probe(s) on /api/health:8000"
        % (container_name, ", ".join(missing))
    )
print("probes verified on container %r: Startup, Liveness" % container_name)
' "$APP_CONTAINER_NAME" "$BASELINE" || PROBE_STATUS=$?
  if [ "$PROBE_STATUS" -eq 0 ]; then
    PROBES_OK=1
    break
  fi
  if [ "$PROBE_STATUS" -eq 2 ]; then
    printf 'the new revision did not provision; not waiting out the poll\n' >&2
    exit 1
  fi
  sleep 10
done
[ -n "$PROBES_OK" ] || exit 1
```

Changing the template creates a new revision. In multiple-revision mode, check
which revision has traffic rather than assuming this patch moved it. For later
deploys, follow `docs/deploy-cutover-runbook.md` §6: pin the current revision,
check the new revision's health, and move traffic explicitly only after it is
healthy. The first app revision receives initial traffic; the probe patch is a
separate template revision, so verify traffic and health after applying it.

## OneDrive sync job

A scheduled Container Apps Job runs one OneDrive archive pass
(`app/storage/onedrive_sync.py`) every 30 minutes, from the **same GHCR
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
  --command "/app/backend/.venv/bin/python" \
  --args "/app/backend/app/storage/onedrive_sync.py" \
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
    ENVIRONMENT=<non-local-environment-name> \
    PYTHONPATH=/app/backend

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
  live in `/app/backend` (see the repo-root `Dockerfile`), hence the absolute
  paths. No `--command`/`--args` token may start with a dash: az parses a bare
  `-c` or `-m` as one of its own options and silently drops it (an earlier
  `--command "sh" "-c" "..."` became command `["sh"]` and every run failed with
  `sh: 0: cannot open ...`). So the module runs by file path, not `-m`. That
  puts `app/storage` on `sys.path` instead of `/app/backend`, hence
  `PYTHONPATH=/app/backend`: the module uses only absolute `from app...`
  imports and has a `__main__` block. `STATIC_FILES_DIR` is baked into the
  image and is not set here.
- **Env/secret split** follows `docs/deploy-cutover-runbook.md` §2: everything
  is a secret except `S3_REGION`, `GRAPH_ONEDRIVE_FOLDER`, `ENVIRONMENT` and
  `PYTHONPATH`.
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
