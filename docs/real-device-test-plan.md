# Real-device test plan

For the Week 4 real-device test day (spec §7 and §12). Written by task `t-real-device-test-plan`; the
test day itself is the owner task `t-owner-real-device-test-day`, story `s-real-device-testing`. The goal is to confirm the offline queue, access control and photo handling on
real phones, and to test the one design assumption still at MEDIUM confidence (iOS `start_url`,
decision-log Entry 18).

**This needs the production HTTPS URL** (`docs/deploy-cutover-runbook.md`, step 7). The service worker,
geolocation and `crypto.randomUUID()` only work in a secure context, so a LAN `http://` address will
give false failures.

**What you need:**
- Two phones, ideally one iPhone and one Android.
- The rider link and the viewer link.
- A few HEIC photos taken in portrait orientation with the iPhone camera.
- Neon SQL access for case 12.

Record a **FAIL** with the device, the OS version, what you saw, and a screenshot of the queue notice.

---

### 1. iOS install and first launch (Entry 18, MEDIUM confidence)
Steps:
1. On the iPhone, open the rider link in Safari.
2. Tap Share, then Add to Home Screen.
3. Launch the app from the home screen.

Expected: the app opens at `/` and shows the **"Paste your trip link"** screen, because the installed
app's storage is separate from Safari's. Paste the rider link and the trip opens. Close the app and
launch it again: it goes straight to the trip.
If the app instead opens straight at the trip on first launch, record that. It is harmless, but it means
the Entry 18 assumption was wrong.
- [ ] Pass  - [ ] Fail

### 1a. Android install and first launch
Steps:
1. On the Android phone, open the rider link in Chrome.
2. Install it: accept Chrome's install prompt, or use the menu's **Install app** (older Chrome: **Add to
   Home screen**, which should still create an installed app, not a bookmark).
3. Launch the app from the home screen.

Expected:
- Chrome offers **Install**. If it only offers a plain shortcut, record a FAIL: the manifest icons
  (`t-pwa-manifest-icons`) are meant to make the app installable.
- The home-screen icon is the app's own icon, not a generic letter or a page screenshot.
- The splash screen and the status bar use the app's **dark green** theme colour.
- The app opens **straight at the trip**, with no paste-link screen. On Android the installed app shares
  Chrome's storage, so it already knows the last trip. (This is the opposite of iOS in case 1.)
- [ ] Pass  - [ ] Fail

### 2. Rider vs viewer
Steps:
1. Open the rider link.
2. Open the viewer link on the other phone.

Expected:
- **Rider link:** shows the display-name prompt once, plus the "Add stop" link.
- **Viewer link:** shows no prompt and no "Add stop" link. Opening `/t/<viewer>/add` directly sends the
  viewer back to the trip.
- **The viewer is never asked for location.** Use a browser profile or phone that has never granted
  location to the site. No location permission prompt appears at any point, including when opening
  `/t/<viewer>/add` directly. (Only a rider on Add stop requests GPS.)
- [ ] Pass  - [ ] Fail

### 3. Airplane-mode capture, close, reopen, resync (spec §12, top priority)
Steps:
1. Open the installed app online once, then turn on airplane mode.
2. Add a stop with 2 photos and save it.
3. Force-quit the app, then reopen it while still in airplane mode.
4. Turn airplane mode off and bring the app to the foreground.

Expected:
- While offline, the queue notice shows **"Waiting to send: 1 stop, 2 photos"**, and it is still there
  after the force-quit and reopen.
- Once back online, the count clears without any action from you, and the stop appears on the map and
  in the timeline with both photos.
- The second phone shows the stop after a refresh.
- [ ] Pass  - [ ] Fail

### 4. Capture from the installed app, not Safari (Entry 18)
Steps:
1. Go offline and queue a stop in **Safari**.
2. Open the **installed app**.

Expected: the installed app does not show the Safari stop, because the two have separate storage. Go back
online in Safari and the stop sends from there. This confirms the user-guide instruction to capture from
the installed app.
- [ ] Pass  - [ ] Fail

### 5. Offline cold open of a trip link
Steps:
1. Open the rider link and the viewer link once each while online.
2. Turn on airplane mode, force-quit, and open each link again.

Expected: the trip name, stops and timeline render from the saved trip data. Map tiles may be blank.
The app does not show "Trip not found" or a browser error page.
- [ ] Pass  - [ ] Fail

### 6. GPS denied, set the location with a map tap
Steps:
1. Deny location permission for the site or app.
2. Tap Add stop.

Expected: the screen shows "GPS unavailable: tap the map to set the location". Tapping the map shows
"Location: …(map tap)", and a second tap moves it. After saving, the stop shows
**"· approximate location"** in the timeline and on the stop detail page.
- [ ] Pass  - [ ] Fail

### 6a. GPS prompt dismissed without choosing
Steps:
1. Reset the site's location permission so the browser asks again.
2. As a rider, tap Add stop.
3. When the location prompt appears, **don't choose**: dismiss it (tap outside it, or swipe it away), or
   just leave it open.

Expected: within about **15 seconds** the screen switches to "GPS unavailable: tap the map to set the
location", and the map tap works as in case 6. The form does not wait for an answer forever. (The app
runs its own 15-second timer, because a browser may never report a dismissed prompt.)
- [ ] Pass  - [ ] Fail

### 7. GPS allowed
Steps: grant location permission, then add a stop.

Expected: the screen shows "Location: …(GPS)" with a sensible position, and the saved stop has no
"approximate location" tag.
- [ ] Pass  - [ ] Fail

### 8. Two riders uploading at once
Steps: on both phones, using the rider link, add a stop with 3 or more photos and tap Save at the same
moment.

Expected: both stops and all photos arrive, nothing is duplicated or missing, and both phones' queue
notices clear.
- [ ] Pass  - [ ] Fail

### 9. HEIC and portrait photos
Steps: attach iPhone HEIC photos taken in portrait orientation to a stop, including one taken at a known
time several hours ago.

Expected:
- The thumbnails and the enlarged photo are **upright**.
- The upload is a JPEG no larger than 1600px on its long edge.
- The photo's `takenAt` (check it with the photo list GET under `/docs`) matches the camera time,
  including your UTC offset, not the upload time.
- [ ] Pass  - [ ] Fail

### 10. Queued item stuck at 10 or more attempts
Steps: in airplane mode, queue a stop, then keep the app open for about 20 minutes. The backoff runs
5s, 10s, and so on, up to a cap of 5 minutes. Switching away from the app and back triggers extra
attempts and speeds this up.

Expected: the queue notice still shows the stop under "Waiting to send", plus
**"<name> still trying: <error>"**. After you go back online, the stop sends and both lines clear.
- [ ] Pass  - [ ] Fail

### 11. Failed item and Dismiss
Steps: run this as part of case 12, which is the realistic way to produce a failure that will never be
retried.

Expected:
- A failed stop shows **"<name> could not be sent: <message>"** with a **Dismiss** button, and its queued
  photos are marked failed along with it.
- Failed items are not counted in "Waiting to send".
- Tapping Dismiss removes only the item you tap.
- [ ] Pass  - [ ] Fail

### 12. Slug rotation drill (Entry 21)
You run the SQL yourself; don't paste slugs into an agent session.

Steps:
1. On phone A, go offline and queue a stop with 1 photo using the rider link.
2. In the Neon SQL editor, rotate the slug:
   `UPDATE trips SET rider_slug = '<new token>' WHERE rider_slug = '<old>';`
   Generate the new token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. It must
   differ from `viewer_slug`, which a CHECK constraint enforces.
3. Bring phone A back online.
4. Open the old link while online, and open the new link.

Expected:
- The queued stop and photo fail visibly (see case 11). They are not retried in a loop and not silently
  dropped.
- The old link shows **"Trip not found"**.
- The new link loads the trip with rider access.

Afterwards: either share the new link, or set `rider_slug` back to its original value. Setting it back
is fine after a drill because no link actually leaked.
- [ ] Pass  - [ ] Fail

### 13. Bikes page: view, add, edit
Steps:
1. As a rider, open Bikes, add a bike, then edit its specs.
2. Open Bikes with the viewer link.
3. Try Add bike while in airplane mode.

Expected:
- The rider's new bike appears, sorted by rider name, and the edit is saved.
- The viewer sees the same list with no Add or Edit controls.
- Offline, the page shows "You're offline — try again when connected." and keeps your input. Bikes are
  not queued.
- [ ] Pass  - [ ] Fail

### 14. Access control, on a phone
Steps: using the viewer link, try to reach any write path, for example `/t/<viewer>/add`, or the
`POST /api/trips/<viewer>/stops` operation under `/docs`.

Expected: the UI offers no way to write, and the API returns `403 FORBIDDEN`.
- [ ] Pass  - [ ] Fail

### 15. Times show their time zone (decision-log Entry 26)
Steps:
1. Add a stop and note the phone's clock time.
2. Look at the stop's time in the timeline, on its map pin label, and on the stop detail page.
3. If you can, open the viewer link on a device set to a different time zone (or change one phone's
   time zone in Settings and reopen the app).

Expected:
- Every time carries a zone label, for example "ACST" or "GMT+9:30". No time appears without one.
- On the phone that added the stop, the time matches its clock.
- On a device in another zone, the time is shown in **that device's** zone, with that zone's label. The
  hour differs, but it is the same moment. This is by design: times are shown in the reader's zone, not
  the rider's.
- [ ] Pass  - [ ] Fail
