# Screen: Legacy link landing (`/t/$slug` and children)

Access model: decision-log Entry 29 (ADR §11 migration: slugs grant no writes, rider-slug claim creates a
**pending** request; §12 legacy routes kept ≥ 120 days). Entry 18 (the original `/t/$slug` shape).

## Design feature
People still hold old links from the two-link era. The legacy screens keep working for **reading** (viewer slugs
keep read access to their private legacy trip), and they explain the change: adding stops now needs an account and
a leader's approval. A signed-in person with an old *rider* link can turn it into a join request with one tap. It
is never automatic access. Behaviourally the slug stays a locator; the UI never treats it as a credential.

## Design format
Same trip layout as `trip-detail.md` (header, tabs, timeline, stop detail), read from the legacy GETs, plus:

1. **Legacy notice** under the header (`info`, persistent on this route, not dismissable):
   - Title: "This is an old trip link"
   - Signed out: "Viewing still works. To add stops and photos, you now need an account and a leader's approval." +
     secondary md "Sign in" (`next` = this URL) + tertiary "Create an account".
   - Signed in, not a member: "To add stops, ask to join this trip." + secondary md "Request to join" → calls the
     claim endpoint (slug in the body, never in a new URL) → pending state: "Request sent. A leader of this trip will
     review it." with the Pending badge.
   - Signed in, member (v2 `viewer.role` rider or leader): "You're a member of this trip. Open it in the new view to
     add stops." + primary md "Open trip" → `/trips/$tripId`.
2. **No Add stop and no bike editing on legacy routes**, regardless of the legacy `access` value (DESIGN.md §13 C8).
   Members are sent to the v2 route to write.
3. The queue's already-stored legacy items continue to send against the legacy write paths with the session
   cookie (ADR §12). Their notices are the global ones.

## States
| State | Presentation |
|---|---|
| Waking up / error / offline cached | As today (`t.$slug.tsx` shell) with the new visual system |
| Unknown slug | "Trip not found" (the same component and copy as DESIGN.md §7) |
| Claim: already pending | The pending state (200 existing) |
| Claim: already a member (409) | Replaced by the "Open trip" variant |
| Claim: cooldown or blocked (409) | Warning notice with the envelope message |
| Claim: 401 | → `/signin?next=<this URL>` |
| Claim: 429 | Warning with minutes |
| Leader (via operator `grant_leader`) opens the old link | Member variant, "Open trip" |

## APIs called
Legacy reads `GET /api/trips/{slug}`, `/stops`, `/map`, `/stops/{id}/photos` (unchanged). `POST /api/v2/trips/claim`
`{slug}` (ADR §11). `GET /api/v2/trips/{id}` (using `TripOut.id`) to learn `viewer.role` for signed-in users. A 404
there simply means "not a member of a private trip" → show the non-member variant.

## Handoff checklist
- [ ] A legacy rider link never shows Add stop or Edit bike, signed in or not.
- [ ] Request to join posts the slug in the request body; the URL never gains a new slug parameter.
- [ ] After claiming, the Pending badge shows and no write control appears.
- [ ] Members see "Open trip", which goes to `/trips/<id>`.
