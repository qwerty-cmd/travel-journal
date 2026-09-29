# Screen: Trip settings (`/trips/$tripId/settings`), leader only

Access model: decision-log Entry 29 and `docs/api-contract.md` ("Access: public trips, members and leaders",
`PATCH /api/v2/trips/{tripId}` notes). Route naming: see `leader-review.md` (`ba`'s `/manage`, DESIGN.md §13 X1).

## Design feature
Where a leader controls the trip's name and its exposure: Public or Private, and how late the public sees stops. It
also shows the leader what the public can and can't see. There is no server-side publish confirmation: the Publish
dialog is the only guard, so it always runs.

## Design format
Frame: phone 390, column max 480, gap `space/6`. TopBar: back to the trip, "Trip settings".

1. H1 "Trip settings".
2. **Trip name**: TextField "Trip name" (required, 1–100 after trimming) + secondary md "Save name".
3. **Visibility** (fieldset, legend "Who can see this trip?"): current state card with a badge and a sentence
   ("Public: anyone can find and follow it, and riders can ask to join." / "Private: only members can see it. Nobody
   new can ask to join."), then a single button: primary md "Make public…" (opens the Publish dialog, DESIGN.md §9)
   or `danger-secondary` md "Make private…" (ConfirmDialog, DESIGN.md §8.3).
4. **Public delay** (shown for public and private trips; it applies once public): number input "Hours before stops
   are public" (48 px, `inputmode="numeric"`, 0–168) + suffix "hours", pre-filled from `TripOut.publicDelayHours`,
   with quick-choice chips (buttons, 48 px): "0", "12", "24", "48". Helper: "A delay stops the public seeing where
   you are right now, or where you're camping tonight. 0 means stops are public immediately." When 0 is chosen, an
   inline `warning`: "With no delay, anyone can see your latest location as soon as you add a stop." Primary md
   "Save delay".
5. **What the public sees** (Card, small text list): "Map, stops, photos and bikes, <N> hours after each stop" (for
   0: "as soon as each stop is sent") · "Riders' display names" · "Never: usernames, the member list, join requests".
6. **Publishing a private trip that already has stops**: the Publish dialog adds an `info` line "Publishing shares
   everything already added, not only new stops." (Trips from the old two-link model start private and need this
   explicit publish; `TripOut` has no "legacy" flag, so the line shows for **any** private trip, which is true for
   all of them.)

Each Save sends only the field it changes (`{name}`, `{visibility}` or `{publicDelayHours}`), never `null`.

## States
| State | Presentation |
|---|---|
| Not a leader | Redirect to the trip (the server rejects writes anyway) |
| Value out of range (client) | Field error "Choose 0 to 168 hours." / "Enter a trip name." / "Use 100 characters or fewer." |
| `422` from the server | Field error with the envelope message |
| Saving | Button loading; success inline "Saved. The public sees stops <N> hours after they're added." / "Name saved." |
| Publish / unpublish success | Header badge updates; toast "Trip is now public" / "Trip is now private" |
| Offline | "Changing settings needs a connection." Buttons disabled with that reason |
| `403` (no longer a leader) | Full-page Forbidden state (`global-states.md`) with "Back to trip" |
| `401` / `429` | Standard (DESIGN.md §7) |

## APIs called
- `GET /api/v2/trips/{tripId}` (`TripOut`: `name`, `visibility`, `publicDelayHours`, `viewer.role`).
- `PATCH /api/v2/trips/{tripId}` (`TripPatch`: `name?`, `visibility?`, `publicDelayHours?` 0–168; omit = no change,
  explicit `null` → `422`): `200`, `401`, `403`, `404`, `422`, `429`.

## Handoff checklist
- [ ] Making the trip public always passes through the Publish dialog with the first-stop warning.
- [ ] Making it private always asks for confirmation, and the dialog says nobody new can ask to join.
- [ ] Choosing 0 hours shows the live-location warning.
- [ ] Each save sends only the changed field, never a `null`.
- [ ] There's no per-stop "hide from public" control and no invite control.
