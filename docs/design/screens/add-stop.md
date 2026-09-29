# Screen: Add stop and photo upload (`/trips/$tripId/add`)

Access model: decision-log Entry 29 (ADR §3 offline and sessions, §7 writer gate, §8 revocation). Queue: Entry 19,
`frontend/src/offline/README.md`. Times: Entry 26. Mockup: `docs/design/mockups/add-stop.html`.

## Design feature
The rider's core job, done outdoors, one-handed, often with no signal. It captures a name, notes, a location (GPS
first, map tap as the fallback) and photos, then **saves to the device queue and returns** without waiting for the
network. **Behaviour unchanged from today** (`routes/t.$slug.add.tsx` header): the stop id and `arrivedAt` are fixed
at mount; GPS uses `enableHighAccuracy`; the app's own **15 s** fallback timer triggers the map; a late GPS fix never
replaces a tapped point; photos are processed at pick time (≤ 1600 px JPEG); Save is enabled only with a name, a
location and no photo processing; the stop and photos are enqueued in one transaction; the queue sends with 30 s
(stop) / 120 s (photo) timeouts and 5 s → 300 s backoff. Only riders and leaders reach it; anyone else is
redirected to the trip **before** geolocation is requested.

## Design format
Frame: phone 390, `surface/page`, column max 480, gap 16. The Save button lives in a BottomActionBar.

1. TopBar: close IconButton `x` "Cancel" (→ trip; if anything was entered, ConfirmDialog "Discard this stop?" /
   "Discard" danger / "Keep editing") and title "Add stop".
2. **Status stack** (global).
3. **LocationStatus** (Card, horizontal: 24 px icon + text stack + optional action):
   | State | Icon | Title | Detail |
   |---|---|---|---|
   | Pending (0–15 s) | `crosshair` with a slow pulse (static under reduced motion) | "Getting your location…" | "This can take a few seconds outdoors." |
   | GPS fix | `map-pin` (success) | "Location found (GPS)" | "-25.34410, 131.03690" (tabular, 5 dp) |
   | Fallback | `alert-triangle` (warning tone) | "GPS unavailable: tap the map to set the location" (existing copy) | "Or pan the map and tap **Use map centre**." |
   | Manual set | `pin-approx` | "Location set on the map" | coords + "Shown as approximate location." + tertiary "Move it: tap the map again" |
4. **Fallback map** (only in fallback / manual states): map frame 280 px, `radius/lg`, grid background (so an
   offline blank tile area still reads as a map), a fixed centre `crosshair` overlay (decorative), and the tapped
   point as the hollow dashed approximate pin. Under it a secondary md full-width "Use map centre" button (keyboard
   and switch access, WCAG 2.1.1; DESIGN.md §13 C15) and caption "With no signal the map may look blank. Tap as
   close as you can." (existing guidance).
5. TextField "Name" (required). Helper: "e.g. Roadhouse fuel stop".
6. TextArea "Notes" (optional).
7. **Photos** section: H2 "Photos" (visually small heading) + a secondary lg full-width button "Add photos"
   (`camera`), which triggers the hidden `<input type="file" accept="image/*" multiple>` (the label stays linked for
   screen readers). Below: a preview grid of 72 px tiles (object URLs), each with a 48 px Remove IconButton "Remove
   photo 2" at its top-right. "Processing 2 photos…" (status) while processing. An undecodable file shows the
   inline danger line "Couldn't read photo IMG_0042.HEIC" (existing copy).
8. Public-trip note (members of public trips, small, muted, `globe` icon): "This stop is visible to the public 24
   hours after it's saved."
9. **BottomActionBar**: a disabled-reason helper above the button when disabled ("Add a name and a location to
   save." / "Wait for photos to finish processing."), then primary lg "Save stop". On save: → the trip, where the
   global QueueNotice shows "Waiting to send: 1 stop, 2 photos" (or it clears in seconds online). No toast: the
   notice is the confirmation.

≥ 768: the map is 360 px high; the form stays max 480.

## States
| State | Presentation |
|---|---|
| Not rider/leader (`viewer.role`) | Redirect to the trip before any GPS request (unchanged) |
| Pending requester | Redirect to the trip (which shows the pending notice) |
| GPS pending / fix / fallback / manual | LocationStatus table above |
| GPS denied | Immediate fallback (existing) |
| Late GPS after a tap | Ignored; the manual point stays (existing) |
| Processing photos | Save disabled with reason |
| Local save failure (IndexedDB) | Danger notice "Couldn't save the stop on this device" (existing copy) + "Try again" |
| Offline | Works fully; the Offline notice is informational only |
| Signed out while on this screen (401 happens later in the queue) | Saving still works: the queue pauses and shows "Sign in to send N items" (ADR §3) |
| Revoked, items already queued | Items fail on send with "You're no longer a rider on this trip"; photos offer "Save photo to this device" (DESIGN.md §5.6) |
| Cold trip cache (no trip data offline) | The trip shell's "Can't reach the server"; Add stop needs the trip to have been opened once (unchanged) |

## APIs called
None directly. Reads `GET /api/v2/trips/{tripId}` from the shell's cache for `viewer.role`. The queue later sends
the stop and photo writes: **the paths for app-created trips are `TBC`** (DESIGN.md §13 C2). Legacy slug-addressed
entries keep `POST /api/trips/{slug}/stops` and `/photos` (ADR §7, §12).

## Handoff checklist
- [ ] Viewers, anonymous users and pending requesters never get a geolocation prompt (fresh profile).
- [ ] With the prompt ignored, the fallback appears at about 15 s (unchanged timer).
- [ ] Keyboard only: pan the fallback map with the arrow keys, press "Use map centre", and the location shows "set on the map".
- [ ] The approximate pin is hollow and dashed; LocationStatus says "approximate location".
- [ ] Save is disabled with visible reason text until a name and a location exist and no photo is processing.
- [ ] In airplane mode: Save returns to the trip and "Waiting to send: 1 stop, N photos" appears.
- [ ] The Remove buttons are ≥ 48 px and named with the photo number.
- [ ] The bottom bar never hides the focused Notes field (focus Notes with the keyboard open on a phone).
