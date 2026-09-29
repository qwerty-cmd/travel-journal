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
| Sign in to send | Queue paused on 401 | "Sign in to send 3 items" / detail "They're saved on this phone." | "Sign in" → `/signin?next=` |
| Held for another account | Entries whose `userId` ≠ the current user | "2 items were saved by another account on this phone" / "Sign in as that account to send them." | none |
| Waiting | Non-failed entries | "Waiting to send: 1 stop, 2 photos" (existing wording) | "Details" disclosure: per item "Waiting" or "Still trying: <lastError>" (≥ 10 attempts; existing rule) |
| Failed | Failed entries | per item: "<label> couldn't be sent" + `lastError` | Photo: "Save photo to this device" + "Dismiss"; stop: "Dismiss". An unsaved photo's Dismiss confirms |
| Access revoked (a subset of Failed) | Failed with the revoked message | Group title "Your rider access was removed" + "These items were refused. Save any photos you want to keep." | as Failed |

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
| Session ended (401 on a page that needs sign-in) | `user` | "Please sign in" | "Your session has ended. Sign in to continue." | "Sign in" |
| Forbidden (403, e.g. a leader page after demotion) | `lock` | "You can't do this here" | envelope message, or "You're not a leader on this trip." | "Back to trip" |
| Rate limited on a page load | `clock` | "Too many requests" | "Try again in N minutes." | "Try again" (disabled until then) |

## APIs called
None of their own. The queue reads IndexedDB (`btj-queue`); the page states render from whichever query failed.

## Handoff checklist
- [ ] With an empty queue and online, the status stack has zero height.
- [ ] A 401 during drain turns items into "Sign in to send N items", and they're not failed (still in the Waiting count after signing in, then sent).
- [ ] The revoked group shows "Save photo to this device" on photo rows, and it downloads a JPEG with the stop name in the filename.
- [ ] The held-for-another-account notice never shows a username.
- [ ] The "Trip not found" text is identical for unknown and private trips (per sign-in state).
- [ ] While 10 items drain, the summary live region announces at most once every 5 s (debounced), then "All sent" once the stack empties.
