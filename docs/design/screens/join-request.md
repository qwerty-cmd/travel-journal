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
normal query refetch-on-focus. The request itself (its id for Cancel, its message and date) comes from the create
response, or from `GET /api/v2/me/join-requests` matched to this trip.

**What the UI can know before sending.** `viewer.role` has no rejected or blocked value: those users are `none`.
`/me/join-requests` reports `blocked` as `rejected`, and carries no cooldown date. So the screen never predicts a
refusal and never shows a date or a "blocked" label from its own data. If the latest request for this trip is
`rejected`, it shows a neutral notice above the form and still lets the user send; the server's `409` message is the
only authority on cooldowns and blocks, and is shown verbatim.

## States
| State | Presentation |
|---|---|
| Anonymous | Redirect to `/signin?next=/trips/$tripId/join` |
| Already rider/leader (role), or `409` because already a member | Redirect to the trip with toast "You're already on this trip" |
| Duplicate submit while pending (`200` existing) | Shows the pending state (same as success); the original message is kept, not replaced |
| Latest request for this trip was `rejected` (from `/me/join-requests`) | Info notice above the form: "A leader didn't approve your last request. You can ask again. If it's too soon, you'll be told when you send." Form shown |
| Refused (`409 CONFLICT`) on send: cooldown, block, revocation by a leader in the last 7 days, or a pending cap (20 per person, 100 per trip) | Warning notice with the **envelope message verbatim** (these messages are written for the requester and carry no other record's data). The form is hidden for this visit; the typed message is kept in memory. The UI adds nothing that would distinguish a block from a cooldown beyond what the server said |
| Rate limited (`429`, `join` bucket 10/hour) | Warning notice "Too many requests. Try again in N minutes." from `Retry-After`; Send disabled until then |
| Validation (`422`, e.g. message over 280) | Field error under the message with the envelope message (the client counter should prevent it) |
| Cancelled (`200`) | Toast "Request cancelled"; the form reappears, empty. Re-requesting straight away is allowed |
| Cancel `409` (already decided while the page was open) | Info notice with the envelope message; the page refetches `viewer.role` (it may now be `rider`) |
| Cancel `404` | The pending state clears on refetch |
| Approved (on next visit) | Role becomes rider: redirect to the trip; one-time success notice "You're in. Tap Add stop to add your first stop." |
| Private trip, non-member | They can't reach it: the standard 404 "Trip not found". A create on a private trip is the same byte-identical `404` |
| Offline | Danger notice "You need a connection to send a request." Message kept |

Private trips have **no join path** for new riders (C13, filed as ordinary debt `t-am-private-trip-invites`). The
design doesn't design invites. Instead, creators are steered to public, which is the default (`create-trip.md`,
`trip-settings.md`). The only ways into a private trip are a legacy rider-link claim (`legacy-link.md`) or a leader
making the trip public.

## APIs called
- `GET /api/v2/trips/{tripId}` (`viewer.role`).
- `POST /api/v2/trips/{tripId}/join-requests` (`JoinRequestCreate`: `message?`, trimmed, ≤ 280, empty → `null`;
  → `MyJoinRequestOut`): `201` new, `200` existing pending, `401`, `404`, `409`, `422`, `429`.
- `POST /api/v2/join-requests/{requestId}/cancel` (→ `MyJoinRequestOut`): `200` (also when already cancelled),
  `401`, `404`, `409`, `429`.
- `GET /api/v2/me/join-requests` (`MyJoinRequestOut[]`, newest first): the requester's own request for this trip.

## Handoff checklist
- [ ] The counter blocks input past 280 characters (or shows an error) and is announced politely.
- [ ] The pending state shows on both `/join` and the trip header; Add stop is absent while pending.
- [ ] Cancel needs no confirmation and the form is usable immediately after.
- [ ] After a `409` the envelope message is shown verbatim and the Send button is hidden; the screen never shows the
  word "Blocked" or a cooldown date of its own.- [ ] Only the display name is mentioned as visible to leaders; the username is never shown here.
