# Screen: Sign up and recovery code (`/signup`)

Access model: decision-log Entry 29 (ADR §3: username private, displayName public, password 15–128, recovery code
shown once; §10 signup rate limits). Mockup: `docs/design/mockups/signup-recovery.html`.

## Design feature
Create an account in two steps: (1) the details, then (2) the recovery code, shown **once**. The code is the only
way back into the account without the operator, so step 2 makes saving it deliberate but not burdensome.

## Design format — step 1 "Create an account"
Frame: phone 390, form column max 480, gap 16.

1. TopBar: back.
2. H1 "Create an account".
3. Intro (body): "An account lets you add stops and photos to a trip once a leader approves you. You don't need
   one to follow public trips."
4. iOS notice (iOS Safari non-standalone only): DESIGN.md §10.3.
5. Error summary slot.
6. TextField "Display name" (`autocomplete="nickname"`). Helper: "Shown on photos you add and to trip leaders."
7. TextField "Username" (`username`, no autocapitalise). Helper: "Private. Only used to sign in." Rules come from
   the server (C12) and its messages are shown on this field.
8. PasswordField "Password" (`new-password`). Helper: "15 to 128 characters. A short sentence you'll remember works
   well." Live hint "7 more characters needed" / "18 characters".
9. Primary lg "Create account". Loading: "Creating account…".
10. Link "Already have an account? Sign in".

## Design format — step 2 "Save your recovery code"
Same route, replaced content (no navigation, so Back can't re-show the code; the code lives only in component
memory).

1. Success notice (`success`, `check-circle`): "Account created. You're signed in as <display name>."
2. H1 "Save your recovery code".
3. Body: "If you forget your password, this code is the only way back in. We can't show it again."
4. **RecoveryCodeBlock** (DESIGN.md §5.13): the code in mono groups of 4; "Copy code" (secondary md) · "Download
   as text file" (tertiary; file `bike-trip-journal-recovery-code.txt` containing the display name, **not** the
   username, the code, and one line of instructions).
5. Tips (small, muted list): "Save it in your password manager." · "Or write it down and keep it with your
   licence."
6. Checkbox "I've saved my recovery code" (48 px hit area).
7. Primary lg "Continue" (disabled until the checkbox is ticked; helper above it while disabled: "Tick the box
   once you've saved the code."). → `next` or `/`.

## States
| State | Presentation |
|---|---|
| Field errors (client) | "Enter a display name." · "Enter a username." · "Use at least 15 characters." / "Use 128 characters or fewer." |
| Username taken (409 / envelope) | Username field error with the server message (e.g. "That username is taken.") |
| Validation (422) | Server `details` mapped to fields; otherwise in the summary |
| Rate limited (429) | Warning notice "Too many new accounts from this network. Try again in N minutes." + submit disabled |
| Offline | Danger notice "You need a connection to create an account." |
| Cold start | Button loading + "Waking up the server…" after 3 s |
| Step 2: copy failed (clipboard blocked) | Inline "Couldn't copy. Select the code and copy it, or download it." |
| Step 2: user tries to leave (browser back or closes tab) | `beforeunload` prompt while the checkbox is unticked (browser-native text) |
| Already signed in | Redirect to `/account` |

## APIs called
- `TBC: POST /api/v2/auth/signup` (DESIGN.md §13 C1): `{displayName, username, password}` → session cookie +
  recovery code in the response body, shown once and never stored (not localStorage, not IndexedDB, not the query
  cache: use a mutation result held in component state, with `gcTime: 0`).

## Handoff checklist
- [ ] The recovery code isn't in localStorage, sessionStorage, IndexedDB, the URL or the TanStack Query cache after step 2.
- [ ] Reloading during step 2 doesn't re-show the code (lands on `/` signed in).
- [ ] Copy puts the exact code (without group spaces) on the clipboard; download yields a `.txt` with the code and no username.
- [ ] "Continue" stays disabled until the checkbox is ticked, with the reason shown.
- [ ] The password hint counts characters live; no strength meter, no composition rules.
- [ ] Display name and username helpers explain public vs private.
