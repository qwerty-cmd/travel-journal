# Screen: Trip settings (`/trips/$tripId/settings`), leader only

Access model: decision-log Entry 29 (ADR §1: visibility public/private, `public_delay_hours` 0–168, default 24;
the Publish dialog warning).

## Design feature
Where a leader controls the trip's exposure: Public or Private, and how late the public sees stops. It also shows
the leader what the public can and can't see.

## Design format
Frame: phone 390, column max 480, gap `space/6`. TopBar: back to the trip, "Trip settings".

1. H1 "Trip settings".
2. **Visibility** (fieldset, legend "Who can see this trip?"): current state card with a badge and a sentence
   ("Public: anyone can find and follow it." / "Private: only members can see it."), then a single button:
   primary md "Make public…" (opens the Publish dialog, DESIGN.md §9) or `danger-secondary` md "Make private…"
   (ConfirmDialog, DESIGN.md §8.3).
3. **Public delay** (shown for public and private trips; it applies once public): number input "Hours before stops
   are public" (48 px, `inputmode="numeric"`, 0–168) + suffix "hours", with quick-choice chips (buttons, 48 px): "0",
   "12", "24", "48". Helper: "A delay stops the public seeing where you are right now, or where you're camping
   tonight. 0 means stops are public immediately." When 0 is chosen, an inline `warning`: "With no delay, anyone can
   see your latest location as soon as you add a stop." Primary md "Save delay".
4. **What the public sees** (Card, small text list): "Map, stops, photos and bikes, <N> hours after each stop" ·
   "Riders' display names" · "Never: usernames, the member list, join requests".
5. **Legacy trips** (only if the trip came from the old two-link model): info notice "This trip was created with
   the old links and started private. Publishing it shares everything added so far." (The ADR requires an explicit
   leader Publish for these.)

## States
| State | Presentation |
|---|---|
| Not a leader | Redirect to the trip (the server rejects writes anyway) |
| Value out of range | Field error "Choose 0 to 168 hours." |
| Saving | Button loading; success inline "Saved. The public sees stops <N> hours after they're added." |
| Publish / unpublish success | Header badge updates; toast "Trip is now public" / "Trip is now private" |
| Delay value not exposed by the API (C5) | Section hidden until the contract provides it; visibility still works |
| Offline | "Changing settings needs a connection." Buttons disabled with that reason |
| 401 / 403 / 429 | Standard (DESIGN.md §7) |

## APIs called
`GET /api/v2/trips/{tripId}`; `TBC: PATCH /api/v2/trips/{tripId}` `{visibility?, publicDelayHours?}` (DESIGN.md §13
C5, C7).

## Handoff checklist
- [ ] Making the trip public always passes through the Publish dialog with the first-stop warning.
- [ ] Making it private always asks for confirmation.
- [ ] Choosing 0 hours shows the live-location warning.
- [ ] There's no per-stop "hide from public" control.
