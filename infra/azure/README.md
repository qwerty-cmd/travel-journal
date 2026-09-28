# Azure deployment (DevOps agent scope)

Deploys the single Docker image (built from the repo-root `Dockerfile`) to
Azure Container Apps, free tier. Filled in by the `devops` agent per the
`deploy` skill (`.claude/skills/deploy/SKILL.md`), Week 1 (initial environment)
and Week 4 (production cutover).

Planned contents (format: `az` CLI script blocks in this README — no
Bicep/Terraform/YAML):
- `az containerapp` CLI script — Container Apps environment + app definition,
  scale-to-zero on the free tier
- `az containerapp job` CLI script — the scheduled OneDrive sync job (below)
- Secrets wiring for `DATABASE_URL` (Neon), `S3_*` (Cloudflare R2),
  `GRAPH_REFRESH_TOKEN` — names/structure only, real values are set via
  `az containerapp secret set`, never committed
- Budget alert setup notes (spec Section 13 — a low-threshold tripwire, not a
  hard limit; the free-tier ceilings are far beyond this trip's usage)

## OneDrive sync job

A scheduled Container Apps Job runs one OneDrive archive pass
(`python -m app.storage.onedrive_sync`) every 30 minutes, from the **same GHCR
image** as the app. It is one-way background work: the app never waits on it.
A pass with `GRAPH_REFRESH_TOKEN` empty logs "not configured" and exits 0.

Definition only — nothing is provisioned until the owner runs it. Replace every
`<placeholder>`; secret values are never written here.

```sh
az containerapp job create \
  --name <job-name> \
  --resource-group <resource-group> \
  --environment <containerapps-environment> \
  --image ghcr.io/<owner>/<repo>:<tag> \
  --registry-server ghcr.io \
  --registry-username <github-user> \
  --registry-password <ghcr-read-packages-token> \
  --trigger-type Schedule \
  --cron-expression "*/30 * * * *" \
  --parallelism 1 \
  --replica-completion-count 1 \
  --replica-retry-limit 0 \
  --replica-timeout 900 \
  --cpu 0.25 --memory 0.5Gi \
  --command "sh" "-c" "cd /app/backend && exec .venv/bin/python -m app.storage.onedrive_sync" \
  --secrets \
    database-url=<value> \
    s3-endpoint-url=<value> \
    s3-access-key-id=<value> \
    s3-secret-access-key=<value> \
    s3-bucket-name=<value> \
    graph-client-id=<value> \
    graph-client-secret=<value> \
    graph-refresh-token=<value> \
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

Better practice for the secret values: create the job with `--secrets`
omitted, then set them with `az containerapp job secret set` so values never
land in shell history.

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
  secret names with the same values on the job, and rotate both together
  (e.g. when `GRAPH_REFRESH_TOKEN` is renewed:
  `az containerapp job secret set --name <job-name> --resource-group <resource-group> --secrets graph-refresh-token=<value>`).
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
