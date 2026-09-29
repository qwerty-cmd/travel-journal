# Screen: Account (`/account`)

Access model: decision-log Entry 29 (ADR §3 sessions and revocation).

## Design feature
The signed-in user's own settings: who they are (display name public, username private), password change,
sign out, and sign out everywhere. Also shows their trips and join requests in one place (from "Your trips").

## Design format
Frame: phone 390, column max 480, gap `space/6`.

1. TopBar: back, title "Account".
2. H1 "Account".
3. **Identity card** (Card, gap 8): "Display name" label + value (body 600) + "Shown on photos you add." (small,
   muted); "Username" label + value + "Private. Only you see this." Edit display name: tertiary "Change" → inline
   TextField + Save/Cancel **only if** the contract provides it (C1); otherwise not shown.
4. **Your trips** (H2): compact list of `Card/trip` with role badges (Leader / Rider / Pending) and the join
   requests section ("Pending requests": trip name + "Requested 2 days ago" + "Cancel request" tertiary). Same data
   as `rider-home.md`.
5. **Password** (H2): Card with "Change password": PasswordField "Current password", PasswordField "New
   password" (15–128 guidance), primary md "Change password". Helper: "Changing your password signs you out on all
   other devices."
6. **Recovery code** (H2): body "Your recovery code was shown when you created your account. If you've lost it,
   you can't view it again." (No regenerate control unless the contract adds one.)
7. **Sign out** (H2): secondary md "Sign out"; `danger-secondary` md "Sign out everywhere".
8. **This phone** (H2), shown when the queue has entries: "3 items waiting to send from this phone." Signing out
   keeps them: notice (small) "Items waiting to send stay on this phone and send when you sign in again."

## States
| State | Presentation |
|---|---|
| Loading | Card skeletons |
| 401 (session ended) | Redirect to `/signin?next=/account` |
| Change password: wrong current | Field error with the server message |
| Change password: success | Success notice inline "Password changed. Other devices have been signed out." |
| Sign out | No confirm; → `/` with toast "Signed out" |
| Sign out everywhere | ConfirmDialog neutral: "Sign out on every device?" / "Including this one. Anything waiting to send stays on each phone until someone signs in there." / "Sign out everywhere" |
| Offline | Offline notice; identity renders from the last `me` response if cached in memory, otherwise "You need a connection to manage your account." |
| 429 | Warning notice with minutes |

## APIs called
`TBC: GET /api/v2/auth/me`, `TBC: POST /api/v2/auth/password`, `TBC: POST /api/v2/auth/logout`,
`TBC: POST /api/v2/auth/logout-all`, `TBC: GET /api/v2/me/trips` (DESIGN.md §13 C1, C3); cancel request per
`join-request.md`.

## Handoff checklist
- [ ] The username appears only on this screen (and the sign-in form).
- [ ] Sign out everywhere asks for confirmation; plain Sign out doesn't.
- [ ] Signing out doesn't clear the offline queue (check IndexedDB `btj-queue` before and after).
- [ ] No "Delete account", avatar or email controls exist.
