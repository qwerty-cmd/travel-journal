# Offline queue

IndexedDB-backed queue for stop/photo capture with no signal (spec Section 4).
A captured stop or photo is written here first, then drained to the API when
connectivity returns. Photos arrive already processed: `src/photo.ts`
(`processPhoto`) caps the size at 1600px and re-encodes to JPEG (including HEIC)
before enqueue, and the queue stores those JPEG bytes as-is.
Built in Week 3, evaluated with a scripted network-loss/restart/resume test in
Weeks 2–3 and confirmed on real devices in Week 4 (spec Section 12).

Service worker asset caching (installability) is handled by vite-plugin-pwa in
vite.config.ts — this directory is the app-level queue logic, not the SW itself.
