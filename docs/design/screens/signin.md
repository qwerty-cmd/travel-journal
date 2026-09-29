# Screen: Sign in (`/signin`) and reset with recovery code (`/recover`)

Access model: decision-log Entry 29 (ADR §3 identity, §10 rate limits and lockout). iOS storage isolation: Entry 18.

## Design feature
Signing in with a username and password, the only way to get write access (plus membership). There is no email,
so a forgotten password is reset with the one-time recovery code shown at sign-up. A successful reset issues a
**new** code, which is shown once, exactly like sign-up. Sign-in needs a connection (ADR §3).

## Design format — `/signin`
Frame: phone 390, form column max 480, page padding 16, vertical auto layout gap 16.

1. TopBar: back arrow → previous page or `/`.
2. H1 "Sign in".
3. Context notice (only when arriving with `?next=` from a write or queue prompt), `info`: "Sign in to add stops
   and send anything waiting on this phone."
4. iOS notice (iOS Safari non-standalone only), `info`: the DESIGN.md §10.3 line.
5. Error summary (after a failed submit; `role="alert"`, focus moves here).
6. TextField "Username" (`autocomplete="username"`, `autocapitalize="none"`, `spellcheck="false"`).
7. PasswordField "Password" (`current-password`).
8. Primary lg "Sign in" (full width). Loading: "Signing in…".
9. Links (vertical, gap 8): "Forgot your password? Use your recovery code" → `/recover` (carries the typed
   username); "New here? Create an account" → `/signup` (carries `next`).

## Design format — `/recover`
1. H1 "Reset your password".
2. Body: "Enter your username and the recovery code you saved when you created your account."
3. TextField "Username" (pre-filled if carried).
4. TextField "Recovery code" (`autocomplete="one-time-code"`, `font/family/mono`, spaces ignored on input; the
   helper says "Spaces don't matter.").
5. PasswordField "New password" (`new-password`, 15–128 guidance).
6. Primary lg "Reset password".
7. On success, the **new recovery code** step (identical component and rules to `signup.md` step 2), with the
   title "Save your new recovery code", body "Your old code no longer works. You've been signed out on all other
   devices.", then "Continue" → `next` or `/`.
8. Help text at the bottom (small, muted): "Lost your recovery code too? Ask the person who runs this site to
   reset your account." (operator CLI, ADR §3). Leaders cannot reset accounts, and the copy doesn't suggest they can.

## States
| State | Presentation |
|---|---|
| Default | As above |
| Field empty on submit | Field errors: "Enter your username." / "Enter your password." |
| Wrong credentials (401 / envelope) | Error summary: "Username or password is incorrect." Never says which. Password field cleared; username kept |
| Account locked (server lockout) | Warning notice with the server message (e.g. "Too many failed attempts. Try again in 15 minutes."); submit disabled until then if the time is known |
| Rate limited (429) | Warning notice "Too many attempts. Try again in N minutes." (from `Retry-After`); submit disabled until then |
| Offline / no response | Danger notice "You need a connection to sign in. Anything you've added is still saved on this phone." |
| Cold start | Button loading + after 3 s the "Waking up the server…" notice |
| Recover: bad code | Summary with the server's message (e.g. "That username and recovery code don't match."). Nothing reveals whether the username exists |
| Recover: password too short | Field error "Use at least 15 characters." |
| Already signed in | Redirect to `next` or `/` |
| Success with queued items | Navigate to `next`; the status stack's "Sign in to send" notice disappears and the drain resumes (existing triggers) |

## APIs called
- `TBC: POST /api/v2/auth/login` (DESIGN.md §13 C1): sets the `__Host-btj_session` cookie; the UI never reads it.
- `POST /api/v2/auth/recover` (ADR §3): username, code, new password → new code; revokes all sessions.
- `TBC: GET /api/v2/auth/me` afterwards to refresh the signed-in state.

## Handoff checklist
- [ ] The wrong-password and unknown-username messages are identical.
- [ ] Fields have visible labels and the stated `autocomplete` values; paste works in every field.
- [ ] Show/hide password toggles without submitting; the field returns to hidden after submit.
- [ ] 429 shows minutes from `Retry-After` and disables submit until then.
- [ ] `?next=` accepts only same-origin paths starting with `/`; `?next=https://example.com` goes to `/`.
- [ ] Recover shows the new code once, with Copy and Download; leaving and returning doesn't show it again.
- [ ] No password or recovery code is ever placed in the URL.
