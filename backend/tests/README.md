# Backend tests

Run: `cd backend && uv run pytest` (needs `docker compose up -d postgres minio`).
`import app` resolves through `pythonpath` in `pyproject.toml`.

## Layout

`unit/` — pure model and helper tests with no database or storage
(stop/photo/bike creation and validation, spec Section 12 "lighter touch").

Root `test_*.py` — everything that needs Postgres, MinIO or the running app,
one module per endpoint or concern.

## Where the priority failure modes are tested

Spec Section 12 ranks three failure modes above everything else. `integration/`
is reserved for them, but it has not been created yet: the tests that already
exist live at the root. Put new tests for these three modes in `integration/`
(create it) or next to the module they extend. Don't move the existing files
just to populate the directory.

- **Access control** (403 on viewer-slug writes, 404 on unknown slugs, no slug
  in any response): `test_slug_access.py`, `test_route_dependency_audit.py`,
  `test_no_slug_in_response_bodies.py`, `test_head_method.py`, plus the
  per-endpoint `test_*_endpoint.py` modules.
- **Data integrity** (a photo is never silently lost on OneDrive sync failure):
  `test_onedrive_sync.py`, `test_photos_pending_archive_repo.py`,
  `test_photo_upload_storage.py`.
- **Offline queue** (network loss / restart / resume): not a backend test. It
  runs in the frontend Vitest suite, in `frontend/src/offline/queue.test.tsx`
  and `queuePhotos.test.tsx`. The backend half is the replay/409 behaviour
  covered in the create-endpoint modules.
