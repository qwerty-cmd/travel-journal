---
name: deploy
description: The devops agent's repeatable Azure Container Apps deploy/rollback steps. Use only for an actual deployment or rollback — not for local docker-compose dev, which needs no cloud steps at all.
---

# Deploy

Deployment target is Azure Container Apps (free tier), built from the repo-root `Dockerfile` — the same image that runs locally via `docker-compose`. This is last in build priority (spec Section 7/11): don't reach for this until the app already works end-to-end locally.

**One-time environment setup (Week 1, or whenever the environment doesn't exist yet):**
1. Set the low-threshold Azure budget alert (spec Section 13) as a tripwire, **before** anything touches real cloud spend — including the environment below.
2. Create the Container Apps environment (free tier — scales to zero), then the app and the OneDrive sync Job. The exact commands are in `infra/azure/README.md`, in run order.
3. Wire secrets at creation (`--secrets`, values read without echo) — never in a committed file. Names must match `.env.example`; Container Apps secret names are the env var name in lower kebab case (`DATABASE_URL=secretref:database-url`).
   - **App:** `DATABASE_URL` (Neon), `S3_ENDPOINT_URL`/`S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`/`S3_BUCKET_NAME` (R2); plain vars `S3_REGION`, `ENVIRONMENT`. Leave `S3_PUBLIC_ENDPOINT_URL` unset — R2's endpoint is already browser-reachable.
   - **OneDrive sync Job:** the same database and R2 values, plus `GRAPH_CLIENT_ID`/`GRAPH_CLIENT_SECRET`/`GRAPH_REFRESH_TOKEN` and plain `GRAPH_ONEDRIVE_FOLDER`.
   - **The Graph secrets go on the Job only.** The web app never reads them (only `onedrive_sync.py` does), so least privilege keeps the long-lived OneDrive credential off the internet-facing app.

**Every deploy runs through `.github/workflows/deploy.yml` (decision-log Entry 32).** Agents never run it; the owner dispatches it and approves it in the GitHub `production` environment.
1. **Pick the image — don't build it.** Every push to `main` that passes CI publishes `ghcr.io/<owner>/<repo>:<full-commit-sha>` via `.github/workflows/ci.yml` (never `latest`). The deploy input is that full SHA.
2. The workflow, in order: verifies CI and the image; pins traffic to the serving revision; migrates with the manual Job (`job update --image`, then `job start` with **no options** — any start override replaces the whole container, env and command included); applies `infra/azure/app.yaml` as revision `rel-<sha12>` at 0% traffic; shifts traffic once the revision is healthy; updates the OneDrive sync Job to the same image; runs `infra/azure/smoke.sh`. Migrations are forward-only; a new revision never serves an old schema.
3. Committed YAML never carries a `secrets:` key (a partial list deletes the omitted secrets) or any secret value.
4. Only if Actions is unavailable: the manual fallback in `docs/deploy-cutover-runbook.md` §6, still owner-run. ACR has no free tier and is not used.

**Rollback:** `.github/workflows/rollback.yml` with the previous revision name (the deploy summary prints the exact command). It moves traffic only — migrations are not undone — and warns when the target predates Entry 29, which is security-degrading (runbook §7a). Clean old revisions with `deactivate-revisions.yml`, dry run first.

**Never do without explicit human confirmation first:** provisioning new billable resources, changing a secret value, or a production cutover — per the `devops` agent's scope (spec Section 11), this work always needs review before it's applied, not just after.
