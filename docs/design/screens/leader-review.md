# Screen: Leader review of join requests (`/trips/$tripId/members?view=requests`)

Access model: decision-log Entry 29 (ADR §5: approve / reject / reject and block / bulk; 100 pending per trip;
legacy-link `via`). Mockup: `docs/design/mockups/leader-review.html`.

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
   all" checkbox sits at the list head whenever there are ≥ 2 requests.
4. **Request list**: vertical, gap 12, `Card/request` (DESIGN.md §5.5), oldest first:
   - Checkbox "Select <name>" · display name (body 600) · "Requested 2 hours ago" (abs time in `title`).
   - Message block (if any), quoted.
   - `legacy` badge "Via old rider link" when `via=legacy_rider_link`.
   - Actions (horizontal, gap 8, wraps): primary md "Approve" · secondary md "Reject" · IconButton "More options for
     <name>" → menu item "Reject and block" (danger text).
5. After an action, the row collapses and a Toast echoes it ("Approved Sam", "Rejected Alex"). Focus moves to the
   next row's Approve button (or to the list's heading if none remain).
6. **Blocked** view: list of blocked people (display name, "Blocked 3 Oct by <leader display name>" if provided) with
   secondary md "Unblock" (no confirm; toast "Unblocked Jo. They can ask to join again.").

## States
| State | Presentation |
|---|---|
| Loading | 3 request-card skeletons |
| Empty | EmptyState `users`: "No requests right now" / "When someone asks to join, they'll appear here. Share the trip link with your riders." + secondary "Copy trip link" |
| Blocked empty | "Nobody is blocked." |
| Approve/reject in flight | That row's buttons show loading; others stay usable |
| Bulk in flight | Bulk bar buttons loading; the selected rows get `aria-busy` |
| Bulk partial failure | Danger notice "Approved 2 of 3. Couldn't approve Jo: <message>." Failed rows stay selected |
| Request already handled by another leader (409/404) | The row updates to show "Already handled" (muted) and is removed on the next refetch |
| Reject and block | ConfirmDialog destructive (DESIGN.md §8.3) |
| 403 (no longer a leader) | Danger notice "You're no longer a leader on this trip." Tab disappears after the refetch |
| 401 | "Your session has ended. Sign in to continue." + Sign in |
| Offline | Offline notice; "Reviewing requests needs a connection." Actions disabled with that reason |
| Pending cap reached (100) | Info notice at the top: "This trip has 100 requests waiting. New requests are refused until you review some." |
| 429 | Warning notice with minutes |

## APIs called
`TBC: GET /api/v2/trips/{tripId}/join-requests?status=pending`, `TBC: POST …/join-requests/{requestId}/approve`,
`TBC: POST …/join-requests/{requestId}/reject` `{block: boolean}`, `TBC` bulk approve/reject,
`TBC: GET …/blocks`, `TBC: DELETE …/blocks/{userId}` (DESIGN.md §13 C6). Never shows usernames or user ids.

## Handoff checklist
- [ ] Only leaders can reach this view (others: the Members tab is absent; a direct URL shows the trip Map tab).
- [ ] Each row has Approve, Reject and a More menu with Reject and block; block asks for confirmation, reject doesn't.
- [ ] The bulk bar shows the selected count and never covers a focused checkbox.
- [ ] After an action, focus lands on the next row's Approve.
- [ ] Legacy-link requests show "Via old rider link".
- [ ] No username or user id appears in the DOM (check `data-*` attributes too).
