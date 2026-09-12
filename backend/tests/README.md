# Backend tests

`unit/` — stop/photo/bike creation and validation (spec Section 12, "lighter touch").

`integration/` — the three priority failure modes, written contract-first per the
Test agent's scope (spec Section 11): access control (403 on viewer-slug POSTs),
data integrity (a photo is never silently lost on OneDrive sync failure), and the
offline queue's simulated network-loss/restart/resume scenario.

Run: `cd backend && uv run pytest`
