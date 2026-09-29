---
name: deploy
description: The devops agent's repeatable Azure Container Apps deploy/rollback steps. Use only for an actual deployment or rollback — not for local docker-compose dev, which needs no cloud steps at all.
---

# Deploy

Deployment target is Azure Container Apps (free tier), built from the repo-root `Dockerfile` — the same image that runs locally via `docker-compose`. This is last in build priority (spec Section 7/11): don't reach for this until the app already works end-to-end locally.

**One-time environment setup (Week 1, or whenever the environment doesn't exist yet):**
1. Create the Container Apps environment (free tier — scales to zero).
2. Set the low-threshold Azure budget alert (spec Section 13) as a tripwire, before anything else touches real cloud spend.
3. Wire secrets via `az containerapp secret set` — `DATABASE_URL` (Neon), `S3_ENDPOINT_URL`/`S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`/`S3_BUCKET_NAME` (R2), `GRAPH_CLIENT_ID`/`GRAPH_CLIENT_SECRET`/`GRAPH_REFRESH_TOKEN`; optional, each with a default: `S3_REGION`, `GRAPH_ONEDRIVE_FOLDER`, `ENVIRONMENT` (leave `S3_PUBLIC_ENDPOINT_URL` unset in production — R2's endpoint is already browser-reachable). Names must match `.env.example` — values never get committed anywhere. Container Apps secret names are the env var name in lower kebab case (`DATABASE_URL=secretref:database-url`); the app and the OneDrive sync job each hold their own copy under the same names.

**Every deploy:**
1. Build the image from the repo-root `Dockerfile` (multi-stage: builds the frontend, then the Python runtime serves both).
2. Push to GitHub Container Registry (GHCR) — the user's choice; ACR has no free tier and is not used.
3. **Apply migrations against Neon before moving traffic** (`python -m app.data.migrate`, run by the owner with the production `DATABASE_URL`). Migrations are forward-only; a new revision must never serve an old schema.
4. Update the Container App to the new image tag. Update the OneDrive sync Container Apps Job to the same tag (`az containerapp job update --image`), so it never runs a stale image against a newer schema — see `infra/azure/README.md`.
5. Confirm `/api/health` responds and the SPA loads, before considering the deploy done.

Exact commands for each step: `docs/deploy-cutover-runbook.md` §6.

**Rollback:** Container Apps keeps prior revisions — route traffic back to the last known-good revision rather than rebuilding forward under pressure.

**Never do without explicit human confirmation first:** provisioning new billable resources, changing a secret value, or a production cutover — per the `devops` agent's scope (spec Section 11), this work always needs review before it's applied, not just after.
