# Offline queue

IndexedDB-backed queue for stop/photo capture with no signal (spec Section 4).
A captured stop or photo is written here first, client-side resized/compressed
(cap ~1600px, HEIC→JPEG), then drained to the API when connectivity returns.
Built in Week 3, evaluated with a scripted network-loss/restart/resume test in
Weeks 2–3 and confirmed on real devices in Week 4 (spec Section 12).

Service worker asset caching (installability) is handled by vite-plugin-pwa in
vite.config.ts — this directory is the app-level queue logic, not the SW itself.
