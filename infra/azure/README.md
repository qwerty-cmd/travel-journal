# Azure deployment (DevOps agent scope)

Deploys the single Docker image (built from the repo-root `Dockerfile`) to
Azure Container Apps, free tier. Filled in by the `devops` agent per the
`deploy` skill (`.claude/skills/deploy/SKILL.md`), Week 1 (initial environment)
and Week 4 (production cutover).

Planned contents:
- `containerapp.bicep` (or `az containerapp` CLI script) — Container Apps
  environment + app definition, scale-to-zero on the free tier
- Secrets wiring for `DATABASE_URL` (Neon), `S3_*` (Cloudflare R2),
  `GRAPH_REFRESH_TOKEN` — names/structure only, real values are set via
  `az containerapp secret set`, never committed
- Budget alert setup notes (spec Section 13 — a low-threshold tripwire, not a
  hard limit; the free-tier ceilings are far beyond this trip's usage)

Nothing here is a hard Azure dependency for the *app* — see spec Section 4
"Portability principle." This directory is the one place that's allowed to be
Azure-specific, since it's the deploy target, not the app.
