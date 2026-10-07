# Screen: Stop detail and gallery (`/trips/$tripId/stops/$stopId`)

Access model: decision-log Entry 29 (ADR §1 public delay, §9 public photos via presigned URLs). Times: Entry 26.

## Design feature
One stop: its name, when it was reached (with a zone label), whether the location is approximate, notes, and photos
with who took them. Read-only for everyone except riders and leaders, who can **add photos to this existing stop**
(owner decision 2026-10-07). Gate: `viewer.role` is `rider` or `leader`, never `isMember` or `access`. Visitors,
pending requesters, `none` and anonymous see the page read-only, with no Add photos control. Photos go through the
same offline queue as add-stop's: saved on the device, sent when possible. The existing behaviour stays: 1 h
presigned URLs are never persisted, there's one refetch on an image error, and the enlarged view is looked up by id.
Editing or deleting existing photos is not part of this.

## Design format
Frame: phone 390, gap 16, page padding 16.

1. TopBar: back "Back to trip" (IconButton + visually the arrow; accessible name "Back to trip") · trip name.
2. H1 stop name (title).
3. Meta (small, muted): `<time>` "Tue 14 Oct 2026, 4:05 pm ACST" + " · approximate location" (with `pin-approx`
   icon) when manual. Members of public trips, recent stop: the "Public from 4:05 pm tomorrow ACST" chip
   (`arrivedAt + TripOut.publicDelayHours`; C5 resolved).
4. *(Optional, second pass; not needed for parity)* Mini map, 160 px, static, with a single pin. Offline: grid
   background + "Map needs a connection". Omit it in the first implementation.
5. Notes (body, `white-space: pre-line`, max 68ch), if any.
6. **Photos header row** (horizontal auto layout, gap 12, align centre, space between): H2 "Photos (12)" (hug, left)
   and, **riders and leaders only**, a secondary md button "Add photos" with the `camera` icon (hug, right,
   height 48). It sits in the header row so it's reachable without scrolling past a long grid. Below 320 px the
   row wraps and the button goes full width under the H2. The button triggers a hidden
   `<input type="file" accept="image/*" multiple>` (the label stays linked for screen readers). The same control
   shows in the empty state (see States).
7. **Picked photos strip** (rider/leader, only while photos are picked or processing; same 72 px preview tiles with
   48 px "Remove photo N" IconButtons as add-stop, §7 there). Directly under the header row, above the grid.
   "Processing 2 photos…" (status) while processing; an undecodable file shows the inline danger line
   "Couldn't read photo IMG_0042.HEIC" (existing copy). Photos are processed at pick time (≤ 1600 px JPEG), as in
   add-stop. No cap on photos per add: add-stop has none, so this matches it.
8. **Strip action row** (inside the strip, only when ≥ 1 photo is picked): helper "Wait for photos to finish
   processing." while processing, then primary md "Save N photos" (singular "Save 1 photo"), and a text button
   "Cancel" that discards the picks. Inline in the page, not a BottomActionBar: the page is not a form and the
   grid below stays visible.
9. Photo grid (DESIGN.md §4.7). Each tile is a button, "Photo by Sam, taken 4:02 pm ACST".
10. Photo viewer (Dialog variant): counter "3 of 12", Close, Previous/Next, the caption "Photo by Sam · 4:02 pm ACST".

### Save behaviour
On "Save N photos": each photo is enqueued with `tripId`, the existing `stopId`, a fresh client id and `userId`, in
one IndexedDB transaction, then the strip clears and focus returns to "Add photos". The device is the record, so
there's no wait on the network. Feedback is the global QueueNotice ("Waiting to send: 2 photos", clears in seconds
online), as in add-stop; **no toast**. Simplest consistent choice: the gallery does **not** show queued photos. When
the queue sends them, the photos query for this stop is invalidated and the grid and the "Photos (n)" count
refresh. (If the owner wants a "waiting" tile later, that is a separate change.)

## States
| State | Presentation |
|---|---|
| Loading stops | Title and meta skeleton |
| Loading photos | 6 tile skeletons |
| No photos, rider/leader | EmptyState compact `image`: "No photos yet" with the "Add photos" button as its action (the header-row button is then hidden to avoid two) |
| No photos, anyone else | EmptyState compact `image`: "No photos yet" (existing copy), no action |
| Photos error | Envelope message or "Couldn't load photos" (existing) + Try again |
| Offline, viewing | "Photos need a connection." (accepted limit). The Add photos control still works (below) |
| Offline, adding | Picker, processing and Save work fully; the Offline notice is informational. After Save the QueueNotice shows "Waiting to send: 2 photos" |
| Processing photos | Strip shows "Processing 2 photos…"; Save disabled with "Wait for photos to finish processing." |
| Unreadable file | Inline danger line "Couldn't read photo IMG_0042.HEIC"; the other photos stay |
| Local save failure (IndexedDB) | Danger notice "Couldn't save the photos on this device" + "Try again"; picks are kept |
| Queued | QueueNotice "Waiting to send: N photos". The grid is unchanged |
| Uploaded (`201`, or `200` replay) | The photos query refetches; the grid and count update; QueueNotice clears. A replay is silent success |
| `401` while sending | The queue pauses (not failed, attempt not counted): "Sign in to send N items" (global-states); resumes after sign-in as the same account |
| `403`, including the role lost | Never retried. The item moves to Failed: "You're no longer a rider on this trip" (the envelope message), with "Save photo to this device" and Dismiss (DESIGN.md §5.6). `viewer.role` drops on the next trip fetch, and the Add photos control disappears; if the trip became private to them, the trip-level "Trip not found" applies |
| `404` on send (stop or trip gone) | Never retried. Failed with the envelope message, "Save photo to this device" offered |
| `409` on send | Never retried. Failed with the envelope message, "Save photo to this device" offered |
| `422` `VALIDATION_ERROR` (not a JPEG, or over 15 MiB) | Never retried. Failed with the envelope message (e.g. "Photo is too large" or "Photo must be a JPEG" as the server words it; show the server's text), "Save photo to this device" offered. Client processing makes this rare |
| `429` on send | Retried after `Retry-After`; the entry stays "Waiting", no separate notice |
| Visitor, pending, none, anonymous | No Add photos control, no strip; the page is as it was before |
| Expired URL | One silent refetch (existing); a tile that still fails shows "Photo unavailable" |
| Unknown stop, or a stop inside the public delay for a non-member | "Stop not found" + "Back to trip" (existing copy). The server answers both with the same `404` (the stop is absent from `/stops`, and its `/photos` is `404`), and the UI must not distinguish them either |
| Trip 404 | Trip-level "Trip not found" |
| `429` on a page load | Warning notice "Too many requests. Try again in N seconds." |

## Accessibility
- "Add photos" is a real `<button>` (name "Add photos", ≥ 48 px high), reached in DOM order right after the H2 and
  before the grid; Enter and Space open the picker. The hidden file input is not in the tab order.
- Remove buttons are ≥ 48 px and named "Remove photo N". After removing, focus moves to the next tile's Remove
  button, or to "Add photos" when none are left.
- "Processing…" and "Couldn't read…" are in a `role="status"` live region; the count in "Save N photos" updates
  with it. The disabled-reason is text, not only a greyed button.
- The control is hidden for non-writers by not rendering it (not `disabled`), so no unexplained dead control.
- Contrast and focus ring per DESIGN.md; the icon always has the word beside it.

## APIs called
- `GET /api/v2/trips/{tripId}` (from the shell's cache): `viewer.role` decides whether Add photos shows.
- `GET /api/v2/trips/{tripId}/stops` (`StopOut[]`; no stop-by-id endpoint; usually cached): `200`, `404`, `429`.
- `GET /api/v2/trips/{tripId}/stops/{stopId}/photos` (`PhotoOut[]`, presigned `url`s): `200`, `404`, `429`. Photo
  credit uses `uploadedBy`: the uploader's display name at upload time (or the free-text label on older photos).
- Sent later by the queue, not called by this screen directly:
  `POST /api/v2/trips/{tripId}/stops/{stopId}/photos` (multipart `id`, `takenAt`, `file`; no `uploadedBy`, the
  server stores the account's display name; writer gate): `201` new, `200` replay, `401`, `403`, `404`, `409`,
  `422`, `429`. Classification is the contract's "Offline-queue
  classification (Entry 29)" table; this screen doesn't change it.

## Handoff checklist
- [ ] The time shows a zone label; approximate stops show the icon and the words.
- [ ] The viewer: Escape closes, ←/→ move between photos, focus returns to the opening tile, and the buttons are ≥ 48 px.
- [ ] A portrait photo is shown whole in the viewer (contain), and square-cropped in the grid.
- [ ] A non-member opening a stop that's inside the delay sees exactly "Stop not found".
- [ ] Photo URLs aren't written to localStorage or IndexedDB.
- [ ] "Add photos" renders only when `viewer.role` is `rider` or `leader`; visitor, pending, none and anonymous see none (and no strip, no picker).
- [ ] With zero photos, a rider sees one "Add photos" (in the empty state), others see "No photos yet" with no button.
- [ ] Add photos and Remove photo N are ≥ 48 px high, named as written, and keyboard operable.
- [ ] Save is disabled with the reason text while photos process; "Save 1 photo" / "Save N photos" counts correctly.
- [ ] In airplane mode: Save clears the strip and "Waiting to send: N photos" appears; no toast.
- [ ] A new IndexedDB entry carries `tripId`, this `stopId` and `userId`, no slug, and the form sends no `uploadedBy`.
- [ ] After upload, the grid and "Photos (n)" refresh without a manual reload.
- [ ] `403` shows "You're no longer a rider on this trip" with "Save photo to this device", and is never retried; `429` waits and stays "Waiting"; `401` shows "Sign in to send N items".
- [ ] A non-JPEG or oversized rejection (`422`) lands in Failed with the server's message and the saved blob kept.
