# Screen: Create trip (`/trips/new`) and the initial leader state

Access model: decision-log Entry 29 (ADR §1 visibility default public, §4 creation, §10 limits: 3/day, 20 per user).

## Design feature
A signed-in person starts a trip and becomes its first leader. It's public by default (owner goal), with the
privacy trade-off shown before creation. Creation is an online action with a client-generated id, so a retry
replays idempotently (ADR §4).

## Design format
Frame: phone 390, column max 480, gap 16.

1. TopBar: back, "New trip".
2. H1 "Create a trip".
3. TextField "Trip name" (required). Helper: "For example: Spring loop 2026".
4. Date field "Start date" (native `<input type="date">`, 48 px, required).
5. Fieldset "Who can see it?" with two large radio cards (each 72 px min, full width, 2 px `border/strong`, selected
   = 2 px `brand/primary` + `primary-subtle` bg + `check-circle`):
   - **Public** (`globe`), default: "Anyone can find it and follow along. Stops appear publicly 24 hours after
     they're added."
   - **Private** (`lock`): "Only members can see it. Nobody can ask to join a private trip."
6. When Public is selected, an inline `warning` notice (not a dialog at creation; this is the Publish-dialog
   content from DESIGN.md §9, inline): "**Check your first stop.** Trips often start at someone's home. Public
   viewers see each stop's exact location."
   When Private is selected, an inline `info` notice instead (C13 steer; no invite feature exists): "**Riders
   can't join a private trip.** If others are riding with you, keep it public until they've joined. You can make it
   private later in Trip settings, and members keep their access."
7. Primary lg "Create trip". Loading: "Creating…".

After success: navigate to `/trips/$tripId` with the **initial leader state**:
- Header shows `Leader` + the visibility badge.
- A one-time success notice: "Your trip is ready. You're its leader."
- A "Next steps" Card on the Map tab (dismissable): 1. "Share the trip link with your riders: they sign in and
  tap Request to join." with a secondary "Copy trip link" (copies `https://<origin>/trips/<id>`, "Copied" inline).
  2. "Approve them in **Members**." 3. "Add your bikes in **Bikes**." 4. "Tap **Add stop** when you set off."
- EmptyState for stops as in `trip-detail.md` (rider variant).

## States
| State | Presentation |
|---|---|
| Not signed in | Redirect to `/signin?next=/trips/new` |
| Field errors | "Enter a trip name." · "Use 100 characters or fewer." · "Choose a start date." |
| Validation (`422`) | Error summary with the envelope message |
| Daily limit (`429`, 3 trips per day) | Warning notice with the envelope message + "Try again in N hours." from `Retry-After` (DESIGN.md §5.6 rounding); Create disabled until then. A replay of the same id spends no token |
| Lifetime cap (`409`, 20 trips created) | Warning notice with the envelope message verbatim; Create disabled for this visit. Not retryable |
| Other `409` (the id already belongs to someone else, or the creator is no longer a member of the trip it names) | Error summary with the envelope message; a fresh id is generated on the next tap |
| Offline | Danger notice "You need a connection to create a trip." Input kept. Creation is online-only and never queued |
| Retry after a timeout | Same client id; a replay (`200`) returns the existing trip and navigates normally |
| Cold start | Button loading + waking-up notice |

## APIs called
- `POST /api/v2/trips` (`TripCreate`: `id` canonical lowercase UUID generated at submit, `name`, `startDate`,
  `visibility` default `public` → `TripOut` with `viewer.role = "leader"`): `201` new, `200` replay, `401`, `409`,
  `422`, `429`.
- Then `GET /api/v2/trips/{tripId}` as the trip screen.

## Handoff checklist
- [ ] Public is preselected and the first-stop warning is visible while it's selected; selecting Private shows the
  "Riders can't join a private trip" notice instead.
- [ ] The radio cards are one fieldset with a legend; arrow keys move between options.
- [ ] Double-tapping Create sends one trip (same id); a network retry doesn't create a duplicate.
- [ ] After creation the header shows Leader, and the Members tab and settings icon are present.
- [ ] "Copy trip link" copies an `/trips/<id>` URL, never a slug.
