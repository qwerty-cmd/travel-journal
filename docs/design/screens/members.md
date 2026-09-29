# Screen: Members management (`/trips/$tripId/members?view=members`)

Access model: decision-log Entry 29 (ADR §6 peer leaders; §8 revocation and the offline queue).

## Design feature
Leaders see who can write and manage it: promote a rider to leader, revoke a rider, step down, leave. The rules are
fixed by the server: a leader can't remove or demote another leader, and the last leader can't leave or step down.
The UI simply doesn't offer impossible actions, and it explains the last-leader case.

## Design format
Inside the trip shell, Members tab, segmented "Members" selected.

1. Intro (small, muted): "Leaders can approve requests and manage riders. Every leader has the same powers."
2. **Leaders** (H2, count "Leaders (2)"): `Card/member` rows. The viewer's own row reads "You" after the name and
   has "Step down" (`danger-secondary` md) and "Leave trip" (tertiary danger). Other leaders' rows have **no
   actions**. A muted line instead: "Leaders can't remove each other."
3. **Riders** (H2, "Riders (3)"): rows with actions: secondary md "Make leader" · `danger-secondary` md "Revoke".
4. Footer help (small, muted): "Removed riders can ask to join again after 7 days. To stop that, block them from the
   Requests list when they ask."

Riders (non-leaders) don't see this tab. Their own "Leave trip" lives in the trip settings overflow for members
(TBC contract, C6); until the contract exists it isn't shown.

## Interactions
- **Make leader:** ConfirmDialog neutral: "Make Sam a leader?" / "Sam will be able to approve requests, manage
  riders and change the trip's settings. Leaders can't remove each other, so Sam can only stop being a leader by
  stepping down." / "Make leader". (Confirmed because it can't be undone by *you*.)
- **Revoke:** ConfirmDialog destructive (DESIGN.md §8.3), confirm label "Remove Sam". After success, the row moves
  out; toast "Removed Sam".
- **Step down / Leave:** ConfirmDialog destructive (§8.3). When the viewer is the only leader both buttons are
  disabled with helper text: "You're the only leader. Make someone a leader first." After stepping down: the Members
  tab disappears; toast "You're now a rider on this trip". After leaving: → `/`, toast "You left <trip>".

## States
| State | Presentation |
|---|---|
| Loading | Row skeletons |
| Only you | Leaders (1) with you; Riders EmptyState "No riders yet" / "Approve requests to add riders." |
| 409 last leader (race) | The dialog shows the server message; nothing changes |
| 403 on revoking a leader (race: they were promoted meanwhile) | Danger notice with the envelope message; the list refetches |
| Offline | "Managing members needs a connection." Actions disabled with that reason |
| 401 / 429 | Standard (DESIGN.md §7) |

## APIs called
`TBC: GET /api/v2/trips/{tripId}/members`, `DELETE /api/v2/trips/{tripId}/members/{userId}` (ADR §8), `TBC`
promote, step down, leave (DESIGN.md §13 C6).

## Handoff checklist
- [ ] Other leaders' rows have no action buttons at all.
- [ ] Sole leader: Step down and Leave are disabled and the reason is visible text.
- [ ] Revoke and Make leader both confirm; Revoke's dialog lists the queued-items consequence.
- [ ] Revoke's initial focus is on Cancel.
- [ ] The list shows display names only.
