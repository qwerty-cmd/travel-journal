# Offline queue

IndexedDB-backed queue for stop/photo capture with no signal (spec Section 4;
decision-log Entry 19). A captured stop or photo is written here first, then
drained to the API when it can be sent. Built in Week 3, evaluated with a
scripted network-loss/restart/resume test in Weeks 2–3 and confirmed on real
devices in Week 4 (spec Section 12).

- `queue.ts`: the queue itself (`enqueue`, `drain`, `subscribe`, `dismiss`,
  `startQueue`).
- `QueueNotice.tsx`: the "Waiting to send" / failed / still-trying notice,
  mounted in the root route so it shows on every screen.

## Photos

Photos arrive already processed. `src/photo.ts` (`processPhoto`) caps the long
edge at 1600px and re-encodes to JPEG before enqueue, and the queue stores
those JPEG bytes as-is (as an ArrayBuffer, not a Blob). Only formats the
browser can decode get through: HEIC is re-encoded where the browser can
decode it, but where it can't (e.g. desktop Chrome) `processPhoto` throws
`PhotoDecodeError`, the add-stop form shows "Couldn't read photo <name>", and
nothing is queued.

## Sending

- Drained FIFO from the page, one entry at a time. It does not use Service
  Worker Background Sync (iOS lacks it) and is never gated on
  `navigator.onLine`. Triggers are app start, `enqueue`, the `online` event,
  the tab becoming visible, and a 5s → 300s doubling backoff after a retryable
  failure.
- Stop: `POST /api/trips/{slug}/stops`. Photo:
  `POST /api/trips/{slug}/stops/{stop_id}/photos`, one idempotent request per
  photo, retried whole under the same client id.
- Timeouts: every send is aborted after **30s for a stop** and **120s for a
  photo** (a ~1 MiB photo on a weak cellular link can take over a minute). An
  abort retries with backoff, like a network failure.
- The five never-retry envelope codes (`VALIDATION_ERROR`,
  `METHOD_NOT_ALLOWED`, `CONFLICT`, `FORBIDDEN`, `NOT_FOUND`) mark the entry
  `failed`. A failed stop also fails its pending photos. Failed entries stay
  until the rider dismisses them. Anything else counts an attempt and retries.

## Multiple tabs

All tabs share the one IndexedDB store.

- **Drain lock.** Where the Web Locks API exists, each drain pass requests the
  `navigator.locks` lock `btj-queue-drain` with `ifAvailable`. Only the tab
  holding it sends, so two tabs never send the same entry. A tab that can't
  get it skips the pass, because the holder is draining the same store.
  Without Web Locks, each tab falls back to its own in-memory one-drain guard.
- **Broadcast.** Where BroadcastChannel exists, every change is posted on
  `btj-queue`. `changed` makes other tabs' subscribers (QueueNotice) re-read.
  `enqueued` does that too and also wakes the receiving tab's drain, so an
  entry added in a tab without the lock is sent by the tab that has it.

Service worker asset caching (installability) is handled by vite-plugin-pwa in
vite.config.ts. This directory is the app-level queue logic, not the SW itself.
