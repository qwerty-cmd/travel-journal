# Screen: Leader review of join requests (`/trips/$tripId/members?view=requests`)

Access model: decision-log Entry 29 and `docs/api-contract.md` ("Join request create", "Trip join-requests
(leader)", "Decision", "Unblock"). Mockup: `docs/design/mockups/leader-review.html`.

Route name: `ba`'s `t-am-fe-leader-review` AC names the leader area `/trips/$tripId/manage`. This spec's
`/members?view=…` and `/settings` are the same screens; the router path is `ba`/`dev`'s call (DESIGN.md §13, X1).

## Design feature
Leaders decide who can write. The list must be fast to work through on a phone (groups arrive together, hence bulk
approve), but careful: each request is one person, shown by display name and message, and a block is a deliberate,
confirmed action.

## Design format
Frame: phone 390. It's inside the trip shell (header + tabs, Members active).

1. **Segmented control** (Tabs `segmented`): "Requests 3" · "Members" · "Blocked".
2. Intro (small, muted): "Approve people you know are riding. Approved riders can add stops and photos, and see the
   trip live."
3. **Bulk bar** (appears when ≥ 1 selected; sticks to the top of the list region, not the viewport, so it never
   covers focus): "2 selected" · primary md "Approve 2" · secondary md "Reject 2" · tertiary "Clear". A "Select
   all" checkbox sits at the list head whenever there are ≥ 2 requests. There is no bulk endpoint: the UI sends one
   decision per selected request, in sequence, and reports each outcome.
4. **Request list**: vertical, gap 12, `Card/request` (DESIGN.md §5.5), oldest first:
   - Checkbox "Select <name>" · `requester.displayName` (body 600) · "Requested 2 hours ago" (abs time in `title`).
   - Message block (if any), quoted.
   - `legacy` badge "Via old rider link" when `via = legacy_rider_link`.
   - Actions (horizontal, gap 8, wraps): primary md "Approve" · secondary md "Reject" · IconButton "More options for
     <name>" → menu item "Reject and block" (danger text).
5. After an action, the row collapses and a Toast echoes it ("Approved Sam", "Rejected Alex"). Focus moves to the
   next row's Approve button (or to the list's heading if none remain).
6. **Blocked** view: list of blocked requests (display name, the original message if any) with secondary md
   "Unblock" (no confirm). Unblocking turns the request into a rejection that keeps its original date, so the
   7-day cooldown still runs from the original decision. Toast: "Unblocked Jo. They can ask to join again once 7 days
   have passed since the original decision."

## States
| State | Presentation |
|---|---|
| Loading | 3 request-card skeletons |
| Empty | EmptyState `users`: "No requests right now" / "When someone asks to join, they'll appear here. Share the trip link with your riders." + secondary "Copy trip link" (copies `https://<origin>/trips/<id>`). Private trip: the body instead reads "Nobody can ask to join while the trip is private. Make it public in Trip settings to let riders ask." and the action is "Trip settings" (C13) |
| Blocked empty | "Nobody is blocked." |
| Approve/reject in flight | That row's buttons show loading; others stay usable |
| Bulk in flight | Bulk bar buttons loading; the selected rows get `aria-busy` |
| Bulk partial failure | Danger notice "Approved 2 of 3. Couldn't approve Jo: <envelope message>." Failed rows stay selected |
| Same decision repeated (another leader already did it, `200`) | Treated as success; the row collapses |
| Different decision already made (`409`) or request gone (`404`) | The row shows "Already handled" (muted) with the envelope message and is removed on the next refetch |
| Unblock on a request that isn't blocked (`409`) | Same "Already handled" treatment in the Blocked view |
| Reject and block | ConfirmDialog destructive (DESIGN.md §8.3) |
| 403 (no longer a leader) | Danger notice with the envelope message, or "You're no longer a leader on this trip." Tab content reduces to the rider view after the refetch |
| 401 | "Your session has ended. Sign in to continue." + Sign in |
| Offline | Offline notice; "Reviewing requests needs a connection." Actions disabled with that reason |
| Pending cap reached (list length = 100) | Info notice at the top: "This trip has 100 requests waiting. New requests are refused until you review some." |
| 429 | Warning notice "Too many requests. Try again in N seconds." (bulk: the loop stops, reports what was done, and the remaining rows stay selected) |

## APIs called
- `GET /api/v2/trips/{tripId}/join-requests` (`TripJoinRequestOut[]`; `state=pending` default, `state=blocked` for the
  Blocked view): `requester {userId, displayName}`, `message`, `via`. `200`, `401`, `403`, `404`, `422`, `429`.
- `POST /api/v2/trips/{tripId}/join-requests/{requestId}/decision` (`JoinDecisionCreate`: `action` = `approve` |
  `reject` | `reject_and_block`): `200`, `401`, `403`, `404`, `409`, `422`, `429`. Bulk = a loop over this.
- `POST /api/v2/trips/{tripId}/join-requests/{requestId}/unblock`: `200`, `401`, `403`, `404`, `409`, `429`.

`requester.userId` is used only as a React key and in the request path; it is never rendered, nor put in a `data-*`
attribute. Usernames never reach this screen.

## Handoff checklist
- [ ] Only leaders can reach this view (riders see the read-only Members view; others have no Members tab; a direct
  URL shows the trip Map tab).
- [ ] Each row has Approve, Reject and a More menu with Reject and block; block asks for confirmation, reject doesn't.
- [ ] Bulk approve of 3 sends 3 decision requests and reports each result.
- [ ] The bulk bar shows the selected count and never covers a focused checkbox.
- [ ] After an action, focus lands on the next row's Approve.
- [ ] Legacy-link requests show "Via old rider link".
- [ ] No username or user id appears in the DOM (check `data-*` attributes too).
- [ ] The Unblock toast mentions the 7-day wait from the original decision.
