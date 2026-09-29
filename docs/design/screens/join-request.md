# Screen: Request to join and pending (`/trips/$tripId/join`)

Access model: decision-log Entry 29 (ADR §5 per-person requests, cooldowns, blocks, caps; §11 legacy claim).
Mockup: `docs/design/mockups/join-pending.html`.

## Design feature
A signed-in non-member asks to become a rider on one trip. Requests are per person (each account asks for itself),
with an optional message (≤ 280) so a group can say "we're the three riders on the Tuesday ferry". The screen then
shows the pending state until a leader decides. Approval is the only way to get write access.

## Design format — request
Frame: phone 390, column max 480, gap 16.

1. TopBar: back to the trip.
2. Trip summary row: trip name (heading) + visibility badge.
3. H1 "Ask to join this trip".
4. Body: "A leader of this trip will see your display name, **<display name>**, and your message. Once they approve
   you, you can add stops and photos."
5. "How joining works" (small, numbered list, DESIGN.md §10.2).
6. TextArea "Message to the leaders (optional)" with the counter "0 / 280". Helper: "Say who you are, e.g. which
   bike you ride."
7. Primary lg "Send request". Loading: "Sending…".

## Design format — pending (same route, and mirrored on the trip header)
1. Pending badge + H1 "Request sent".
2. Info notice (`clock`): "Waiting for a leader to approve you. You'll see **Add stop** on the trip once you're
   approved. Nothing to do until then."
3. Your message quoted (if any) + "Sent 10 Oct, 9:12 am ACST".
4. Secondary md "Back to the trip" · tertiary "Cancel request" (no confirm: re-requesting straight away is allowed).
5. Small muted line: "Approval isn't instant. Leaders may be riding. Check back later."

The pending state is recomputed from `viewer.role === "pending"` on each visit. There's no polling faster than the
normal query refetch-on-focus.

## States
| State | Presentation |
|---|---|
| Anonymous | Redirect to `/signin?next=/trips/$tripId/join` |
| Already rider/leader (role) or 409 "already a member" | Redirect to the trip with toast "You're already on this trip" |
| Duplicate submit while pending (200 existing) | Shows the pending state (same as success) |
| Rejected, in cooldown (409 CONFLICT) | Warning notice with the envelope message, or the default "A leader didn't approve your request. You can ask again 7 days after it was declined." Form hidden. If C4 provides a date: "You can ask again from 17 Oct." |
| Revoked, in cooldown (409) | Same as rejected, with the envelope message (e.g. removed from the trip in the last 7 days) |
| Blocked | Warning notice "You can't request to join this trip. Contact the trip's leader if you think this is a mistake." No form |
| Caps (too many pending requests, per user or per trip) | Warning notice with the server message |
| Rate limited (429) | "Too many requests. Try again in N minutes." |
| Cancelled | Toast "Request cancelled"; the form reappears, empty |
| Approved (on next visit) | Role becomes rider: redirect to the trip; one-time success notice "You're in. Tap Add stop to add your first stop." |
| Private trip, non-member | They can't reach it: the standard 404 "Trip not found" (see note) |
| Offline | Danger notice "You need a connection to send a request." Message kept |

Note, private trips: a private trip returns 404 to non-members (ADR §2), so a non-member can't open its join page.
The ADR's paths to join a private trip are a legacy rider-link claim (`legacy-link.md`) or the leader making the trip
public. **Flag for `ba`/`architect`:** there's no invite mechanism for new private trips. That's a product gap, and
this design doesn't invent one.

## APIs called
- `GET /api/v2/trips/{tripId}` (`viewer.role`).
- `TBC: POST /api/v2/trips/{tripId}/join-requests` `{message?}`; `TBC: DELETE` own pending request; `TBC` own
  request status (DESIGN.md §13 C4, C6).

## Handoff checklist
- [ ] The counter blocks input past 280 characters (or shows an error) and is announced politely.
- [ ] The pending state shows on both `/join` and the trip header; Add stop is absent while pending.
- [ ] Cancel needs no confirmation and the form is usable immediately after.
- [ ] Rejected/blocked states show no Send button.
- [ ] Only the display name is mentioned as visible to leaders; the username is never shown here.
