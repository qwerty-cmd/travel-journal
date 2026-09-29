# Global states (every route)

Access model: decision-log Entry 29 (new codes 401 UNAUTHENTICATED, 429 RATE_LIMITED; revocation vs the queue).
Queue: Entry 19. Standard wording: `docs/design/DESIGN.md` §7. Components: §5.6 (StatusNotice), §5.10–5.12.

## Design feature
The shared shell pieces that make state visible everywhere: the status stack under the top bar (offline, sign in to
send, held for another account, waiting, failed), and the full-page states (loading, waking up, error, not found,
no trips). The root route mounts the status stack, as it mounts `QueueNotice` today, so it shows on every screen.

## Design format — status stack
Vertical auto layout, gap 0, fill width, directly under the TopBar, not sticky. Order: Offline → Sign in to send →
Held for another account → Waiting (collapsible) → Failed (expanded). Each is a StatusNotice (DESIGN.md §5.6). When
empty it renders nothing (zero height).

| Notice | When | Copy | Actions |
|---|---|---|---|
| Offline | Last request had no response, or `navigator.onLine` false (hint only) | "You're offline. New stops are saved on this phone and sent later." | none |
| Sign in to send | The drain is paused on `401 UNAUTHENTICATED` (the entry is not failed and the attempt is not counted) | "Sign in to send 3 items" / detail "They're saved on this phone." | "Sign in" → `/signin?next=`. The drain resumes on the `auth` broadcast after sign-in and on the usual triggers. Pause survives a reload |
| Held for another account | Entries whose `userId` ≠ the signed-in user, or that have a `userId` while nobody is signed in (held: neither sent nor failed). Pre-upgrade entries without a `userId` are never held | "2 items were saved by another account on this phone" / "Sign in as that account to send them." (signed out: "Sign in to send them.") | none |
| Waiting | Non-failed, non-held entries, including those waiting out a `429 Retry-After` (counted as an attempt, no separate notice) | "Waiting to send: 1 stop, 2 photos" (existing wording) | "Details" disclosure: per item "Waiting" or "Still trying: <lastError>" (≥ 10 attempts; existing rule) |
| Failed | Never-retry codes: `VALIDATION_ERROR`, `METHOD_NOT_ALLOWED`, `CONFLICT`, `FORBIDDEN`, `NOT_FOUND`. Kept in IndexedDB with the blob until dismissed | per item: "<label> couldn't be sent" + `lastError` (the envelope message, e.g. a photo over 15 MiB or not a JPEG) | Photo: "Save photo to this device" + "Dismiss"; stop: "Dismiss". An unsaved photo's Dismiss confirms |
| Access revoked (a subset of Failed) | Failed with `FORBIDDEN` "You're no longer a rider on this trip" | Group title "Your rider access was removed" + "These items were refused. Save any photos you want to keep." | as Failed |

Accessibility: the stack is a `<section aria-label="Unsent stops and connection status">`. Count changes are
announced politely: one live region for the stack's summary line, not per item, debounced to at most one
announcement every 5 s while draining, then "All sent" once when the queue empties. ("All sent" is a visually
hidden announcement only. Visually the stack simply disappears.)

## Design format — full-page states
Centred EmptyState (DESIGN.md §5.10) inside `<main>`, the TopBar still present.

| State | Icon | Title | Body | Action |
|---|---|---|---|---|
| Loading | n/a | Skeleton | | |
| Waking up (> 3 s) | `clock` | "Waking up the server…" | "The first visit after a quiet spell can take a little while." | none (auto) |
| Can't reach the server | `cloud-off` | "Can't reach the server" | "Check your signal and try again." | "Try again" |
| Error with envelope | `alert-triangle` | "Something went wrong" | the envelope message | "Try again" |
| Trip not found (unknown **or** private) | `map-pin` | "Trip not found" | signed out: "Check the link. If this is a private trip, sign in with an account that belongs to it." · signed in: "Check the link. If this is a private trip, you need to be a member to see it." | "Go to Discover" (+ "Sign in" when signed out) |
| Page not found (unknown route) | `map-pin` | "Page not found" | "That address doesn't exist in this app." | "Go to Discover" |
| No trips anywhere (Discover empty and no member trips) | `globe` | "No public trips yet" | "When a trip is made public, it appears here." | signed in: "Create a trip" |
| Session ended (401 on a page that needs sign-in: `/account`, leader pages, `/me/*`) | `user` | "Please sign in" | "Your session has ended. Sign in to continue." | "Sign in" |
| Forbidden (403, e.g. a leader page after stepping down, or the members list after revocation) | `lock` | "You can't do this here" | envelope message, or "You're not a leader on this trip." | "Back to trip" |
| Rate limited on a page load (`429`, `public-read` 120/min) | `clock` | "Too many requests" | "Try again in N seconds." from `Retry-After` (DESIGN.md §5.6 rounding) | "Try again" (disabled until then) |

Trip reads never produce the 401 page: the v2 reader gate answers only `200` or `404`. A `403` with the CSRF message
("This request came from another site and was blocked.") is shown as an ordinary envelope error; it shouldn't occur
from the app itself.

## APIs called
None of their own. The queue reads IndexedDB (`btj-queue`) and classifies send results by the contract's
"Offline-queue classification (Entry 29)" table; the page states render from whichever query failed.

## Handoff checklist
- [ ] With an empty queue and online, the status stack has zero height.
- [ ] A 401 during drain turns items into "Sign in to send N items", and they're not failed (still in the Waiting count after signing in, then sent).
- [ ] The revoked group shows "Save photo to this device" on photo rows, and it downloads a JPEG with the stop name in the filename.
- [ ] The held-for-another-account notice never shows a username.
- [ ] A queue `429` with `Retry-After: 7` keeps the item under "Waiting" (no failed row, no extra notice).
- [ ] The "Trip not found" text is identical for unknown and private trips (per sign-in state).
- [ ] While 10 items drain, the summary live region announces at most once every 5 s (debounced), then "All sent" once the stack empties.
