# Ride tracks (m7) — size and token estimates

Rough planning figures from 2026-10-07, before any build task started. Accuracy about ±50%. Story: `s-ride-track-upload` in `docs/progress.json`; contract: `docs/api-contract.md` (Ride tracks); decision: Entry 34.

## Code size

About **2,500–3,500 new lines across ~25 files**, and **under 100 lines changed in existing code**. Most of it is tests. Calibration: photo upload is ~1,000 lines of code plus a 583-line test file (`test_jpeg_strip.py`).

| Task | Lines (code + tests) | Notes |
|---|---|---|
| t-trk-contract | ~270 | Done; docs only |
| t-trk-fit-core | ~700 | ~250 code, ~450 tests; hand-built FIT fixtures |
| t-trk-store-api | ~900 | Migration 0005, repo, storage helper, models, routes, tests |
| t-trk-delete-acl | ~350 | Mostly access-control tests |
| t-trk-design-spec | ~100 | `docs/design/` only |
| t-trk-frontend | ~500 | One new component plus a small `TripMap.tsx` edit |
| t-trk-gpx | ~350 | Reuses the track model and endpoints |

Existing files touched, all small: `TripMap.tsx` (one extra layer), the router include, the track route's own body cap (photos' 16 MiB cap unchanged), the lockfile and the regenerated frontend client. `map.py` is untouched.

## Token use

About **1.4–2.1M tokens** for the whole story, all agents combined, on top of ~220k already spent on planning. Extrapolated from agent-reported usage this session (architect research ~27k, architect design ~38k, `ba` ~45k, `docs` ~88k, QA ~24k). The main session re-reads its context every turn, mostly at the cached rate, and is not in the per-task figures.

| Task | Agents | Tokens |
|---|---|---|
| t-trk-fit-core | dev, qa, docs | 200–300k |
| t-trk-store-api | dev, test-writer, qa, docs | 400–600k |
| t-trk-delete-acl | dev, test-writer, qa | 250–350k |
| t-trk-design-spec | designer | 60–100k |
| t-trk-frontend | dev, qa, docs | 250–400k |
| t-trk-gpx | dev, qa | 150–250k |
| t-trk-real-fit | dev, qa | 80–120k |

Cheapest slice to run first, to see the real cost: t-trk-fit-core (~250k).

Ways to spend less, none recommended over keeping the gates: merge store-api and delete-acl into one patch (~100k saved, harder review); skip the design spec (~80k; the screen follows the photo-upload pattern); skip scrum-master post-flight checks. Each extra QA-fix round costs 50–100k.

## Not in these estimates

OneDrive archive of originals (owner-gated), elevation profile, photo placement along the track, offline upload, privacy trim, planned-route GPX, accommodation. All are filed as debt rows with promotion events.
