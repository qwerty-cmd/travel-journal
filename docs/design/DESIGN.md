# Bike Trip Journal: Design system and UX spec

> **Owner:** `designer`. **Status:** v1, reconciled with `docs/api-contract.md` 2026-09-29 (`t-am-design-spec`).
> **Scope:** the redesign that follows the accepted
> access-model ADR ("Public trips, local accounts, leader-gated membership", Architect, 2026-09-29, orchestrator
> ruling same day), recorded as **decision-log Entry 29**. Referred to below as **the ADR** (Entry 29) with its
> section numbers (ADR §1 … §15).
>
> **Stitch was not used.** This session had no `mcp__stitch__*` tools and no Stitch skills. Everything here was
> written from the code, the ADR, `docs/user-guide.md` and `docs/real-device-test-plan.md`. How to generate the
> screens with Stitch later: `docs/design/stitch/HANDOFF.md`.
>
> **Source of truth for `dev`:** this file (tokens, components, patterns, states) and `docs/design/screens/*.md`
> (per-screen layout). Mockups in `docs/design/mockups/` are illustrations only. Every API a spec calls is now a
> named endpoint in `docs/api-contract.md` (v2 Endpoints table); there are no `TBC` placeholders left. **Where this
> design and the contract disagree, the contract wins** (status codes, error messages, queue behaviour). How each
> earlier dependency was resolved: §13. Styling approach, tokens-to-CSS mapping and icon source: decision-log
> **Entry 30** (§14, Appendix A).

Contents: 1 Principles · 2 Audiences · 3 Information architecture · 4 Visual system · 5 Components ·
6 Accessibility · 7 Interaction states · 8 Patterns · 9 Privacy UX · 10 Content and onboarding copy ·
11 Rationale · 12 Not in scope · 13 Contract and behaviour dependencies (resolved) · 14 Decisions · 15 Handoff
checklist · Appendix A `tokens.css`

---

## 1. Product principles

1. **Outdoors first.** The rider is in bright sun, possibly wearing gloves, using one hand, with patchy signal.
   We favour high contrast, large targets (48 px), short labels and one primary action per screen over subtlety.
2. **Capture never waits for the network.** Add stop writes to the device queue and returns immediately
   (decision-log Entry 19). No design element in the capture flow may need a network round trip to render.
3. **Always say where things stand.** Every screen shows its real state: loading, waking up, offline, waiting to
   send, failed, signed out, pending. Nothing unsent is ever hidden, and nothing is dropped silently.
4. **Privacy by default, and in plain view.** Public viewers see the trip 24 hours late (ADR §1). The UI says so
   wherever a rider or leader might assume otherwise, and it shows a trip's Public or Private status on every
   trip surface.
5. **The server decides who can do what.** Rider, leader and viewer controls render from `viewer.role` in the
   server's `TripOut` (ADR §2). Nothing is inferred from device storage. The UI only hides things; the server enforces.
6. **Calm for followers.** Friends and family see a clean journal: map, timeline, photos, bikes. They never see
   write controls, member lists or anything about accounts other than a quiet way to sign in.
7. **Dependency-free.** System fonts, inline SVG icons, stock OpenStreetMap tiles, and plain CSS with custom
   properties; no CSS framework (decided, Entry 30; §14).

## 2. Audiences and mental model

| Audience | Who | What they think the app is | What they can do | What they must never see |
|---|---|---|---|---|
| **Public viewer** (anonymous) | Friends, family, anyone with a public trip link, on any device | "A travel diary of a bike trip, a day behind" | Browse Discover; open public trips; see map, timeline, photos and bikes, all subject to the public delay | Write controls, member or request lists, usernames, anything live |
| **Signed-in person, not a member** | A rider who has just made an account, or a follower who signed up | "I have an account; this trip isn't mine yet" | Everything a public viewer can do, plus request to join (one request per trip), cancel a request, create their own trip | Write controls on trips they don't belong to |
| **Pending requester** | Signed in, request sent | "Waiting for the organiser" | View as public; see "Request pending"; cancel it | Write controls |
| *(also `none`)* | Revoked, rejected, blocked or self-departed users | As a signed-in non-member | As a signed-in non-member; a new request is refused by the server with a `409` whose message is shown verbatim | A "blocked" label (the server reports blocked as rejected) |
| **Rider** (approved member) | On the bikes | "My trip's journal; I add to it" | Everything above, plus see the trip live (no delay); add stops and photos (queued offline); add and edit bikes (online only); see the member list; leave the trip | Member admin controls (promote, revoke, request review, settings) |
| **Leader** | Organiser; there can be several peer leaders (ADR §6) | "I run this trip and decide who writes" | Everything a rider can do, plus review requests (approve, reject, reject and block, bulk), promote riders, revoke riders, unblock, step down, publish or unpublish, set the public delay | Nothing else is hidden from leaders. They still can't remove or demote another leader, and the UI offers no such control |

**Mental model sentence** (shown in onboarding, §10): *Anyone can view public trips. Adding stops and photos
needs an account and a leader's approval.*

**Reading vs writing.** Reading needs no account for public trips. Writing needs three things: an account, an
approved membership, and a session on **this** device and app (the iOS Home Screen app keeps its own sign-in;
Entry 18). The UI names the missing one precisely: "Sign in", "Request to join", "Waiting for approval".

## 3. Information architecture and navigation

### 3.1 Route map

Routes marked **ADR** are named in ADR §2. Routes marked **proposed** are this design's proposal. `ba` confirms
them when scoping, and the router is `dev`'s implementation.

| Route | Name | Audience | Status | Screen spec |
|---|---|---|---|---|
| `/` | Discover (+ "Your trips" when signed in) | all | ADR | `screens/discover.md`, `screens/rider-home.md` |
| `/trips/$tripId` | Trip: **Map** tab (default) | all with read access | ADR | `screens/trip-detail.md` |
| `/trips/$tripId/timeline` | Trip: **Timeline** tab | all with read access | proposed | `screens/trip-detail.md` |
| `/trips/$tripId/bikes` | Trip: **Bikes** tab | all with read access | proposed | `screens/trip-detail.md` |
| `/trips/$tripId/members` | Trip: **Members** tab (leader: Requests · Members · Blocked; rider: read-only Members + Leave) | rider, leader | proposed (`ba`: `/manage`, see X1) | `screens/leader-review.md`, `screens/members.md` |
| `/trips/$tripId/settings` | Trip settings (name, visibility, public delay) | leader | proposed (`ba`: `/manage`, see X1) | `screens/trip-settings.md` |
| `/trips/$tripId/join` | Request to join, and pending state | signed-in non-member | proposed | `screens/join-request.md` |
| `/trips/$tripId/add` | Add stop | rider, leader | proposed | `screens/add-stop.md` |
| `/trips/$tripId/stops/$stopId` | Stop detail and gallery | all with read access | proposed | `screens/stop-detail.md` |
| `/trips/new` | Create trip | signed in | proposed | `screens/create-trip.md` |
| `/signin` | Sign in | signed out | ADR | `screens/signin.md` |
| `/recover` | Reset password with recovery code | signed out | proposed | `screens/signin.md` |
| `/signup` | Create account → recovery code step | signed out | ADR | `screens/signup.md` |
| `/account` | Account | signed in | ADR | `screens/account.md` |
| `/t/$slug` and children | Legacy link landing (old two-link trips) | anyone with an old link | ADR §11–12 (kept ≥ 120 days) | `screens/legacy-link.md` |
| (all) | Global states | all | n/a | `screens/global-states.md` |

`?next=` on `/signin` and `/signup`: after success, return to the page that sent the user there. The value must be
a same-origin path starting with `/` (never an absolute URL). Anything else falls back to `/`.

### 3.2 Navigation model

- **Top bar** (every screen, 56 px): left, a back button (inner screens) or the wordmark "Bike Trip Journal"
  (top-level). Right: "Sign in" text button (signed out) or an Account icon button labelled "Account" (signed in).
  There is no hamburger menu. The app has too few destinations to justify one.
- **Status stack** directly under the top bar: offline notice, queue notice, sign-in-to-send notice (§5.6). Not
  sticky, so it never covers focused content (WCAG 2.4.11).
- **Trip header** (inside a trip): trip name (H1), "Starts 12 Oct 2026", visibility Badge, the viewer's role
  Badge (Leader / Rider / Pending) when they have one, and a settings icon button for leaders.
- **Trip tabs** under the header: `Map · Timeline · Bikes`, plus `Members` for riders and leaders (leaders get a
  count Badge when requests are pending). Tabs are links to routes (so Back works and they can be deep-linked), styled as tabs,
  with `aria-current="page"` (§5.7).
- **Primary action bar** (riders and leaders on trip tabs): a bottom-docked full-width "Add stop" button, 56 px,
  above the safe-area inset. On ≥ 1024 px it moves into the trip header as an ordinary primary button.
- **≥ 1024 px:** Map and Timeline merge into one split view (map 7 columns, timeline 5), and the tab row reads
  `Journey · Bikes · Members`.

### 3.3 First-open and returning flows

| Situation | Lands on | Notes |
|---|---|---|
| First visit, no account | `/` Discover with the onboarding card (§10.1) | |
| Signed in, member of ≥ 1 trip | `/` with "Your trips" above Discover | One tap to the trip. The old auto-redirect to the last slug is **not** carried over (D7 decided; `ba` confirmed as C10) |
| Device holds a pre-upgrade `lastSlug` | `/` with a "Continue: last trip link" card → `/t/$slug` | `screens/rider-home.md` |
| Opened `/trips/$tripId` link | That trip | If private and the reader isn't a member: the 404 state (§7) |
| Old `/t/$slug` link | Legacy landing | `screens/legacy-link.md` |
| iOS Home Screen app, first launch | `/` signed out, even if signed in in Safari | Onboarding card shows the iOS line (§10.3) |

## 4. Visual system

Tokens are named like a Figma Variables collection (`group/subgroup/name`). **Entry 30 mapping:** in CSS each token
becomes a custom property by replacing every `/` with `-` and prefixing `--`: `color/brand/primary` →
`--color-brand-primary`, `font/size/body` → `--font-size-body`, `space/4` → `--space-4`. Every token in this section
has exactly one value, so the mapping is 1:1. They live in `frontend/src/styles/tokens.css` (paste-ready in
Appendix A); `base.css` holds element defaults; each component has its own `Component.css` with BEM-lite classes
(`.btn`, `.btn--primary`, `.btn__icon`). Breakpoints (`bp/*`) can't be custom properties inside `@media`, so they are
written as literal `min-width` values and documented in §4.4.

**Dark mode: out for v1.** Reasons: sunlight legibility is the priority, the brand colour and the OSM tiles are
light-native, and every state would need a second contrast audit. The tokens are semantic (`color/surface/*`,
`color/text/*`), so dark mode can be added later as a second mode of the same collection. Set
`<meta name="color-scheme" content="light">` so UA widgets stay light.

### 4.1 Colour tokens

Contrast ratios use the WCAG 2.x relative-luminance formula. They were computed by hand from the sRGB values and
rounded down to 2 decimals. `qa` re-checks them with any WCAG checker (§15).

| Token | Value | Applies to |
|---|---|---|
| `color/brand/primary` | `#1f5c40` | Primary buttons, links, active tab indicator, map trail and pins, focus-adjacent accents. The PWA `theme_color` |
| `color/brand/primary-pressed` | `#164430` | Primary button pressed/hover |
| `color/brand/primary-subtle` | `#e3efe8` | Selected tab background, Public badge, info notice background |
| `color/brand/on-primary` | `#ffffff` | Text and icons on `brand/primary` |
| `color/surface/page` | `#f4f6f3` | App background |
| `color/surface/card` | `#ffffff` | Cards, fields, dialogs, top bar |
| `color/surface/sunken` | `#eceeeb` | Private badge, skeleton base, empty map area, disabled field |
| `color/surface/inverse` | `#17201b` | Offline notice, Toast |
| `color/surface/scrim` | `rgba(23,32,27,0.56)` | Behind dialogs |
| `color/surface/photo-viewer` | `rgba(0,0,0,0.92)` | Full-screen photo viewer |
| `color/text/primary` | `#17201b` | Body and headings |
| `color/text/muted` | `#4d5a52` | Secondary text: dates, counts, helper text. Never lighter than this |
| `color/text/on-inverse` | `#ffffff` | Text on `surface/inverse` and on the photo viewer |
| `color/text/link` | `#1f5c40` | Links (always underlined in body text) |
| `color/text/disabled` | `#5f6b64` | Disabled control labels (kept legible although WCAG exempts them) |
| `color/border/strong` | `#6b776f` | Field borders, checkbox borders, secondary button border (meets 3:1 non-text) |
| `color/border/subtle` | `#d5dbd6` | Dividers and card outlines. Decorative only, never the sole boundary of a control |
| `color/status/danger` | `#b3261e` | Destructive buttons, error text, failed items, Rejected/Revoked badges |
| `color/status/danger-subtle` | `#fbe9e7` | Error notice background |
| `color/status/warning-text` | `#7a4b00` | Pending badge text, "still trying" and rate-limit notices |
| `color/status/warning-subtle` | `#fdf0d5` | Warning notice background, Pending badge |
| `color/status/success` | `#1f5c40` | Same as brand. Success is always paired with an icon and words |
| `color/control/disabled-bg` | `#e4e8e5` | Disabled button background |
| `color/focus/ring` | `#1d4ed8` | 3 px focus outline with 2 px offset on light surfaces |
| `color/focus/ring-on-dark` | `#ffffff` | Focus outline on `surface/inverse` and the photo viewer |
| `color/map/trail` | `#1f5c40` | Trail polyline, 4 px, over a 7 px `map/pin-stroke` halo |
| `color/map/pin` | `#1f5c40` | GPS stop pin fill; white 2 px stroke |
| `color/map/pin-stroke` | `#ffffff` | 2 px stroke on the GPS pin; 7 px halo under the trail |
| `color/map/pin-approx-fill` | `#ffffff` | Map-tap ("approximate") stop pin fill |
| `color/map/pin-approx-stroke` | `#1f5c40` | Approximate pin 3 px **dashed** stroke. The shape differs, not only the colour |
| `color/map/empty` | `#eceeeb` | Map container background when tiles haven't loaded (offline) |
| `color/map/grid` | `#d5dbd6` | 1 px lines of the 24 px grid drawn over `map/empty` |
| `color/map/attribution-bg` | `rgba(255,255,255,0.85)` | Background of the OSM attribution control |

**Computed contrast (text: AA needs 4.5:1, or 3:1 for large text; non-text UI: 3:1)**

| Foreground | Background | Ratio | Use | Result |
|---|---|---|---|---|
| `text/primary` #17201b | `surface/card` #ffffff | 16.68:1 | body | AAA |
| `text/primary` | `surface/page` #f4f6f3 | 15.35:1 | body | AAA |
| `text/muted` #4d5a52 | `surface/card` | 7.24:1 | secondary text | AAA |
| `text/muted` | `surface/page` | 6.66:1 | secondary text | AA (AAA large) |
| `text/muted` | `surface/sunken` #eceeeb | 6.21:1 | Private badge meta | AA |
| `brand/on-primary` #fff | `brand/primary` #1f5c40 | 7.88:1 | primary button label | AAA |
| `brand/on-primary` | `brand/primary-pressed` #164430 | 11.03:1 | pressed label | AAA |
| `brand/primary` | `surface/card` | 7.88:1 | links, secondary button label | AAA |
| `brand/primary` | `surface/page` | 7.25:1 | links on page | AAA |
| `brand/primary` | `brand/primary-subtle` #e3efe8 | 6.67:1 | Public badge, selected tab | AA |
| `text/primary` | `brand/primary-subtle` | 14.12:1 | info notice text | AAA |
| `status/danger` #b3261e | `surface/card` | 6.54:1 | error text | AA |
| `status/danger` | `surface/page` | 6.01:1 | error text | AA |
| `#ffffff` | `status/danger` | 6.54:1 | danger button label | AA |
| `status/danger` | `status/danger-subtle` #fbe9e7 | 5.58:1 | error notice | AA |
| `text/primary` | `status/danger-subtle` | 14.23:1 | error notice body | AAA |
| `status/warning-text` #7a4b00 | `status/warning-subtle` #fdf0d5 | 6.56:1 | Pending badge, warning notice | AA |
| `text/primary` | `status/warning-subtle` | 14.78:1 | warning notice body | AAA |
| `text/primary` | `surface/sunken` | 14.30:1 | Private badge | AAA |
| `text/disabled` #5f6b64 | `control/disabled-bg` #e4e8e5 | 4.50:1 | disabled label | AA (exempt anyway) |
| `text/on-inverse` #fff | `surface/inverse` #17201b | 16.68:1 | offline notice, toast | AAA |
| `border/strong` #6b776f | `surface/card` | 4.67:1 | field border (non-text) | ≥ 3:1 |
| `border/strong` | `surface/page` | 4.30:1 | field border (non-text) | ≥ 3:1 |
| `focus/ring` #1d4ed8 | `surface/card` | 6.70:1 | focus indicator | ≥ 3:1 |
| `focus/ring` | `surface/page` | 6.17:1 | focus indicator | ≥ 3:1 |
| `map/trail` #1f5c40 | typical OSM land (~#f2efe9) | ~6.8:1 | trail line | ≥ 3:1 |

Rules:
- **Sunlight rule:** body text uses `text/primary` only. `text/muted` is for secondary information, and nothing
  lighter than `text/muted` is ever used for text.
- **Never colour alone:** every status has an icon and a word ("Pending", "Failed", "Private"). The approximate
  pin differs by shape (hollow, dashed), not only by colour.
- The focus ring is not the brand green, because green on a green button would fail 3:1. The 2 px offset keeps
  the ring on the page or card surface, where it measures 6.17:1 or better.

### 4.2 Typography

**System font stack, no web font.** Justification: zero bytes to precache, no FOIT/FOUT on cold start, native
legibility tuned per platform, and no licence to track. Decided (Entry 30, D3): no web font.

`font/family/base`: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif,
"Apple Color Emoji", "Segoe UI Emoji"`
`font/family/mono`: `ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace`. Used only for the
recovery code.

Each size has a paired line-height token (`font/size/X` + `font/line-height/X`). Values are in `rem` in CSS (root
16 px); px are shown for reading.

| Size token · line-height token | Size / line height | Weight | Applies to |
|---|---|---|---|
| `font/size/display` · `font/line-height/display` | 30 / 36 px (1.875 / 2.25 rem) | 700 | Discover hero title (≥ 768 px only; phones use `title`) |
| `font/size/title` · `font/line-height/title` | 24 / 30 px (1.5 / 1.875 rem) | 700 | Screen H1: trip name, "Sign in" |
| `font/size/heading` · `font/line-height/heading` | 20 / 26 px (1.25 / 1.625 rem) | 600 | Section H2, stop name on detail, dialog title |
| `font/size/body` · `font/line-height/body` | 17 / 24 px (1.0625 / 1.5 rem) | 400 | Body, field values, buttons (600). 17 px ≥ 16 px stops iOS zooming on focus |
| `font/size/small` · `font/line-height/small` | 15 / 20 px (0.9375 / 1.25 rem) | 400 | Meta lines (dates, counts), helper text, badges (600) |
| `font/size/caption` · `font/line-height/caption` | 14 / 18 px (0.875 / 1.125 rem) | 400 | Map attribution, photo credit. Never the only carrier of essential information |
| `font/size/code` · `font/line-height/code` | 20 / 28 px (1.25 / 1.75 rem), mono | 600 | Recovery code, with `font/letter-spacing/code` = `0.06em` |
| `font/weight/regular` · `font/weight/semibold` · `font/weight/bold` | 400 · 600 · 700 | | |

- Text is set in `rem` with the root at 100%, so user text-size settings scale everything (WCAG 1.4.4). Layouts
  must survive 200% text and 320 px width (1.4.10 Reflow).
- Coordinates and counts use `font-variant-numeric: tabular-nums`.
- Maximum line length is 68ch for notes and body copy.

### 4.3 Spacing, radii, elevation, motion, sizing

| Token | Value | Applies to |
|---|---|---|
| `space/1` | 4 px | Icon-to-label gap inside badges |
| `space/2` | 8 px | Gap between inline controls; icon-to-label in buttons |
| `space/3` | 12 px | Gap inside a card's stack; field label → input |
| `space/4` | 16 px | Page side padding on phones; card padding; gap between fields |
| `space/5` | 20 px | Gap between cards in a list |
| `space/6` | 24 px | Page side padding ≥ 768 px; gap between sections |
| `space/8` | 32 px | Gap above a section heading |
| `space/12` | 48 px | Empty-state vertical padding |
| `radius/sm` | 6 px | Badges, checkboxes, thumbnails |
| `radius/md` | 10 px | Buttons, fields, notices |
| `radius/lg` | 16 px | Cards, dialogs, map frame |
| `radius/pill` | 999 px | Count badges |
| `elevation/0` | none | Page-level content |
| `elevation/1` | `0 1px 2px rgba(23,32,27,.12), 0 1px 1px rgba(23,32,27,.08)` | Cards |
| `elevation/2` | `0 4px 12px rgba(23,32,27,.16)` | Bottom action bar, top bar after scroll, toast |
| `elevation/3` | `0 12px 32px rgba(23,32,27,.28)` | Dialogs |
| `size/target/min` | 48 px (square) | Every tappable control (beats WCAG 2.5.8's 24 px and Apple/Material 44/48). Gloves |
| `size/target/primary` | 56 px tall | Bottom "Add stop", "Save stop", signup/sign-in submit |
| `size/icon/md` | 24 px | Icons in buttons and bars |
| `size/icon/sm` | 18 px | Icons in badges and notices |
| `size/topbar` | 56 px | Top bar height |
| `size/thumb/min` | 96 px | Minimum photo grid tile; tiles are fluid above it (§4.7) |
| `size/focus/width` | 3 px | Focus outline width |
| `size/focus/offset` | 2 px | Focus outline offset |
| `size/form/max` | 480 px | Max width of form columns |
| `motion/duration/fast` | 120 ms | Button press, tab indicator |
| `motion/duration/base` | 200 ms | Dialog, toast enter/exit |
| `motion/easing/standard` | `cubic-bezier(.2,0,0,1)` | All transitions |
| `z/map` · `z/bar` · `z/toast` · `z/dialog` · `z/viewer` | 0 · 500 · 900 · 1000 · 1100 | One token each. Stacking above Leaflet panes (Leaflet uses up to 1000 for controls, so the map sits in its own stacking context via `isolation: isolate`) |

**Reduced motion:** under `@media (prefers-reduced-motion: reduce)` all durations become 0, the skeleton shimmer
stops (a static `surface/sunken` block remains), and Leaflet `flyTo`/zoom animation is disabled
(`zoomAnimation:false`, `fadeAnimation:false`, `markerZoomAnimation:false`).

### 4.4 Layout grid and breakpoints

| Breakpoint | Width | Grid | Page padding | Content max width | Notes |
|---|---|---|---|---|---|
| `bp/base` (phone) | < 768 px | 4 col, 12 px gutter | 16 px | fluid | Designed at 390 px; must work at 320 px |
| `bp/md` (tablet) | ≥ 768 px | 8 col, 16 px gutter | 24 px | 720 px, centred | Trip cards in 2 columns; forms stay 480 px max |
| `bp/lg` (desktop) | ≥ 1024 px | 12 col, 24 px gutter | 32 px | 1200 px | Trip: map 7 col, timeline 5 col; Add stop in the header |

Forms (sign in, sign up, create trip, add stop) are a single column, max 480 px, at every breakpoint. Safe areas:
the bottom action bar and dialogs pad by `env(safe-area-inset-bottom)`. The app's `index.html` needs
`viewport-fit=cover` (a small dev task).

### 4.5 Icons

**Decided (Entry 30, D4): inline SVG, no icon dependency.** Paths are either drawn in-house or vendored **only**
from Lucide (ISC), Feather (MIT) or Tabler Icons (MIT), with that set's licence notice committed next to the icon
components. No runtime package, no icon font, no sprite fetched over the network. Each icon is a React component
that renders `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
stroke-linejoin="round">`, so it inherits text colour and needs no network or precache entry. Decorative icons
get `aria-hidden="true"`. An icon-only button always has an accessible name (`aria-label`) and a visible tooltip is
not required.

Set (16): `plus`, `arrow-left`, `map-pin`, `pin-approx` (dashed ring), `list` (timeline), `bike`, `users`,
`settings`, `camera`, `image`, `cloud-off` (offline), `clock` (pending / waiting), `alert-triangle` (failed /
warning), `check-circle` (sent / approved), `x` (close / remove), `download` (save photo), `lock` (private),
`globe` (public), `eye` / `eye-off` (password), `crosshair` (map centre), `user` (account). Draw them on a 24 px
grid with a 2 px stroke and 2 px padding (Lucide, Feather and Tabler all use this grid and stroke, so vendored and
in-house icons match). `pin-approx` has no stock equivalent and is drawn in-house.

### 4.6 Map treatment

- **Tiles:** Leaflet with the stock OpenStreetMap tile server, as today (decided, Entry 30, D5; a styled provider
  would add a dependency, possibly a key and usage limits). Keep the OSM attribution control visible and unobscured
  (licence requirement), in `font/size/caption` on `color/map/attribution-bg`.
- **Frame:** `radius/lg` corners, 1 px `border/subtle`, `color/map/empty` background with a 24 px grid of
  `color/map/grid` lines, so an offline map is visibly a map area, not a broken blank.
- **Heights:** on the phone Map tab, 60vh (min 320 px, max 560 px). On add-stop, 280 px. At ≥ 1024 px, fill the
  7-column pane (sticky, `calc(100vh - header)`).
- **Pins:** `L.divIcon` with an inline SVG. GPS stop: filled `map/pin` teardrop 28 × 36 px with a 2 px
  `map/pin-stroke`. Map-tap stop: the same size but a `map/pin-approx-fill` body with a 3 px dashed
  `map/pin-approx-stroke` (the "approximate" meaning shows by shape). Latest stop: 36 × 46 px plus a "Latest" label chip. The hit area is
  ≥ 48 × 48 px.
- **Trail:** `map/trail` 4 px polyline over a 7 px `map/pin-stroke` (white) halo, in chronological order (the
  server's trail).
- **Pin tap:** opens the stop detail (existing behaviour), and the pin's accessible name is "<stop name>,
  <arrival time with zone>".
- **Keyboard:** the map container is focusable (`tabindex="0"`, `aria-label="Trip map"`). Leaflet's keyboard
  handler (arrow keys to pan, +/- to zoom) stays enabled. Pins are reachable by Tab. Every pin also exists as a
  row in the Timeline, which is the non-map equivalent (WCAG 1.1.1 and 2.1.1).
- **Empty:** no stops shows a whole-of-Australia view (existing behaviour) with an overlaid EmptyState card:
  "No stops yet".
- **Public delay:** for non-members the map shows only delay-filtered stops (server-side), and the trail only when
  2 or more are visible. A small caption under the map: "Stops appear here <N> hours after they're added.", with N
  from `TripOut.publicDelayHours` ("1 hour" singular; no caption when N is 0, and none on private trips).

### 4.7 Photo treatment

- **Grid:** CSS grid, `repeat(auto-fill, minmax(96px, 1fr))`, 4 px gap, square tiles (`aspect-ratio: 1`,
  `object-fit: cover`), `radius/sm`. That gives 3 columns at 390 px (~116 px tiles), 5–6 at 768 px, and 8 at 1024.
- **Thumbnails:** `loading="lazy"`, `decoding="async"`, with a `surface/sunken` placeholder under each tile, so
  there's no layout shift. The alt text is "Photo by <uploadedBy>, taken <takenAt with zone>". The tile is a
  `<button>` with that same name.
- **Broken or expired URL:** keep the existing one-refetch rule (stop-detail header). While refetching, the tile
  shows the `image` icon on `surface/sunken`. If it still fails, "Photo unavailable" (caption text) in the tile.
- **Full-screen viewer:** a Dialog variant (§5.8) on `surface/photo-viewer`. The image uses `object-fit: contain`,
  so portrait and landscape both fit whole, never cropped. The top bar has a 48 px "Close" `x` button and
  "3 of 12". The bottom bar has "Photo by <name> · <time with zone>". Previous/Next are 48 px buttons at the sides
  (≥ 768 px) or in the bottom bar (phone), plus ←/→ keys. Swipe is optional; buttons are the required path
  (WCAG 2.5.1/2.5.7). Escape closes. Tapping the backdrop closes (existing behaviour). Focus returns to the tile
  that opened it.
- **Photos need the network** (accepted limit in the stop-detail header). Offline, the grid shows the EmptyState
  "Photos need a connection".
- **Add-stop previews:** 72 px square tiles from the processed blob (object URL, revoked on unmount), with a
  48 px "Remove photo <n>" button overlaid top-right.

## 5. Components

Each component is written as a Figma component with **variants** (mutually exclusive) and **properties**
(booleans, text slots). All interactive components have `default · hover · pressed · focus-visible · disabled`
states unless stated. Hover is desktop only and must not carry information.

### 5.1 Button

| Property | Values |
|---|---|
| variant | `primary` (brand fill, white label) · `secondary` (card fill, 2 px `border/strong`, `brand/primary` label) · `tertiary` (text only, underlined on hover, `brand/primary`) · `danger` (danger fill, white label) · `danger-secondary` (card fill, 2 px danger border, danger label) |
| size | `md` (48 px tall, padding 0 16) · `lg` (56 px tall, padding 0 20, full width on phones) |
| state | default · pressed (`primary-pressed`, or 6% ink overlay) · focus-visible (ring) · disabled (`control/disabled-bg`, `text/disabled`, `aria-disabled` kept focusable when a reason is shown) · loading (spinner replaces the leading icon; label stays, e.g. "Signing in…"; `aria-busy="true"`; not re-clickable) |
| leadingIcon | optional icon slot (24 px) |
| fullWidth | boolean |

Rules: one `primary` per view. Labels are verbs ("Save stop", "Approve", "Send request"), never "OK" or "Submit".
Destructive actions use `danger` only inside a ConfirmDialog. The trigger outside it is `danger-secondary` or
`tertiary`. **Disabled-with-reason:** if a primary action is disabled, say why in helper text directly above it
(e.g. "Add a name and a location to save"). Never a silently greyed button.

### 5.2 IconButton

48 × 48 px hit area, 24 px icon, `radius/md`. Variants: `plain` · `on-dark` (photo viewer, focus ring-on-dark).
It always has an `aria-label`.

### 5.3 TextField / TextArea

Anatomy (vertical auto layout, gap `space/2`): Label (body, 600) → optional helper (small, muted) → input (48 px
min, 17 px text, `radius/md`, 2 px `border/strong`, card fill, padding 12 px) → error line (small, danger,
`alert-triangle` icon, prefixed "Error:" for screen readers).

| Property | Values |
|---|---|
| state | default · focus (ring + border `brand/primary`) · filled · error (border `status/danger` 2 px + error line; `aria-invalid="true"`; `aria-describedby` → helper + error) · disabled · read-only |
| required | boolean: shows "(required)" after the label in words. No lone asterisk |
| counter | optional "12 / 280" (small, muted) for message and notes fields |

TextArea: min 3 rows, grows to 8, then scrolls. Labels are always visible, and a placeholder is never a label.
`autocomplete` set per field (§8.1).

### 5.4 PasswordField

TextField plus a trailing 48 px toggle IconButton "Show password" / "Hide password" (`eye` / `eye-off`,
`aria-pressed`). Helper text: **"15 to 128 characters. A short sentence you'll remember works well."** A live
length hint below the field (small, muted): "18 characters", or when too short, "7 more characters needed". No
strength meter, no composition rules (NIST SP 800-63B-4). `autocomplete="current-password"` on sign-in and
`"new-password"` on sign-up and reset. Paste is always allowed. The toggle never submits the form. The field
reverts to hidden on submit.

### 5.5 Card

| Variant | Anatomy | Interaction |
|---|---|---|
| `trip` | Vertical auto layout, padding 16, gap 8, `radius/lg`, `elevation/1`. Row 1: trip name (heading) + visibility Badge. Row 2 (small, muted): "Started 12 Oct 2026 · 3 riders". Row 3 (small, muted): "Last public stop 2 days ago", or "No public stops yet". For "Your trips", add a role Badge (Leader / Rider / Pending) | The whole card is one link (name is the link text; the card uses the stretched-link pattern). 48 px min |
| `stop` | Timeline row: time rail (dot or hollow dot for approximate) · stack: stop name (body 600) → "Tue 14 Oct, 4:05 pm ACST" (small, muted) + " · approximate location" → notes (body, clamped to 3 lines) | The row is a link to the stop detail (a real `<a>`, replacing today's `div role=link`) |
| `bike` | Rider name (heading) → "2019 Make Model" (body) → specs (body, pre-line) | Rider/leader: "Edit" `secondary md` button |
| `member` | Display name (body 600) + role Badge → "Joined 3 Oct" (small, muted) → action row | Leader actions per `screens/members.md` |
| `request` | Checkbox (48 px hit area) · display name (body 600) · "Requested 2 hours ago" · optional message quote (body, 280 max, `border-left` 3 px `border/subtle`) · "Via old rider link" Badge when `via=legacy_rider_link` · action row: Approve (primary md) · Reject (secondary md) · overflow "Reject and block" (danger-secondary) | See `screens/leader-review.md` |

### 5.6 Banner / StatusNotice

Full-width block in the status stack or inline in a screen. Horizontal auto layout: 24 px icon, a text stack
(title body 600 + detail small), and an optional action on the right (wraps below on narrow widths). Padding 12
16, `radius/md` when inline, square when in the global stack. Never auto-dismisses.

| Variant (tone) | Colours | Icon | Role |
|---|---|---|---|
| `info` | `brand/primary-subtle` bg, `text/primary` | `clock` | `role="status"` |
| `success` | `brand/primary-subtle` bg | `check-circle` | `role="status"` |
| `warning` | `warning-subtle` bg, `warning-text` title | `alert-triangle` | `role="status"` |
| `danger` | `danger-subtle` bg, `status/danger` title | `alert-triangle` | `role="alert"` only when it appears in response to a user action; otherwise `status` |
| `offline` | `surface/inverse` bg, white text | `cloud-off` | `role="status"` |

**Global instances (the status stack, top to bottom):**

1. **Offline:** shown when a request has just failed with no response, or `navigator.onLine === false`. This is a
   hint only; the queue never gates on it (Entry 19). "You're offline. New stops are saved on this phone and
   sent later."
2. **Sign in to send** (`warning`): the drain is **paused** on `401 UNAUTHENTICATED` (contract "Offline-queue
   classification"): the item is not marked failed and the attempt is not counted. Copy, verbatim from the
   contract: "Sign in to send N items" + button "Sign in" → `/signin?next=<current>`. The drain resumes on sign-in
   (the `auth` broadcast) and on the usual triggers.
3. **Held for another account** (`info`): entries whose `userId` ≠ the signed-in user, or with a `userId` while
   nobody is signed in; they are neither sent nor failed. "2 items were saved by another account on this phone.
   Sign in as that account to send them." No action button. It must never reveal the other username (the entry
   stores an id only). Pre-upgrade entries without a `userId` are sent under whatever session is current.
4. **QueueNotice** (`info`, collapsible): "Waiting to send: 1 stop, 2 photos" with a "Details" disclosure
   (`aria-expanded`) listing each entry: label + state "Waiting" / "Still trying: <lastError>" (warning icon,
   attempts ≥ 10). Includes entries waiting out a `429` (`Retry-After`, counted, no doubling).
5. **Failed items** (`danger`, always expanded): the never-retry codes (`VALIDATION_ERROR`, `METHOD_NOT_ALLOWED`,
   `CONFLICT`, `FORBIDDEN`, `NOT_FOUND`). One row per failed entry: "<label> couldn't be sent" and the `lastError`
   envelope message (for a revoked rider: "You're no longer a rider on this trip"; for a refused photo: the 422
   message about JPEG or the 15 MiB limit). Actions: photo entries get **"Save photo to this device"** (`secondary
   md`, `download` icon) and **"Dismiss"** (`tertiary`). Stop entries get "Dismiss" only. Dismiss on a photo that
   hasn't been saved opens a ConfirmDialog ("Dismiss without saving? The photo will be deleted from this phone.").
   When a stop fails, its queued photos fail with it, unsent.
6. **Rate limited** (`warning`, inline on the form that got the 429): the envelope message, then "Try again in
   N <unit>." from `Retry-After` (seconds). **Rounding rule:** under 60 s → seconds ("Try again in 45 seconds");
   under 2 h → whole minutes, rounded up ("Try again in 12 minutes"); otherwise hours, rounded up ("Try again in
   5 hours"). Submit stays disabled until the time passes; the line updates each minute (each second only in the
   last minute). Queue 429s are not shown separately: they count as "Waiting".

### 5.7 Tabs

Link-based tabs (routes), horizontal, scrollable on overflow, full width on the phone, each tab 48 px tall with
min width 72 px. Label (body 600) + optional count Badge. Active: `brand/primary` label, 3 px bottom indicator,
`aria-current="page"`. Inactive: `text/muted` label. Semantics: `<nav aria-label="Trip sections">` with links
(not ARIA `tablist`, because each tab is a URL). Variants: `trip` (under the trip header) · `segmented` (Members
tab's Requests · Members · Blocked: a pill group, `brand/primary-subtle` selected bg, also links via `?view=`).

### 5.8 Dialog / ConfirmDialog

- Native `<dialog>` with `showModal()` (focus trap, Escape, inert background for free). Scrim `surface/scrim`.
  Card `radius/lg`, `elevation/3`, padding 24, max width 440 px. On phones it's a bottom sheet: full width,
  top radius `radius/lg` only, plus the safe-area inset.
- Anatomy: title (heading, `aria-labelledby`) → body (body) → optional consequence list → button row (on phones
  stacked, primary on top; on ≥ 768 px right-aligned with the cancel action on the left of it).
- **ConfirmDialog variants:** `neutral` (primary confirm) · `destructive` (danger confirm; the confirm label
  repeats the verb and object, e.g. "Revoke Sam"). Initial focus goes to the **cancel** button for destructive
  dialogs, and to the first field or the confirm button otherwise. Focus returns to the trigger on close.
- **Typed confirmation** is not used. The actions are reversible or low-volume, and a clear verb label plus a
  consequence list is enough.

### 5.9 Badge

Inline flex, 24 px tall (non-interactive), padding 0 8, `radius/sm`, small 600, with an 18 px icon. Text is always
present.

| Variant | Icon | Colours | Text |
|---|---|---|---|
| `public` | `globe` | `primary-subtle` / `brand/primary` | Public |
| `private` | `lock` | `surface/sunken` / `text/primary` | Private |
| `pending` | `clock` | `warning-subtle` / `warning-text` | Pending |
| `leader` | `users` | `brand/primary` / white | Leader |
| `rider` | `bike` | `surface/sunken` / `text/primary` | Rider |
| `blocked` | `lock` | `danger-subtle` / `status/danger` | Blocked (leader's Blocked view only; never shown to the requester) |
| `legacy` | none | `surface/sunken` / `text/muted` | Text slot: "Via old rider link" (request rows) or "Old trip link" (continue card) |
| `count` | none | `brand/primary` / white, `radius/pill`, min 24 px | "3". The accessible name includes the noun, e.g. "3 pending requests" |

### 5.10 EmptyState

Vertical auto layout, centred, padding `space/12` 16. A 48 px icon in a 72 px `surface/sunken` circle, then the
title (heading), one sentence (body, muted), and an optional single action. Copy lives in each screen spec.

### 5.11 Skeleton

A `surface/sunken` block, `radius/sm`, in the shape of the content it replaces (trip card: 3 lines; stop row: 2
lines; map: frame). Shimmer: a 1.5 s linear gradient sweep, off under reduced motion. The container gets
`aria-busy="true"` and a visually hidden "Loading…" text. **Cold start:** if loading lasts more than 3 s, a
StatusNotice `info` appears above the skeleton: "Waking up the server… The first visit after a quiet spell can
take a little while." (keeps today's copy intent).

### 5.12 Toast

For confirmations whose result is also visible on the page (e.g. "Request sent", "Sam approved"). `surface/inverse`,
white text, `radius/md`, `elevation/2`, bottom-centred above the action bar, max width 440 px. `role="status"`.
Visible 5 s, the timer pauses on hover/focus, plus a Close IconButton. Never for errors, never for anything the
user must act on, never the only record of an outcome (WCAG 2.2.1). Reduced motion: no slide, instant.

### 5.13 Other shared pieces

- **TopBar:** `surface/card`, 56 px, gains `elevation/2` after scroll. Contents per §3.2.
- **BottomActionBar:** `surface/card`, `elevation/2`, padding 12 16 + safe area, one `primary lg` full-width
  button. The page gets bottom padding equal to its height so it never covers content (2.4.11).
- **LocationStatus** (add stop): a StatusNotice-like row with states in `screens/add-stop.md`.
- **RecoveryCodeBlock:** `surface/sunken` block, `radius/md`, padding 16. The code is in `font/size/code`, split
  into groups of 4 characters separated by spaces (visual only; copy/paste yields the exact code), `user-select:
  all`. Actions: "Copy code" (secondary) and "Download as text file" (tertiary). Copy feedback is the inline text
  "Copied" for 3 s, next to the button (`role="status"`).
- **Checkbox:** 24 px box, 2 px `border/strong`, `radius/sm`, and a 48 px hit area via the label.

## 6. Accessibility requirements (WCAG 2.2 AA)

| Criterion | Requirement in this app |
|---|---|
| 1.1.1 Non-text content | Photos: "Photo by <name>, taken <time>". Map: the Timeline is the equivalent. Decorative icons `aria-hidden` |
| 1.3.1 Info and relationships | Real headings (one H1 per screen), `<nav>`, `<main>`, lists for timelines and requests, `<label for>` on every field, `<fieldset><legend>` for the visibility choice |
| 1.3.5 Identify input purpose | `autocomplete`: `username`, `current-password`, `new-password`, `nickname` (display name), `one-time-code` (recovery code) |
| 1.4.1 Use of colour | Status = icon + word; approximate pin = shape; links underlined in body text |
| 1.4.3 / 1.4.11 Contrast | §4.1 table. Text ≥ 4.5:1, UI boundaries and focus ≥ 3:1 |
| 1.4.4 / 1.4.10 / 1.4.12 | rem units; reflow at 320 px; no fixed heights on text containers; survives text-spacing overrides |
| 2.1.1 Keyboard | Everything operable by keyboard, including the **map-tap fallback**: a "Use map centre" button with a fixed crosshair sets the manual location at the map's centre after keyboard panning (`screens/add-stop.md`) |
| 2.4.3 Focus order | DOM order = visual order. The bottom action bar comes last in the DOM |
| 2.4.7 Focus visible | `:focus-visible` ring: 3 px `focus/ring`, 2 px offset; ring-on-dark on dark surfaces. Never `outline: none` without a replacement |
| 2.4.11 Focus not obscured (min) | No sticky element covers a focused control. The bottom bar reserves page padding; `scroll-padding-bottom` equals the bar height; the status stack isn't sticky |
| 2.5.1 / 2.5.7 Pointer gestures, dragging | Photo viewer has buttons (swipe optional). The map needs no drag to set a location (tap, or pan with the keyboard plus "Use map centre") |
| 2.5.8 Target size (min) | All targets ≥ 48 × 48 px (well above 24 × 24). Inline text links in paragraphs are exempt but get 4 px vertical padding |
| 3.2.2 On input | Choosing visibility or selecting request checkboxes never navigates or submits |
| 3.3.1 / 3.3.3 Error identification, suggestion | Field errors in text next to the field, with a fix hint. On submit, focus moves to an error summary at the top ("2 problems: …" linking to fields) |
| 3.3.7 Redundant entry | The username carries over from sign-up to the next step; recovery pre-fills the username from `/signin` if typed |
| 3.3.8 Accessible authentication (min) | No cognitive tests. Paste allowed in all fields; password managers supported via autocomplete; show-password toggle; the recovery code can be copied and downloaded |
| 4.1.3 Status messages | Queue, toasts and "Copied" use `role="status"`; form-submit errors use `role="alert"` |
| Reduced motion | §4.3 |
| Orientation | Works in portrait and landscape; nothing is locked |

## 7. Interaction states: the standard set

Every screen spec names which of these apply and the exact copy. This table is the default wording.

| State | Trigger | Presentation | Default copy |
|---|---|---|---|
| **Loading** | Query pending, < 3 s | Skeleton in the content's shape | (visually hidden) "Loading…" |
| **Cold start** | Loading > 3 s | Skeleton + `info` notice | "Waking up the server… The first visit after a quiet spell can take a little while." |
| **Empty** | Successful response, no items | EmptyState | per screen |
| **Error (envelope)** | Error with an envelope message | Inline `danger` notice + "Try again" (secondary) | The envelope's `error.message`, as given |
| **Error (no envelope)** | Network failure, no cache | `danger` notice + "Try again" | "Can't reach the server. Check your signal and try again." |
| **Offline (with cache)** | Request failed, cached data exists | Render cached data + global Offline notice | "You're offline. Showing what this phone saved." |
| **Unauthorized (401)** | `UNAUTHENTICATED` from a session-gated route (writes, `/auth/*`, `/me/*`, leader routes). **Trip reads never 401** (the reader gate answers 200 or 404) | Page: redirect to `/signin?next=…`, except where it would lose typed input (inline "Sign in, then save again"). Queue: **paused**, not failed, attempt not counted: "Sign in to send N items" | "Your session has ended. Sign in to continue." |
| **Forbidden (403)** | `FORBIDDEN`: signed in and the trip is visible, but not permitted (non-member or revoked writing, non-leader doing a leader action, a leader target, a wrong current password, a CSRF block) | Inline `danger` notice; controls re-render from a refetched `viewer.role`. Queue: never-retry, failed and kept | Envelope message (e.g. "You're not a rider on this trip." / "You're no longer a rider on this trip.") |
| **Pending approval** | `viewer.role = pending` | Pending Badge in the trip header + `info` notice on the trip | "Your request to join is waiting for a leader. You'll be able to add stops once approved." |
| **Previously not approved** | The requester's latest request is `rejected` in `/me/join-requests` (the server reports **blocked as rejected**, and gives no cooldown date) | `info` notice on the join screen; the form stays | "A leader didn't approve your last request. You can ask again. If it's too soon, you'll be told when you send." |
| **Join refused (409)** | `CONFLICT` on a join request or claim: cooldown, block, recent revocation, pending cap, already a member | `warning` notice; form hidden for this visit | The **envelope message verbatim**. The UI adds no "blocked" label or date of its own |
| **Revoked** | Queue items fail with 403 after revocation; `viewer.role` drops to `none` | Failed items with "Save photo to this device" + Dismiss; the trip loses write controls (or shows 404 if private) | Envelope message: "You're no longer a rider on this trip". Notice title: "Your rider access was removed" |
| **Rate limited (429)** | `RATE_LIMITED` + `Retry-After` (seconds); covers both rate limits and the per-account lockout, which answers 429 even to correct credentials | `warning` notice on the form; submit disabled until the time passes. Queue: retried after `Retry-After`, shown as "Waiting" | Envelope message + "Try again in N <unit>." (rounding rule §5.6) |
| **Not found (404)** | Unknown trip **or** private trip for a non-member: the server's 404 is byte-identical for both (contract "Private means invisible, not forbidden"). Also a stop hidden by the public delay | Full-page EmptyState | Title "Trip not found". Body: "Check the link. If this is a private trip, sign in with an account that belongs to it." (signed in: "Check the link. If this is a private trip, you need to be a member to see it."). The copy depends only on the reader's own sign-in state, never on whether the trip exists |
| **Queued / waiting** | Local entries not sent | Global QueueNotice | "Waiting to send: 1 stop, 2 photos" |
| **Failed and dismissable** | Never-retry code | Global Failed items | "<label> couldn't be sent: <lastError>" |

## 8. Patterns

### 8.1 Forms and validation
- One column, labels above, 16 px between fields, the primary button at the end, full width on the phone.
- **Validate on submit, then live.** No errors while the user is typing a field for the first time. After a
  failed submit, a field's error clears as soon as it becomes valid.
- Client checks mirror the contract's rules, so most errors are caught before a round trip: required fields;
  username `^[a-z0-9][a-z0-9_.-]{2,31}$` after lowercasing; display name 1–40 after trimming; password 15–128
  characters (code points); trip name 1–100 after trimming; join message ≤ 280; public delay 0–168 hours. The
  server stays the authority.
- **The envelope has only `code` and `message`; there is no per-field `details`.** A `422` message goes in the error
  summary verbatim. A `409` or `403` goes on a field only where the endpoint has exactly one cause for it (signup's
  `409` = username taken → Username field; password change's `403` = wrong current password → Current password
  field); otherwise in the summary.
- Submitting shows the loading state on the button. Inputs stay editable but submit is blocked. Nothing typed
  is cleared on error (passwords excepted after a failed sign-in).
- Sign-in errors never say which of username or password was wrong: the contract's message is "Username or
  password is incorrect." Lockout and rate limit are both a `429`, shown with the server message and never
  described as "locked".

### 8.2 Confirmations
- **Confirm before:** revoke member, reject and block, step down, leave trip, unpublish (make private), publish
  (make public; privacy warning §9), dismiss an unsaved failed photo, sign out everywhere.
- **No confirm (reversible or cheap):** approve, reject (a 7-day cooldown, not permanent), cancel own request
  (immediate re-request allowed), unblock, promote (reversible by the promoted leader stepping down), sign out.
- Every successful action gives feedback where it happened (the row changes state), plus a Toast only when the
  row disappears from view (e.g. an approved request leaves the Requests list).

### 8.3 Destructive actions (copy and consequences)

| Action | Who | Trigger style | Dialog title | Consequence list | Confirm label |
|---|---|---|---|---|---|
| Revoke member | Leader, on a rider (never on a leader) | `danger-secondary` "Revoke" | "Remove Sam as a rider?" | "Sam can no longer add stops or photos." · "Anything Sam hasn't sent yet will be refused and shown to Sam as not sent." · "Sam can ask to join again in 7 days." · "Stops Sam already added stay on the trip." | "Remove Sam" |
| Reject and block | Leader | In the request row's "More" menu | "Reject and block Alex?" | "Alex can't ask to join this trip again until a leader unblocks them." · "You can unblock Alex from Members → Blocked." | "Reject and block" |
| Step down | Leader (not the last leader) | `danger-secondary` on the leader's own row | "Step down as leader?" | "You'll stay on the trip as a rider." · "You won't be able to review requests or manage members." | "Step down" |
| Leave trip | Rider or leader (not the last leader) | `tertiary` danger text on the viewer's own row in Members | "Leave this trip?" | "You won't be able to add stops or photos." · "Anything not sent yet will fail." · "What you've added stays on the trip." · Public trip: "You can ask to join again whenever you like." / Private trip: "It's private, so you won't be able to see it or ask to join again." | "Leave trip" |
| Make private | Leader | Trip settings | "Make this trip private?" | "Only members will be able to see it." · "Nobody new can ask to join while it's private." · "Anyone with the public link will see 'Trip not found'." | "Make private" |

**Last-leader guard:** when the viewer is the only leader, "Step down" and "Leave trip" are disabled with the reason
shown: "You're the only leader. Promote another rider to leader first." (matching the contract's `409` message). If
the server still answers `409`, show its message in the dialog.

### 8.4 Status feedback
- Global, persistent state goes in the status stack (§5.6). A local action's result goes inline, beside the
  control. Toasts are only a courtesy echo.
- Counts always carry nouns ("3 pending requests", not "3").
- Times always carry a zone label (Entry 26). Use `formatInstant` for absolute times. Relative times ("2 hours
  ago") are allowed only in secondary meta lines and have the absolute time in a `title` and in the `<time
  datetime>` attribute.

## 9. Privacy UX

- **Badges everywhere:** every trip card and trip header shows `Public` (globe) or `Private` (lock).
- **The public delay, explained.** N is `TripOut.publicDelayHours` (0–168, default 24), which every caller
  receives (C5 resolved). Copy below shows the default; always render the trip's own N ("1 hour" singular).
  - To **viewers** (public trip, non-member), a caption under the map and at the top of the timeline: "Stops
    appear here 24 hours after they're added." When N is 0, no caption.
  - To **riders and leaders** of a public trip (members see everything live), a one-line `info` notice on the trip
    Map and Timeline tabs, dismissable per device: "You see stops live. The public sees them after 24 hours." (N =
    0: "You and the public both see stops as soon as they're sent.") On a stop whose `arrivedAt + N` is still in
    the future (members only), a meta chip "Public from 4:05 pm tomorrow ACST", computed on the device (the server
    applies the same rule on its own clock, so the chip is a good estimate, not a promise). Private trips: no
    notice and no chip.
  - On Add stop, under the Save button (members of public trips): "This stop is visible to the public 24 hours
    after it's saved." (N = 0: "…as soon as it's sent.")
  - Leaders change the delay in Trip settings (0–168 h) with the helper: "A delay stops the public seeing where
    you are right now, or where you're camping tonight. 0 means stops are public immediately."
- **Publish dialog** (making a trip public, and the Create-trip visibility choice when "Public" is selected):
  - Title: "Make this trip public?"
  - Body: "Anyone can find it on Discover and see its map, stops, photos, bikes and riders' display names."
  - Warning notice (`warning`): "**Check your first stop.** Trips often start at someone's home. Public viewers
    see each stop's exact location. If your first stop is a home, consider starting from a nearby landmark."
  - Facts list: "Public viewers see stops 24 hours late." (N from the trip; N = 0: "Public viewers see stops as soon
    as they're sent.") · "They never see usernames, member lists or requests." · When publishing an existing
    private trip: "Publishing shares everything already added, not only new stops."
  - Buttons: "Make public" (primary) · "Keep private" (secondary).
  - The server has no publish confirmation of its own (contract, `PATCH /api/v2/trips/{tripId}`), so this dialog is
    the only guard and must never be skipped.
  - Per-stop "hide from public" is not available (ADR §1, filed as debt), and the design must not show such a
    control.
- **What viewers can see:** an expandable "Who can see this trip?" link in the trip header opens a small Dialog:
  - Public trip: "Anyone: the map, stops, photos and bikes, <N> hours after each stop is added, and riders'
    display names. Members: everything, as it happens." "Never shown publicly: usernames, who asked to join, the
    member list."
  - Private trip: "Only members of this trip."
- **Display name vs username:** sign-up explains "Your display name is shown on photos you add and to leaders.
  Your username is private; it's only for signing in."

## 10. Content and onboarding copy

Voice: plain, short, active, Australian/British spelling ("organiser", "colour"), sentence case, no exclamation
marks, no jargon ("session", "queue", "slug" and "401" never appear in the UI).

### 10.1 Onboarding card (Discover, signed out, dismissable per device)
- Title: "Follow a bike trip"
- Body line 1 (required, verbatim): **"Anyone can view public trips. Adding stops and photos needs an account and
  a leader's approval."**
- Body line 2: "Pick a trip below to follow it. No account needed."
- Actions: "Create an account" (secondary) · "Sign in" (tertiary)

### 10.2 How to join as a rider (on `/trips/$tripId/join` and in the signed-in onboarding)
1. "Create an account, or sign in."
2. "Open your trip and tap **Request to join**."
3. "A leader approves you. Then **Add stop** appears on the trip."

### 10.3 iOS line (Entry 18)
Shown on Discover's onboarding card, on `/signin` and `/signup` when the UA is iOS Safari **not** in standalone
mode, and in the user guide:
> "On iPhone, add this app to your Home Screen and **sign in inside the installed app**. The installed app keeps
> its own sign-in and its own unsent stops, separate from Safari."

On iOS **in** standalone mode with no session and a non-empty queue: "Sign in here to send the stops saved in this
app."

### 10.4 Finding trips
- Discover's intro: "Public trips, most recently updated first."
- A private trip can't be found. The copy says so on the 404: "If this is a private trip…" (§7).
- "Have an old trip link?" (tertiary link on Discover) opens a paste field, as today's `/` form did, and it
  routes to `/t/$slug` (legacy).

## 11. Rationale

Principles only. No proprietary text or third-party branded UI is copied.

| Decision | Grounding |
|---|---|
| Persistent status stack; exact counts; "Waiting" vs "Failed" | Nielsen #1 *Visibility of system status*. The rider must know what reached the server |
| "Sign in", "Request to join", "Waiting for approval" instead of generic "Access denied" | Nielsen #9 *Help users recognise, diagnose and recover from errors*; WCAG 3.3.3 Error suggestion |
| Plain words, no "slug", "session", "401" | Nielsen #2 *Match between system and the real world* |
| Confirm destructive actions; Dismiss requires saving or confirming for photos; cancel-own-request needs no confirm because re-request is allowed | Nielsen #5 *Error prevention* and #3 *User control and freedom* |
| One component library, one primary per view, consistent badges | Nielsen #4 *Consistency and standards* |
| Visible labels, show-password, paste allowed, copyable recovery code | Nielsen #6 *Recognition rather than recall*; WCAG 3.3.8 Accessible authentication |
| Minimal chrome, no hamburger, calm viewer mode | Nielsen #8 *Aesthetic and minimalist design* |
| Bulk approve for groups | Nielsen #7 *Flexibility and efficiency of use* |
| Help text inline (delay, publish warning) rather than a help page | Nielsen #10 *Help and documentation*, in context |
| 48 px targets, bottom primary button | WCAG 2.5.8 (24 px min, exceeded); one-handed and gloved use |
| High-contrast palette, 17 px body, no light greys | WCAG 1.4.3 / 1.4.11; outdoor glare reduces effective contrast, so we aim above AA |
| Focus ring offset in blue; nothing sticky over focus | WCAG 2.4.7, 2.4.11 |
| Keyboard "Use map centre" | WCAG 2.1.1 Keyboard, 2.5.7 Dragging movements |
| Identical 404 for private and unknown | ADR §2 (existence can't be probed); the copy doesn't leak |
| No web font, inline SVG | Offline-first: nothing extra to precache; fast cold start |

References: W3C, *Web Content Accessibility Guidelines (WCAG) 2.2*, W3C Recommendation (2023, updated 2024);
W3C, *Understanding WCAG 2.2* (for 2.4.11, 2.5.7, 2.5.8, 3.3.7, 3.3.8); J. Nielsen, *10 Usability Heuristics
for User Interface Design* (Nielsen Norman Group, 1994, updated 2020); NIST SP 800-63B-4 (password guidance, as
cited by the ADR).

## 12. Not in scope

These are **not** designed and must not appear, including if a generator (Stitch) invents them: likes,
reactions, comments, follows/followers, sharing buttons to social networks, avatars or profile photos, search
bars, filters/sort controls on Discover (sort is fixed by the server), notifications or push, chat, per-stop
"hide from public" (ADR debt), photo edit or delete, stop edit or delete, email fields, "Sign in with
Google/Apple/Microsoft", passkeys (ADR triggered debt), "Forgot username", account deletion, leader removing or
demoting another leader, a group join request, dark-mode toggle, maps with paid styles, offline map downloads,
**invites or join links for private trips** (ordinary debt `t-am-private-trip-invites`; C13), **editing the display
name** (debt `t-am-display-name-edit`), a requester-facing "Blocked" status (the server reports it as rejected), and
a pending-request count on "Your trips" beyond what the leader's own request list gives (X2).

## 13. Contract and behaviour dependencies (for `ba` / `architect`)

The design doesn't change behaviour. The draft flagged C1–C15 where a screen needed something the ADR didn't
specify. **All fifteen are now resolved** by `docs/api-contract.md` (Entry 29) or by `ba`, and no spec carries a
`TBC:` path. Where the contract's answer differed from what a draft spec assumed, the spec was changed to match the
contract.

| # | Need | Resolution | Specs changed |
|---|---|---|---|
| C1 | Auth paths and shapes | **Contract.** `POST /api/v2/auth/signup` (201 + one-time recovery code; not idempotent, a lost-response retry gets 409), `signin` (401 "Username or password is incorrect." for unknown user, wrong password and disabled account), `signout` (204 always), `signout-all`, `recover` (401 on bad code; new code shown once), `GET /api/v2/auth/me` (only response with a username), `password` (**403**, not 401, for a wrong current password; 422 if unchanged; this device gets a fresh session), `recovery-code` (rotation with password; 403 on a wrong one). Display-name edit is **not** provided: debt `t-am-display-name-edit`, no control designed. The draft's `login`/`logout` names are replaced | signin, signup, account |
| C2 | Write paths for trips without slugs | **Contract + `ba`.** `POST /api/v2/trips/{tripId}/stops`, `POST /api/v2/trips/{tripId}/stops/{stopId}/photos` (form `id`, `takenAt`, `file`; no `uploadedBy`), `POST /api/v2/trips/{tripId}/bikes`, `PATCH /api/v2/trips/{tripId}/bikes/{bikeId}`. New queue entries carry `tripId` + `userId`; slug entries keep the legacy paths | add-stop, trip-detail |
| C3 | "My trips" list | **Contract + `ba`.** `GET /api/v2/me/trips` (`MyTripOut[]`, active memberships with role). Pending requests come from C4, not from this list | rider-home, discover, account |
| C4 | The requester's own request status | **Contract + `ba`.** `GET /api/v2/me/join-requests` (newest first, ≤ 100; `blocked` is reported as `rejected`; no cooldown date). The design therefore never shows "Blocked" or a re-request date to a requester; refusals are shown from the `409` message verbatim. Cancel is `POST /api/v2/join-requests/{requestId}/cancel` (the draft's `DELETE` is replaced) | join-request, account, rider-home, §7 |
| C5 | `publicDelayHours` exposure | **Contract + `ba`.** `TripOut.publicDelayHours` is in every `TripOut`. The "Public from …" chip, the viewer caption and the Publish facts use the trip's own N; the draft's "Not public yet" fallback is dropped | trip-detail, stop-detail, add-stop, trip-settings, §4.6, §9 |
| C6 | Membership and review endpoints | **Contract + `ba`.** `GET …/members` (any active member, so riders get a read-only Members view), `POST …/members/{userId}/promote`, `DELETE …/members/{userId}`, `POST …/step-down`, `POST …/leave` (last leader → 409 "Promote another rider to leader first"; leave starts no cooldown), `GET …/join-requests?state=pending|blocked`, `POST …/join-requests/{requestId}/decision` (`approve` / `reject` / `reject_and_block`), `POST …/join-requests/{requestId}/unblock` (blocked → rejected, cooldown from the original decision still runs). **No bulk endpoint**: bulk is the UI looping over `decision`. **No pending count** field: see X2 | leader-review, members, trip-detail, rider-home |
| C7 | Trip settings update | **Contract.** `PATCH /api/v2/trips/{tripId}` with `name?`, `visibility?`, `publicDelayHours?` (omit = no change, `null` → 422). No server-side publish confirmation, so the Publish dialog is mandatory. Trip-name editing added to Trip settings | trip-settings |
| C8 | Legacy `access` once slugs grant nothing | **Contract.** `access` is derived from membership; the legacy GET also fills `viewer`. Legacy routes stay read-only in the UI anyway; members get "Open trip" | legacy-link |
| C9 | `DisplayNamePrompt` superseded | **`ba` confirmed:** the prompt is removed; the name comes from the account and the server sets `uploadedBy` (`t-am-fe-rider-home-restyle`) | add-stop |
| C10 | `/` without auto-redirect | **`ba` confirmed:** `/` is Discover with "Your trips" on top, no auto-redirect. A pre-upgrade `lastSlug` gets a "Continue: last trip link" card | discover, rider-home, §3.3 |
| C11 | Queue exposes paused and held counts | **Contract** ("Offline-queue classification (Entry 29)"): 401 pauses without failing or counting, notice "Sign in to send N items"; entries with another `userId` are held; 429 waits `Retry-After`; `t-am-queue-accounts` builds it | global-states, §5.6, add-stop |
| C12 | Username rules | **Contract.** Lowercased, then `^[a-z0-9][a-z0-9_.-]{2,31}$`; display name 1–40; password 15–128 code points. Checked client-side too | signup, §8.1 |
| C13 | Joining a new private trip | **`ba`: no join path.** Filed as ordinary debt `t-am-private-trip-invites`. The design adds no invite UI; it steers creators to public (the default): Create trip's Private option and Trip settings' Make private say "Nobody can ask to join a private trip", and the empty Requests list on a private trip points to Trip settings | create-trip, trip-settings, leader-review, join-request |
| C14 | Offline read by trip id | **`ba` confirmed:** the persisted `TripOut` is keyed per trip id and used as `initialData`. "Your trips" as a list is not persisted (Entry 19 unchanged) | rider-home, trip-detail |
| C15 | Keyboard "Use map centre" | **`ba`:** added to `t-am-fe-rider-home-restyle`; same manual-location path (`locationSource: "manual"`) | add-stop |

**Still open after reconciliation** (contract or scope points the design couldn't settle alone):

| # | Conflict | Design's current answer | Who decides |
|---|---|---|---|
| X1 | `ba`'s `t-am-fe-leader-review` AC puts leader screens at `/trips/$tripId/manage`; these specs use `/trips/$tripId/members?view=…` and `/trips/$tripId/settings`, and give riders a read-only Members view with Leave (the contract lets riders list members and leave, but no `ba` AC names a rider-facing Leave) | Screens are the same whichever path is chosen; riders need *some* place for Leave | `ba` |
| X2 | No pending-request count in `MyTripOut` or `TripOut`. The leader's badge on "Your trips" costs one `GET …/join-requests` per leader trip | Fetch lazily per leader trip; no badge offline. Acceptable for a handful of trips | `ba` / `architect` (a count field would be a contract change) |
| X3 | `MyJoinRequestOut` is described only as "`tripName`, `state` and `message`". The join screen needs its `id` (to cancel), `tripId` (to match the trip) and `createdAt` ("Sent 10 Oct …") | Specs assume all three are present | `architect` confirms in `t-am-contract-models` |
| X4 | `TripSummaryOut` fields aren't listed. Discover cards need name, `startDate`, `riderCount` and `lastPublicStopAt` | Specs assume them | `architect` confirms in `t-am-contract-models` |
| X5 | Join `409` messages (cooldown, block, caps, already a member) aren't worded in the contract. The UI shows them verbatim, so their wording is user-facing copy | Specs show the envelope message and add nothing | `dev` writes them in the task; `designer` can review wording |
## 14. Decisions (owner)

**D1–D7 are decided** (decision-log **Entry 30**, styling ruling from `t-am-styling-ruling`; D7 also confirmed by
`ba` as C10). Only D8 remains open.

| # | Decision | Outcome | What `dev` does |
|---|---|---|---|
| D1 | Styling approach | **Decided (Entry 30): plain CSS with custom properties.** No CSS framework, no CSS Modules, no new dependency | `frontend/src/styles/tokens.css` (Appendix A, verbatim) + `base.css` (element defaults, focus ring, reduced motion); one `Component.css` beside each component, imported by it; BEM-lite class names (`.btn`, `.btn--primary`, `.btn__icon`); token `a/b/c` → `--a-b-c`. No hex value outside `tokens.css` |
| D2 | Dark mode | **Decided: out for v1** (§4) | `<meta name="color-scheme" content="light">`; tokens stay semantic so a dark mode can be a later second block |
| D3 | Web font | **Decided: system stack**, no web font | `--font-family-base` / `--font-family-mono` only |
| D4 | Icon source | **Decided: inline SVG, in-house or vendored paths from Lucide, Feather or Tabler only**, with the licence notice committed | No icon package or font; §4.5 |
| D5 | Map theme | **Decided: stock OSM tiles** | Unchanged Leaflet tile layer; attribution visible |
| D6 | Bottom "Add stop" bar vs floating button | **Decided: full-width bottom bar** | §5.13 BottomActionBar |
| D7 | `/` for a returning rider | **Decided: Discover + "Your trips"**, no auto-redirect | `screens/discover.md`, `screens/rider-home.md` |
| D8 | Stitch generation | **Open (optional).** The owner can run `docs/design/stitch/HANDOFF.md` locally | Nothing; the specs here are sufficient to build |

## 15. Handoff checklist (for `qa`)

Global
- [ ] `frontend/src/styles/tokens.css` matches Appendix A: same property names and values (diff it).
- [ ] Every colour in the built CSS comes from a §4.1 token (`grep` the stylesheets for hex values outside `tokens.css`).
- [ ] No CSS framework, CSS-in-JS or icon package appears in `package.json` (Entry 30).
- [ ] The contrast pairs in §4.1 re-measure at or above the stated ratios in a WCAG checker.
- [ ] Every tappable control measures ≥ 48 × 48 px (devtools box model), including icon buttons, checkboxes (via
  label), map pins' hit areas, and photo tiles.
- [ ] Keyboard only: every screen can be completed with Tab/Shift+Tab/Enter/Space/Escape/arrows, and the focus ring
  is visible on every focusable element.
- [ ] No focused element is hidden under the top bar, bottom action bar or toast (WCAG 2.4.11).
- [ ] With `prefers-reduced-motion: reduce` there is no shimmer, no toast slide, and no map zoom animation.
- [ ] At 320 px wide and at 200% text, no horizontal scroll (except the map) and no clipped text.
- [ ] No text uses a colour lighter than `color/text/muted`.
- [ ] Every status (Public/Private/Pending/Leader/Failed/Waiting) shows a word, not only a colour.
- [ ] Every absolute time shows a zone label (Entry 26).
- [ ] The UI never shows the words "slug", "session", "401", "403", "404" or "429".
- [ ] No web font, icon package or map-style request appears in the network panel.

Audiences
- [ ] Anonymous on a public trip: no Add stop, no Members tab, no settings icon, no request list; a "Riding on this
  trip?" link is present.
- [ ] Signed-in non-member: a "Request to join" button; no Add stop.
- [ ] Pending: the Pending badge and notice; a "Cancel request" button; no Add stop.
- [ ] Rider: Add stop bar present; Members tab present but read-only (no Requests, Blocked, Make leader or Revoke),
  with Leave trip on the rider's own row only.
- [ ] Leader: Members tab with a count badge when requests are pending; a settings icon; no Revoke or Demote control on
  another leader's row.
- [ ] Private or unknown trip as a non-member: identical page text for both (compare the DOM text).

States
- [ ] Cold start: the "Waking up the server…" notice appears only after 3 s of loading.
- [ ] Offline with a queue: the Offline notice and "Waiting to send: N stops, M photos" both show; nothing claims "sent".
- [ ] 401 on the queue: "Sign in to send N items" with a Sign in button; the entries remain "Waiting", not failed.
- [ ] Revoked: failed photo rows show "Save photo to this device" and it downloads a `.jpg`; Dismiss on an unsaved
  photo asks for confirmation.
- [ ] 429 on sign-in (including correct credentials during a lockout): the envelope message, "Try again in N
  minutes" from `Retry-After`, and submit disabled until then; the word "locked" never appears.
- [ ] Held entries for another account: the count shows, and no username is shown.
- [ ] A join `409` shows the envelope message verbatim; the requester never sees "Blocked".

Privacy
- [ ] The Publish dialog shows the first-stop/home warning and the trip's own delay fact (N from `publicDelayHours`).
- [ ] Viewer captions state the trip's delay (none when it's 0); the member notice states "You see stops live".
- [ ] Choosing Private (create or settings) says nobody can ask to join; no invite control exists.
- [ ] No username appears anywhere a non-member can see it (Discover, trip, stop, photo credit).

Onboarding
- [ ] The Discover onboarding card contains the exact line "Anyone can view public trips. Adding stops and photos
  needs an account and a leader's approval."
- [ ] iOS Safari (not standalone) shows "sign in inside the installed app" on `/signin` and `/signup`.

## Appendix A. `tokens.css` (paste verbatim)

Target: `frontend/src/styles/tokens.css` (Entry 30). Every token in §4 appears once, named by the Entry 30 rule
(`color/brand/primary` → `--color-brand-primary`). Font sizes and line heights are in `rem` (root 16 px) so user
text scaling works; everything else is in `px`. Breakpoints are not custom properties (they can't be used in
`@media`): use `min-width: 768px` (`bp/md`) and `min-width: 1024px` (`bp/lg`) literally. If a value here and a table
in §4 ever disagree, fix the table: this block is what `qa` diffs against.

```css
/* Bike Trip Journal design tokens. Source: docs/design/DESIGN.md §4, Appendix A.
   Naming (decision-log Entry 30): token "a/b/c" -> custom property "--a-b-c".
   Light mode only (D2). No other file may contain a hex colour. */

:root {
  color-scheme: light;

  /* Colour: brand */
  --color-brand-primary: #1f5c40;
  --color-brand-primary-pressed: #164430;
  --color-brand-primary-subtle: #e3efe8;
  --color-brand-on-primary: #ffffff;

  /* Colour: surfaces */
  --color-surface-page: #f4f6f3;
  --color-surface-card: #ffffff;
  --color-surface-sunken: #eceeeb;
  --color-surface-inverse: #17201b;
  --color-surface-scrim: rgba(23, 32, 27, 0.56);
  --color-surface-photo-viewer: rgba(0, 0, 0, 0.92);

  /* Colour: text */
  --color-text-primary: #17201b;
  --color-text-muted: #4d5a52;
  --color-text-on-inverse: #ffffff;
  --color-text-link: #1f5c40;
  --color-text-disabled: #5f6b64;

  /* Colour: borders */
  --color-border-strong: #6b776f;
  --color-border-subtle: #d5dbd6;

  /* Colour: status */
  --color-status-danger: #b3261e;
  --color-status-danger-subtle: #fbe9e7;
  --color-status-warning-text: #7a4b00;
  --color-status-warning-subtle: #fdf0d5;
  --color-status-success: #1f5c40;

  /* Colour: controls and focus */
  --color-control-disabled-bg: #e4e8e5;
  --color-focus-ring: #1d4ed8;
  --color-focus-ring-on-dark: #ffffff;

  /* Colour: map */
  --color-map-trail: #1f5c40;
  --color-map-pin: #1f5c40;
  --color-map-pin-stroke: #ffffff;
  --color-map-pin-approx-fill: #ffffff;
  --color-map-pin-approx-stroke: #1f5c40;
  --color-map-empty: #eceeeb;
  --color-map-grid: #d5dbd6;
  --color-map-attribution-bg: rgba(255, 255, 255, 0.85);

  /* Typography: families (D3: system stack, no web font) */
  --font-family-base: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans",
    sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  --font-family-mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;

  /* Typography: sizes and line heights (rem, root 16px) */
  --font-size-display: 1.875rem;     /* 30px */
  --font-line-height-display: 2.25rem; /* 36px */
  --font-size-title: 1.5rem;         /* 24px */
  --font-line-height-title: 1.875rem;  /* 30px */
  --font-size-heading: 1.25rem;      /* 20px */
  --font-line-height-heading: 1.625rem; /* 26px */
  --font-size-body: 1.0625rem;       /* 17px */
  --font-line-height-body: 1.5rem;     /* 24px */
  --font-size-small: 0.9375rem;      /* 15px */
  --font-line-height-small: 1.25rem;   /* 20px */
  --font-size-caption: 0.875rem;     /* 14px */
  --font-line-height-caption: 1.125rem; /* 18px */
  --font-size-code: 1.25rem;         /* 20px */
  --font-line-height-code: 1.75rem;    /* 28px */
  --font-letter-spacing-code: 0.06em;

  /* Typography: weights */
  --font-weight-regular: 400;
  --font-weight-semibold: 600;
  --font-weight-bold: 700;

  /* Spacing (4px grid) */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-12: 48px;

  /* Radii */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 16px;
  --radius-pill: 999px;

  /* Elevation */
  --elevation-0: none;
  --elevation-1: 0 1px 2px rgba(23, 32, 27, 0.12), 0 1px 1px rgba(23, 32, 27, 0.08);
  --elevation-2: 0 4px 12px rgba(23, 32, 27, 0.16);
  --elevation-3: 0 12px 32px rgba(23, 32, 27, 0.28);

  /* Sizing */
  --size-target-min: 48px;
  --size-target-primary: 56px;
  --size-icon-md: 24px;
  --size-icon-sm: 18px;
  --size-topbar: 56px;
  --size-thumb-min: 96px;
  --size-focus-width: 3px;
  --size-focus-offset: 2px;
  --size-form-max: 480px;

  /* Motion */
  --motion-duration-fast: 120ms;
  --motion-duration-base: 200ms;
  --motion-easing-standard: cubic-bezier(0.2, 0, 0, 1);

  /* Stacking (the map container uses isolation: isolate) */
  --z-map: 0;
  --z-bar: 500;
  --z-toast: 900;
  --z-dialog: 1000;
  --z-viewer: 1100;
}

@media (prefers-reduced-motion: reduce) {
  :root {
    --motion-duration-fast: 0ms;
    --motion-duration-base: 0ms;
  }
}
```

`base.css` (not specified token-by-token here) applies these: `body` in `--font-family-base`,
`--font-size-body`/`--font-line-height-body`, `--color-text-primary` on `--color-surface-page`; a global
`:focus-visible { outline: var(--size-focus-width) solid var(--color-focus-ring); outline-offset:
var(--size-focus-offset); }`; and, under reduced motion, no shimmer animation (§4.3).
