# Screen: Stop detail and gallery (`/trips/$tripId/stops/$stopId`)

Access model: decision-log Entry 29 (ADR §1 public delay, §9 public photos via presigned URLs). Times: Entry 26.

## Design feature
One stop: its name, when it was reached (with a zone label), whether the location is approximate, notes, and photos
with who took them. Read-only for everyone (photos are view-only in v1). The existing behaviour stays: 1 h presigned
URLs are never persisted, there's one refetch on an image error, and the enlarged view is looked up by id.

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
6. H2 "Photos (12)".
7. Photo grid (DESIGN.md §4.7). Each tile is a button, "Photo by Sam, taken 4:02 pm ACST".
8. Photo viewer (Dialog variant): counter "3 of 12", Close, Previous/Next, the caption "Photo by Sam · 4:02 pm ACST".

## States
| State | Presentation |
|---|---|
| Loading stops | Title and meta skeleton |
| Loading photos | 6 tile skeletons |
| No photos | EmptyState compact `image`: "No photos yet" (existing copy) |
| Photos error | Envelope message or "Couldn't load photos" (existing) + Try again |
| Offline | "Photos need a connection." (accepted limit) |
| Expired URL | One silent refetch (existing); a tile that still fails shows "Photo unavailable" |
| Unknown stop, or a stop inside the public delay for a non-member | "Stop not found" + "Back to trip" (existing copy). The server answers both with the same `404` (the stop is absent from `/stops`, and its `/photos` is `404`), and the UI must not distinguish them either |
| Trip 404 | Trip-level "Trip not found" |
| `429` | Warning notice "Too many requests. Try again in N seconds." |

## APIs called
- `GET /api/v2/trips/{tripId}/stops` (`StopOut[]`; no stop-by-id endpoint; usually cached): `200`, `404`, `429`.
- `GET /api/v2/trips/{tripId}/stops/{stopId}/photos` (`PhotoOut[]`, presigned `url`s): `200`, `404`, `429`. Photo
  credit uses `uploadedBy`: the uploader's display name at upload time (or the free-text label on older photos).

## Handoff checklist
- [ ] The time shows a zone label; approximate stops show the icon and the words.
- [ ] The viewer: Escape closes, ←/→ move between photos, focus returns to the opening tile, and the buttons are ≥ 48 px.
- [ ] A portrait photo is shown whole in the viewer (contain), and square-cropped in the grid.
- [ ] A non-member opening a stop that's inside the delay sees exactly "Stop not found".
- [ ] Photo URLs aren't written to localStorage or IndexedDB.
