# Screen: Members management (`/trips/$tripId/members?view=members`)

Access model: decision-log Entry 29 and `docs/api-contract.md` ("Members", `require_trip_member_read`). Route
naming: see `leader-review.md` (`ba`'s `/manage`, DESIGN.md §13 X1).

## Design feature
Leaders see who can write and manage it: promote a rider to leader, revoke a rider, step down, leave. Riders see the
same list read-only and can leave. The rules are fixed by the server: a leader can't remove or demote another
leader, and the last leader can't leave or step down. The UI simply doesn't offer impossible actions, and it
explains the last-leader case.

## Design format
Inside the trip shell, Members tab. Leaders see the segmented control with "Members" selected; riders see no
segmented control (Requests and Blocked are leader-only).

1. Intro (small, muted): "Leaders can approve requests and manage riders. Every leader has the same powers."
2. **Leaders** (H2, count "Leaders (2)"): `Card/member` rows (`displayName`, role badge, "Joined 3 Oct"). The viewer's
   own row reads "You" after the name and has "Step down" (`danger-secondary` md) and "Leave trip" (tertiary
   danger). Other leaders' rows have **no actions**. A muted line instead: "Leaders can't remove each other."
3. **Riders** (H2, "Riders (3)"): leader view: rows with actions secondary md "Make leader" · `danger-secondary` md
   "Revoke". Rider view: rows without actions, except the viewer's own row, which has "Leave trip" (tertiary danger).
4. Footer help (leader view only, small, muted): "Removed riders can ask to join again after 7 days. To stop that,
   block them from the Requests list when they ask."

## Interactions
- **Make leader:** ConfirmDialog neutral: "Make Sam a leader?" / "Sam will be able to approve requests, manage
  riders and change the trip's settings. Leaders can't remove each other, so Sam can only stop being a leader by
  stepping down." / "Make leader". (Confirmed because it can't be undone by *you*.) Promoting someone who is
  already a leader answers `200` and is treated as success.
- **Revoke:** ConfirmDialog destructive (DESIGN.md §8.3), confirm label "Remove Sam". After success (`204`, also
  when already revoked), the row moves out; toast "Removed Sam".
- **Step down / Leave:** ConfirmDialog destructive (§8.3). When the viewer is the only leader both buttons are
  disabled with helper text: "You're the only leader. Promote another rider to leader first." (the contract's
  `409` message, reused so the pre-check and the server agree). After stepping down: the leader-only controls
  disappear; toast "You're now a rider on this trip". After leaving: → `/`, toast "You left <trip>".
- **Leave** doesn't start the 7-day cooldown. The dialog's consequence list says so (§8.3).

## States
| State | Presentation |
|---|---|
| Loading | Row skeletons |
| Only you | Leaders (1) with you; Riders EmptyState "No riders yet" / "Approve requests to add riders." |
| 409 last leader (race: another leader stepped down meanwhile) | The dialog shows the envelope message ("Promote another rider to leader first"); nothing changes |
| 403 on revoking a leader (race: they were promoted meanwhile) | Danger notice with the envelope message ("Leaders can't remove another leader"); the list refetches |
| 404 on promote or revoke (the target is no longer an active rider) | Danger notice with the envelope message; the list refetches |
| 403 on the list (revoked member) | The tab disappears on the trip refetch; full-page Forbidden state only if reached by URL |
| Offline | "Managing members needs a connection." Actions disabled with that reason |
| 401 / 429 | Standard (DESIGN.md §7) |

## APIs called
- `GET /api/v2/trips/{tripId}/members` (`MemberOut[]`, active members in `joinedAt` order; any active member):
  `200`, `401`, `403`, `404`, `429`.
- `POST /api/v2/trips/{tripId}/members/{userId}/promote` (`MemberOut`): `200`, `401`, `403`, `404`, `429`.
- `DELETE /api/v2/trips/{tripId}/members/{userId}`: `204`, `401`, `403`, `404`, `429`.
- `POST /api/v2/trips/{tripId}/step-down` (`MemberOut`): `200`, `401`, `403`, `404`, `409`, `429`.
- `POST /api/v2/trips/{tripId}/leave`: `204`, `401`, `403`, `404`, `409`, `429`.

`userId` is used only in request paths and as a React key; it is never rendered.

## Handoff checklist
- [ ] Other leaders' rows have no action buttons at all.
- [ ] Sole leader: Step down and Leave are disabled and the reason is visible text.
- [ ] Revoke and Make leader both confirm; Revoke's dialog lists the queued-items consequence.
- [ ] Revoke's initial focus is on Cancel.
- [ ] A rider sees the list with no Make leader / Revoke controls, and a Leave trip button on their own row only.
- [ ] The list shows display names only.
