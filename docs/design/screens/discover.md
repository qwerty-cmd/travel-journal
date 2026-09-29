# Screen: Discover (`/`)

Access model: decision-log Entry 29 (ADR §2 anonymous browsing). Tokens and components: `docs/design/DESIGN.md`.
Mockup: `docs/design/mockups/discover.html`. The signed-in "Your trips" section is specified in `rider-home.md`.

## Design feature
The front door. Anyone, signed in or not, can browse public trips, most recently updated first, and open one. It
replaces today's paste-link screen. The paste field survives as the secondary "Have an old trip link?" route into
legacy trips (Entry 18's iOS first-launch case). It teaches the access model in one line and offers sign in /
create account without pushing followers into them.

## Design format
Frame: phone 390 wide, vertical auto layout, page padding 16, gap `space/6`, `surface/page`.

1. **TopBar** (fixed height 56, fill width): wordmark "Bike Trip Journal" (heading, `brand/primary`, left). Right:
   "Sign in" tertiary button (signed out), or the Account IconButton (`user`, label "Account").
2. **Status stack** (hug, fill width): global notices (DESIGN.md §5.6); renders nothing when there's nothing to say.
3. **Onboarding card** (signed out, and not dismissed on this device): Card, padding 16, gap 12. Title "Follow a
   bike trip", the verbatim ADR line, "Pick a trip below to follow it. No account needed.", then the iOS line
   (iOS Safari non-standalone only, DESIGN.md §10.3). Action row (horizontal, gap 8, wraps): "Create an account"
   (secondary md) · "Sign in" (tertiary). Dismiss IconButton `x` "Hide this introduction", top-right.
4. **Your trips** (signed in only): see `rider-home.md`. Placed above Public trips.
5. **Section header** "Public trips" (H2) + intro "Public trips, most recently updated first." (small, muted).
   Signed-in users also get a secondary md "Create a trip" button (`plus`) at the right of the header; it wraps
   below on narrow widths.
6. **Trip list**: vertical auto layout, gap `space/4`, one `Card/trip` per item (DESIGN.md §5.5): name + `Public`
   badge; "Started 12 Oct 2026 · 3 riders"; "Last public stop 2 days ago" (relative, absolute in `title`) or "No
   public stops yet".
7. **Load more**: secondary md, full width, "Show more trips". Cursor paging (`limit` 20). Button loading state
   while fetching; appended items receive focus on the first new card's link (announced "20 more trips loaded").
8. **Footer link row**: "Have an old trip link?" tertiary → expands inline a TextField "Paste your old trip link" +
   "Open trip" (secondary md). Parsing and error copy as today: "That doesn't look like a trip link." Routes to
   `/t/$slug`.

≥ 768: trip list becomes a 2-column grid (gap 16), onboarding card max 720 centred, `font/size/display` for a
"Bike Trip Journal" hero H1 above the onboarding copy. ≥ 1024: 3-column grid, content max 1200.

H1 on phones is visually the wordmark; give the page a visually hidden H1 "Discover bike trips".

## States
| State | Presentation |
|---|---|
| Loading | 3 trip-card skeletons |
| Cold start (> 3 s) | + info notice "Waking up the server…" |
| Empty (no public trips) | EmptyState, icon `globe`: "No public trips yet" / "When a trip is made public, it appears here." Signed in: action "Create a trip" |
| Error, envelope | Danger notice with envelope message + "Try again" |
| Error, no response | "Can't reach the server. Check your signal and try again." + "Try again". Your trips (if cached) still render |
| Offline | Offline notice; list error state as above; the paste field still works (it only navigates) |
| Load-more error | Inline danger text under the list "Couldn't load more trips" + "Try again"; loaded cards stay |
| Rate limited (429) | Warning notice "Too many requests. Try again in N minutes." |
| Signed out vs in | Onboarding card vs Your trips + Create a trip |
| Queue non-empty | Status stack shows it here too (root shell) |

## APIs called
- `GET /api/v2/trips?cursor=&limit=20` (ADR §2): public list, delay-applied `lastPublicStopAt`.
- Signed in: `TBC: GET /api/v2/auth/me` (session check, DESIGN.md §13 C1) and `TBC: GET /api/v2/me/trips` (C3).
- None for the paste field.

## Handoff checklist
- [ ] Anonymous: the onboarding card contains the exact ADR sentence; no Create-a-trip button.
- [ ] Cards are single links (one Tab stop per card) with ≥ 48 px height; badge text "Public" present.
- [ ] No username, user id or slug appears in any card.
- [ ] "Show more trips" appends and moves focus to the first new card.
- [ ] The paste field shows "That doesn't look like a trip link." for invalid input and navigates to `/t/<code>` for valid.
- [ ] No search, filter or sort controls exist.
- [ ] The page never auto-redirects (DESIGN.md D7).
