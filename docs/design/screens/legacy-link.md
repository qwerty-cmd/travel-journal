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
   - Signed in, not a member (`viewer.role` `none`): "To add stops, ask to join this trip." + secondary md "Ask to
     join as a rider" → calls the claim endpoint (the slug goes in the body as `riderSlug`, never in a new URL) →
     pending state: "Request sent. A leader of this trip will review it." with the Pending badge. It never grants
     access by itself.
   - Signed in, pending (`viewer.role` `pending`): the pending state above, with "Cancel request" as in
     `join-request.md`.
   - Signed in, member (`viewer.role` rider or leader): "You're a member of this trip. Open it in the new view to
     add stops." + primary md "Open trip" → `/trips/$tripId`.
2. **No Add stop and no bike editing on legacy routes**, regardless of the legacy `access` value (C8: `access` is now
   derived from membership, but the legacy routes stay read-only by design). Members are sent to the v2 route to
   write.

The UI can't tell a rider link from a viewer link (the slug grants nothing and the response doesn't say which it
is), so "Ask to join as a rider" shows on every legacy link. Only a rider link can be claimed; a viewer link answers
`404` (see States).
3. The queue's already-stored legacy items continue to send against the legacy write paths with the session
   cookie (ADR §12). Their notices are the global ones.

## States
| State | Presentation |
|---|---|
| Waking up / error / offline cached | As today (`t.$slug.tsx` shell) with the new visual system |
| Unknown slug | "Trip not found" (the same component and copy as DESIGN.md §7) |
| Claim: `201` new or `200` existing pending | The pending state |
| Claim: `404` (a viewer link, or a slug that no longer matches) | Warning notice "This link can't be used to join." + detail "Ask a leader of this trip for the rider link, or to make the trip public." The notice never says whether the link was a viewer link |
| Claim: already a member (`409`) | Replaced by the "Open trip" variant after the trip refetch |
| Claim: cooldown, block or pending cap (`409`) | Warning notice with the envelope message verbatim |
| Claim: `401` | → `/signin?next=<this URL>` |
| Claim: `429` (`join` bucket) | Warning "Too many requests. Try again in N minutes." |
| Leader (via operator `grant_leader`) opens the old link | Member variant, "Open trip" |

## APIs called
- Legacy reads `GET /api/trips/{slug}`, `/stops`, `/map`, `/stops/{id}/photos`: either slug, full undelayed reads.
  The legacy `GET /api/trips/{slug}` fills `TripOut.viewer` from the optional session, so `viewer.role` decides the
  notice variant with no extra call.
- `POST /api/v2/trips/claim` (`JoinClaimCreate`: `riderSlug` → `MyJoinRequestOut`): `201` new, `200` existing
  pending, `401`, `404`, `409`, `422`, `429`. The slug is never echoed back or logged.
- `POST /api/v2/join-requests/{requestId}/cancel` for the pending variant.

## Handoff checklist
- [ ] A legacy rider link never shows Add stop or Edit bike, signed in or not.
- [ ] "Ask to join as a rider" posts the slug as `riderSlug` in the request body; the URL never gains a new slug parameter.
- [ ] Claiming with a viewer link shows "This link can't be used to join."
- [ ] After claiming, the Pending badge shows and no write control appears.
- [ ] Members see "Open trip", which goes to `/trips/<id>`.
