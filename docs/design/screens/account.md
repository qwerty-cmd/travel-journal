# Screen: Account (`/account`)

Access model: decision-log Entry 29 and `docs/api-contract.md` ("Sessions", "Rate limits and lockout", v2 auth rows).

## Design feature
The signed-in user's own settings: who they are (display name public, username private), password change, a new
recovery code, sign out, and sign out everywhere. Also shows their trips and join requests in one place (the same
data as "Your trips").

## Design format
Frame: phone 390, column max 480, gap `space/6`.

1. TopBar: back, title "Account".
2. H1 "Account".
3. **Identity card** (Card, gap 8): "Display name" label + value (body 600) + "Shown on photos you add." (small,
   muted); "Username" label + value + "Private. Only you see this." There is **no** edit control for the display
   name: editing is filed debt (`t-am-display-name-edit`), so the design shows none.
4. **Your trips** (H2): compact list of `Card/trip` with role badges (Leader / Rider), from `GET /api/v2/me/trips`.
5. **My requests** (H2): rows from `GET /api/v2/me/join-requests` (newest first, at most 100): trip name + state
   Badge + "Requested 2 days ago". States shown: `pending` (Pending badge + tertiary "Cancel request"), `approved`
   ("Approved", `check-circle`), `rejected` ("Not approved", neutral muted text + `x` icon), `cancelled`
   ("Cancelled", muted). The server reports `blocked` as `rejected`, so the UI has no Blocked state for the requester.
   Hide the section when the list is empty.
6. **Password** (H2): Card with "Change password": PasswordField "Current password", PasswordField "New password"
   (15–128 guidance), primary md "Change password". Helper: "Changing your password signs you out on all other
   devices."
7. **Recovery code** (H2): body "Your recovery code was shown when you created your account. It can't be shown
   again, but you can replace it with a new one. The old one stops working." + secondary md "Get a new recovery
   code". This opens a Dialog: PasswordField "Your password" (`current-password`) + primary "Get new code". On
   success the dialog content is replaced by the RecoveryCodeBlock step (identical component and rules to
   `signup.md` step 2: title "Save your new recovery code", Copy, Download, the required "I've saved my recovery
   code" checkbox, then "Done"). The dialog can't be closed by Escape or the scrim while the checkbox is unticked;
   an explicit "Close without saving" tertiary asks a ConfirmDialog ("Your new code won't be shown again.").
8. **Sign out** (H2): secondary md "Sign out"; `danger-secondary` md "Sign out everywhere".
9. **This phone** (H2), shown when the queue has entries: "3 items waiting to send from this phone." Signing out
   keeps them: notice (small) "Items waiting to send stay on this phone and send when you sign in again."

## States
| State | Presentation |
|---|---|
| Loading | Card skeletons |
| 401 on `me` (session ended) | Redirect to `/signin?next=/account` |
| Change password: wrong current (`403 FORBIDDEN`) | Field error on "Current password" with the envelope message. **Not** a sign-out: a 403 here means the password was wrong, and the session is still valid. It counts toward the account lockout |
| Change password: same as current (`422`) | Field error on "New password" with the envelope message |
| Change password: success (`200` + new cookie) | Success notice inline "Password changed. Other devices have been signed out." The contract revokes every session and issues a fresh one to this device, so the user stays signed in |
| New recovery code: wrong password (`403`) | Field error in the dialog with the envelope message |
| New recovery code: success | Code shown once in the dialog (see Design format 7) |
| `429` on password change or new code | Warning notice with the envelope message + "Try again in N minutes." (`signin` bucket and account lockout; submit disabled until then) |
| Cancel request: `409` (already decided) | Inline muted text on the row with the envelope message; the list refetches |
| Cancel request: `404` | The row is removed on refetch (the request is gone) |
| Sign out | No confirm; `POST /api/v2/auth/signout` always answers 204. Clear `btj.me`, broadcast `auth`, → `/` with toast "Signed out" |
| Sign out everywhere | ConfirmDialog neutral: "Sign out on every device?" / "Including this one. Anything waiting to send stays on each phone until someone signs in there." / "Sign out everywhere". Then as Sign out |
| Offline | Offline notice; identity renders the display name from `btj.me` (the username is not cached, so its row reads "Needs a connection"); actions show "You need a connection to manage your account." |

## APIs called
- `GET /api/v2/auth/me` (`MeOut`: `id`, `username`, `displayName`, `createdAt`). The only response with a username.
- `POST /api/v2/auth/password` (`PasswordChange`: `currentPassword`, `newPassword` → `MeOut` + Set-Cookie):
  `200`, `401`, `403`, `422`, `429`.
- `POST /api/v2/auth/recovery-code` (`RecoveryCodeCreate`: `password` → `RecoveryCodeIssuedOut`): `200`, `401`,
  `403`, `422`, `429`.
- `POST /api/v2/auth/signout` (`204` always) and `POST /api/v2/auth/signout-all` (`204`, `401`, `429`).
- `GET /api/v2/me/trips` (`MyTripOut[]`) and `GET /api/v2/me/join-requests` (`MyJoinRequestOut[]`).
- `POST /api/v2/join-requests/{requestId}/cancel` (`200`, `401`, `404`, `409`, `429`), as in `join-request.md`.

## Handoff checklist
- [ ] The username appears only on this screen (and the sign-in form), and is not in `btj.me`.
- [ ] A wrong current password (403) shows a field error and leaves the user signed in.
- [ ] "Get a new recovery code" asks for the password, shows the code once and requires the checkbox before "Done".
- [ ] A blocked request shows as "Not approved", never as "Blocked".
- [ ] Sign out everywhere asks for confirmation; plain Sign out doesn't.
- [ ] Signing out doesn't clear the offline queue (check IndexedDB `btj-queue` before and after), and clears `btj.me`.
- [ ] No "Delete account", avatar, email or display-name edit controls exist.
