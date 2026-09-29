# Bike Trip Journal: Design system and UX spec

> **Owner:** `designer`. **Status:** v1 draft, 2026-09-29. **Scope:** the redesign that follows the accepted
> access-model ADR ("Public trips, local accounts, leader-gated membership", Architect, 2026-09-29, orchestrator
> ruling same day), recorded as **decision-log Entry 29**. Referred to below as **the ADR** (Entry 29) with its
> section numbers (ADR §1 … §15).
>
> **Stitch was not used.** This session had no `mcp__stitch__*` tools and no Stitch skills. Everything here was
> written from the code, the ADR, `docs/user-guide.md` and `docs/real-device-test-plan.md`. How to generate the
> screens with Stitch later: `docs/design/stitch/HANDOFF.md`.
>
> **Source of truth for `dev`:** this file (tokens, components, patterns, states) and `docs/design/screens/*.md`
> (per-screen layout). Mockups in `docs/design/mockups/` are illustrations only. Where a spec needs an API that the
> ADR doesn't name yet, it is marked **TBC contract** and listed in §13. `dev` must not implement a TBC surface
> until `docs/api-contract.md` defines it.

Contents: 1 Principles · 2 Audiences · 3 Information architecture · 4 Visual system · 5 Components ·
6 Accessibility · 7 Interaction states · 8 Patterns · 9 Privacy UX · 10 Content and onboarding copy ·
11 Rationale · 12 Not in scope · 13 Contract and behaviour dependencies · 14 Open decisions · 15 Handoff checklist

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
7. **Reversible and dependency-free.** System fonts, inline SVG icons, stock OpenStreetMap tiles and no CSS
   framework until the owner decides otherwise (§14).

## 2. Audiences and mental model

| Audience | Who | What they think the app is | What they can do | What they must never see |
|---|---|---|---|---|
| **Public viewer** (anonymous) | Friends, family, anyone with a public trip link, on any device | "A travel diary of a bike trip, a day behind" | Browse Discover; open public trips; see map, timeline, photos and bikes, all subject to the public delay | Write controls, member or request lists, usernames, anything live |
| **Signed-in person, not a member** | A rider who has just made an account, or a follower who signed up | "I have an account; this trip isn't mine yet" | Everything a public viewer can do, plus request to join (one request per trip), cancel a request, create their own trip | Write controls on trips they don't belong to |
| **Pending requester** | Signed in, request sent | "Waiting for the organiser" | View as public; see "Request pending"; cancel it | Write controls |
| **Rider** (approved member) | On the bikes | "My trip's journal; I add to it" | Everything above, plus see the trip live (no delay); add stops and photos (queued offline); add and edit bikes; leave the trip (TBC contract) | Member admin controls |
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
| `/trips/$tripId/members` | Trip: **Members** tab (Requests · Members · Blocked) | leader | proposed | `screens/leader-review.md`, `screens/members.md` |
| `/trips/$tripId/settings` | Trip settings (visibility, public delay) | leader | proposed | `screens/trip-settings.md` |
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
- **Trip tabs** under the header: `Map · Timeline · Bikes`, plus `Members` for leaders, with a count Badge when
  requests are pending. Tabs are links to routes (so Back works and they can be deep-linked), styled as tabs,
  with `aria-current="page"` (§5.7).
- **Primary action bar** (riders and leaders on trip tabs): a bottom-docked full-width "Add stop" button, 56 px,
  above the safe-area inset. On ≥ 1024 px it moves into the trip header as an ordinary primary button.
- **≥ 1024 px:** Map and Timeline merge into one split view (map 7 columns, timeline 5), and the tab row reads
  `Journey · Bikes · Members`.

### 3.3 First-open and returning flows

| Situation | Lands on | Notes |
|---|---|---|
| First visit, no account | `/` Discover with the onboarding card (§10.1) | |
| Signed in, member of ≥ 1 trip | `/` with "Your trips" above Discover | One tap to the trip. The current auto-redirect to the last slug is **not** carried over (open decision D7) |
| Opened `/trips/$tripId` link | That trip | If private and the reader isn't a member: the 404 state (§7) |
| Old `/t/$slug` link | Legacy landing | `screens/legacy-link.md` |
| iOS Home Screen app, first launch | `/` signed out, even if signed in in Safari | Onboarding card shows the iOS line (§10.3) |

## 4. Visual system

Tokens are named like a Figma Variables collection (`group/subgroup/name`). In CSS they become custom properties
by replacing `/` with `-` and prefixing `--`: `color/brand/primary` → `--color-brand-primary`.

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
| `color/map/trail` | `#1f5c40` | Trail polyline, 4 px, over a 7 px `#ffffff` halo |
| `color/map/pin` | `#1f5c40` | GPS stop pin fill; white 2 px stroke |
| `color/map/pin-approx` | `#ffffff` fill, `#1f5c40` 3 px dashed stroke | Map-tap ("approximate") stop pin. The shape differs, not only the colour |
| `color/map/empty` | `#eceeeb` with a 24 px `#d5dbd6` grid | Map container background when tiles haven't loaded (offline) |

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
legibility tuned per platform, and no licence to track. A web font would be a locked-stack decision (§14 D3), and
nothing in this design needs one.

`font/family/base`: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif,
"Apple Color Emoji", "Segoe UI Emoji"`
`font/family/mono`: `ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace`. Used only for the
recovery code.

| Token | Size / line height | Weight | Applies to |
|---|---|---|---|
| `font/size/display` | 30 / 36 px | 700 | Discover hero title (≥ 768 px only; phones use `title`) |
| `font/size/title` | 24 / 30 px | 700 | Screen H1: trip name, "Sign in" |
| `font/size/heading` | 20 / 26 px | 600 | Section H2, stop name on detail, dialog title |
| `font/size/body` | 17 / 24 px | 400 | Body, field values, buttons (600). 17 px ≥ 16 px stops iOS zooming on focus |
| `font/size/small` | 15 / 20 px | 400 | Meta lines (dates, counts), helper text, badges (600) |
| `font/size/caption` | 14 / 18 px | 400 | Map attribution, photo credit. Never the only carrier of essential information |
| `font/size/code` | 20 / 28 px, mono, `letter-spacing: 0.06em` | 600 | Recovery code |
| `font/weight/regular` · `semibold` · `bold` | 400 · 600 · 700 | | |

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
| `size/target/min` | 48 × 48 px | Every tappable control (beats WCAG 2.5.8's 24 px and Apple/Material 44/48). Gloves |
| `size/target/primary` | 56 px tall | Bottom "Add stop", "Save stop", signup/sign-in submit |
| `size/icon/md` | 24 px | Icons in buttons and bars |
| `size/icon/sm` | 18 px | Icons in badges and notices |
| `size/topbar` | 56 px | Top bar height |
| `size/thumb` | fluid, min 96 px | Photo grid tile (§4.7) |
| `motion/duration/fast` | 120 ms | Button press, tab indicator |
| `motion/duration/base` | 200 ms | Dialog, toast enter/exit |
| `motion/easing/standard` | `cubic-bezier(.2,0,0,1)` | All transitions |
| `z/map` · `z/bar` · `z/toast` · `z/dialog` · `z/viewer` | 0 · 500 · 900 · 1000 · 1100 | Stacking above Leaflet panes (Leaflet uses up to 1000 for controls, so the map sits in its own stacking context via `isolation: isolate`) |

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

**Decision (reversible): a small in-house inline-SVG set, no icon dependency.** Each icon is a React component
that renders `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
stroke-linejoin="round">`, so it inherits text colour and needs no network or precache entry. Decorative icons
get `aria-hidden="true"`. An icon-only button always has an accessible name (`aria-label`) and a visible tooltip is
not required.

Set (16): `plus`, `arrow-left`, `map-pin`, `pin-approx` (dashed ring), `list` (timeline), `bike`, `users`,
`settings`, `camera`, `image`, `cloud-off` (offline), `clock` (pending / waiting), `alert-triangle` (failed /
warning), `check-circle` (sent / approved), `x` (close / remove), `download` (save photo), `lock` (private),
`globe` (public), `eye` / `eye-off` (password), `crosshair` (map centre), `user` (account). Draw them on a 24 px
grid with a 2 px stroke and 2 px padding. If the owner prefers a maintained set, vendoring SVG paths from an
ISC/MIT set is an owner decision (§14 D4). No runtime package either way.

### 4.6 Map treatment

- **Tiles:** Leaflet with the stock OpenStreetMap tile server, as today. A nicer theme (for example a
  hosted vector or raster style) would add a third-party dependency and possibly a key and usage limits. That is
  owner decision D5. Keep the OSM attribution control visible and unobscured (licence requirement), in
  `font/size/caption` with a white 85% background.
- **Frame:** `radius/lg` corners, 1 px `border/subtle`, `surface/sunken` background with the 24 px grid pattern
  (`color/map/empty`), so an offline map is visibly a map area, not a broken blank.
- **Heights:** on the phone Map tab, 60vh (min 320 px, max 560 px). On add-stop, 280 px. At ≥ 1024 px, fill the
  7-column pane (sticky, `calc(100vh - header)`).
- **Pins:** `L.divIcon` with an inline SVG. GPS stop: filled `map/pin` teardrop 28 × 36 px with a 2 px white
  stroke. Map-tap stop: the same size but a hollow white fill with a dashed `brand/primary` stroke (the
  "approximate" meaning shows by shape). Latest stop: 36 × 46 px plus a "Latest" label chip. The hit area is
  ≥ 48 × 48 px.
- **Trail:** `map/trail` 4 px polyline over a 7 px white halo, in chronological order (the server's trail).
- **Pin tap:** opens the stop detail (existing behaviour), and the pin's accessible name is "<stop name>,
  <arrival time with zone>".
- **Keyboard:** the map container is focusable (`tabindex="0"`, `aria-label="Trip map"`). Leaflet's keyboard
  handler (arrow keys to pan, +/- to zoom) stays enabled. Pins are reachable by Tab. Every pin also exists as a
  row in the Timeline, which is the non-map equivalent (WCAG 1.1.1 and 2.1.1).
- **Empty:** no stops shows a whole-of-Australia view (existing behaviour) with an overlaid EmptyState card:
  "No stops yet".
- **Public delay:** for non-members the map shows only delay-filtered stops (server-side). A small caption under
  the map: "Stops appear here 24 hours after they're added." (the value comes from the trip if exposed, §13 C5).

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
2. **Sign in to send** (`warning`): the queue is paused on 401 (ADR §3). "Sign in to send 3 items" + button
   "Sign in" → `/signin?next=<current>`. The items are not failed.
3. **Held for another account** (`info`): entries whose `userId` ≠ the signed-in user (ADR §3). "2 items were
   saved by another account on this phone. Sign in as that account to send them." No action button. It must
   never reveal the other username (the entry stores an id only).
4. **QueueNotice** (`info`, collapsible): "Waiting to send: 1 stop, 2 photos" with a "Details" disclosure
   (`aria-expanded`) listing each entry: label + state "Waiting" / "Still trying: <lastError>" (warning icon,
   attempts ≥ 10).
5. **Failed items** (`danger`, always expanded): one row per failed entry: "<label> couldn't be sent" and the
   `lastError` (for a revoked rider: "You're no longer a rider on this trip"). Actions: photo entries get **"Save
   photo to this device"** (`secondary md`, `download` icon) and **"Dismiss"** (`tertiary`). Stop entries get
   "Dismiss" only. Dismiss on a photo that hasn't been saved opens a ConfirmDialog ("Dismiss without saving? The
   photo will be deleted from this phone.").
6. **Rate limited** (`warning`, inline on the form that got the 429): "Too many attempts. Try again in 12
   minutes." The minutes come from `Retry-After`, rounded up. Queue 429s are not shown separately: they count as
   "Waiting" (retried).

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
| `blocked` | `lock` | `danger-subtle` / `status/danger` | Blocked |
| `legacy` | none | `surface/sunken` / `text/muted` | Via old rider link |
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
| **Unauthorized (401)** | `UNAUTHENTICATED` on a write or account call | Page: redirect to `/signin?next=…`. Queue: "Sign in to send N items" | "Your session has ended. Sign in to continue." |
| **Forbidden / not a member (403)** | `FORBIDDEN` on a write | Inline `danger` notice; controls re-render from a refetched `viewer.role` | Envelope message, or "You're not a rider on this trip." |
| **Pending approval** | `viewer.role = pending` | Pending Badge in the trip header + `info` notice on the trip | "Your request to join is waiting for a leader. You'll be able to add stops once approved." |
| **Rejected** | 409 `CONFLICT` on re-request, or join-status TBC (§13 C4) | `warning` notice on the join screen | "A leader didn't approve your request. You can ask again 7 days after it was declined." (envelope message wins if present) |
| **Blocked** | Join refused as blocked | `warning` notice | "You can't request to join this trip. Contact the trip's leader if you think this is a mistake." |
| **Revoked** | Queue items fail with 403 after revocation; `viewer.role` drops to `none` | Failed items with "Save photo to this device" + Dismiss; the trip loses write controls (or shows 404 if private) | Envelope message: "You're no longer a rider on this trip". Notice title: "Your rider access was removed" |
| **Rate limited (429)** | `RATE_LIMITED` + `Retry-After` | `warning` notice on the form; submit disabled until the time passes (the countdown updates each minute, not each second) | "Too many attempts. Try again in N minutes." |
| **Not found (404)** | Unknown trip **or** private trip for a non-member (ADR §2): identical | Full-page EmptyState | Title "Trip not found". Body: "Check the link. If this is a private trip, sign in with an account that belongs to it." (signed in: "Check the link. If this is a private trip, you need to be a member to see it."). The copy depends only on the reader's own sign-in state, never on whether the trip exists |
| **Queued / waiting** | Local entries not sent | Global QueueNotice | "Waiting to send: 1 stop, 2 photos" |
| **Failed and dismissable** | Never-retry code | Global Failed items | "<label> couldn't be sent: <lastError>" |

## 8. Patterns

### 8.1 Forms and validation
- One column, labels above, 16 px between fields, the primary button at the end, full width on the phone.
- **Validate on submit, then live.** No errors while the user is typing a field for the first time. After a
  failed submit, a field's error clears as soon as it becomes valid.
- Client checks are limited to what the ADR states: required fields, password length 15–128, message ≤ 280, and
  public delay 0–168 hours. Everything else (username rules, uniqueness) comes back from the server as a
  `VALIDATION_ERROR` or `CONFLICT` envelope and is shown next to the matching field when `details` point to one,
  otherwise in the error summary.
- Submitting shows the loading state on the button. Inputs stay editable but submit is blocked. Nothing typed
  is cleared on error.
- Sign-in errors never say which of username or password was wrong: "Username or password is incorrect."
  Lockout and rate limit use the server message.

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
| Leave trip | Rider or leader (not the last leader) | `tertiary` danger text at the bottom of Members / trip menu | "Leave this trip?" | "You won't be able to add stops or photos." · "Anything not sent yet will fail." · "What you've added stays on the trip." | "Leave trip" |
| Make private | Leader | Trip settings | "Make this trip private?" | "Only members will be able to see it." · "Anyone with the public link will see 'Trip not found'." | "Make private" |

**Last-leader guard:** when the viewer is the only leader, "Step down" and "Leave trip" are disabled with the reason
shown: "You're the only leader. Promote someone to leader first." If the server still answers 409, show its
message in the dialog.

### 8.4 Status feedback
- Global, persistent state goes in the status stack (§5.6). A local action's result goes inline, beside the
  control. Toasts are only a courtesy echo.
- Counts always carry nouns ("3 pending requests", not "3").
- Times always carry a zone label (Entry 26). Use `formatInstant` for absolute times. Relative times ("2 hours
  ago") are allowed only in secondary meta lines and have the absolute time in a `title` and in the `<time
  datetime>` attribute.

## 9. Privacy UX

- **Badges everywhere:** every trip card and trip header shows `Public` (globe) or `Private` (lock).
- **The 24-hour public delay, explained:**
  - To **viewers** (public trip, non-member), a caption under the map and at the top of the timeline: "Stops
    appear here 24 hours after they're added." When the trip has a 0-hour delay, no caption.
  - To **riders and leaders** (members see everything live), a one-line `info` notice on the trip Map and Timeline
    tabs, dismissable per device: "You see stops live. The public sees them after 24 hours." On a stop added in
    the last 24 h (members only), a meta chip: "Public from 4:05 pm tomorrow" (needs the delay value; §13 C5.
    Without it, the chip reads "Not public yet").
  - On Add stop, under the Save button (members of public trips): "This stop is visible to the public 24 hours
    after it's saved."
  - Leaders change the delay in Trip settings (0–168 h) with the helper: "A delay stops the public seeing where
    you are right now, or where you're camping tonight. 0 means stops are public immediately."
- **Publish dialog** (making a trip public, and the Create-trip visibility choice when "Public" is selected):
  - Title: "Make this trip public?"
  - Body: "Anyone can find it on Discover and see its map, stops, photos, bikes and riders' display names."
  - Warning notice (`warning`): "**Check your first stop.** Trips often start at someone's home. Public viewers
    see each stop's exact location. If your first stop is a home, consider starting from a nearby landmark."
  - Facts list: "Public viewers see stops 24 hours late." · "They never see usernames, member lists or requests."
  - Buttons: "Make public" (primary) · "Keep private" (secondary).
  - Per-stop "hide from public" is not available (ADR §1, filed as debt), and the design must not show such a
    control.
- **What viewers can see:** an expandable "Who can see this trip?" link in the trip header opens a small Dialog:
  - Public trip: "Anyone: the map, stops, photos and bikes, 24 hours after each stop is added, and riders'
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
demoting another leader, a group join request, dark-mode toggle, maps with paid styles, offline map downloads.

## 13. Contract and behaviour dependencies (for `ba` / `architect`)

The design doesn't change behaviour. Where a screen needs something the ADR doesn't specify, it is flagged here.
Specs use placeholder paths written as `TBC:` until `docs/api-contract.md` fixes them.

| # | Need | Used by | Why it matters |
|---|---|---|---|
| C1 | Exact paths and shapes for signup, login, logout, current user, change password, sign out everywhere, and (optionally) edit display name | signin, signup, account | ADR §3 describes the behaviour but names only `/api/v2/auth/recover` |
| C2 | **Write paths for new trips (NULL slugs):** stops, photos, bikes by trip id | add-stop, bikes, offline queue | ADR §7 keeps the legacy slug write routes, but app-created trips have no slug. The queue payload today carries `slug`. **Blocker for rider capture on new trips** |
| C3 | "My trips" list for the signed-in user (member, leader, pending) | rider-home (Your trips) | No endpoint in the ADR; Discover lists public trips only |
| C4 | The requester's own join-request status (pending / rejected-until / blocked / cancelled) | join-request | `viewer.role` has no rejected/blocked values. Without it the UI can only learn from the 409 message after tapping |
| C5 | `publicDelayHours` in `TripOut` for members (leaders at least) | trip-settings, "Public from…" chip | ADR §1 defines the field, not its exposure |
| C6 | Join-request, member, promote, step-down, leave, unblock, bulk endpoints and pending count | leader-review, members | ADR §5–6 defines the rules, not the paths. Rider "leave" is not in the ADR |
| C7 | Trip settings update (visibility, delay) | trip-settings | Publish/unpublish is described, no path given |
| C8 | Legacy `GET /api/trips/{slug}` `access` value once slugs grant nothing | legacy-link | The UI must not show Add stop from a rider slug alone |
| C9 | `DisplayNamePrompt` is superseded by the account's `displayName` (server sets `uploadedBy`, ADR §7) | trip shell | Removing the prompt is a behaviour change for `ba` to scope |
| C10 | `/` stops auto-redirecting to the last slug (becomes Discover + "Your trips") | discover | The ADR makes `/` Discover. `ba` confirms the redirect is dropped (D7) |
| C11 | The queue shows "held for another account" and "Sign in to send N" | QueueNotice | ADR §3 behaviour; needs the queue to expose held/paused counts to the UI |
| C12 | Username rules (length, characters) | signup | Not in the ADR. The UI shows server messages until the contract says |
| C14 | Offline read of the continue trip by **trip id** (today's persisted record is keyed by slug), and whether to persist "Your trips" too | rider-home, trip-detail | The design only needs the existing one-trip persistence re-keyed. Persisting more is an Entry 19 change |
| C15 | Keyboard "Use map centre" button on the add-stop map fallback | add-stop | A new control reusing the same manual-location path (`locationSource: "manual"`). Needed for WCAG 2.1.1. `ba` scopes it |
| C13 | **Joining a new private trip:** a private trip is 404 to non-members, so they can't reach its join page; the only ADR routes in are a legacy rider-link claim or the leader publishing | join-request, create-trip | Product gap (no invite mechanism). The design doesn't invent one. `ba`/`architect` decide |

## 14. Open decisions (owner)

| # | Decision | Options and trade-offs | Recommendation |
|---|---|---|---|
| **D1** | **Styling approach** (locked stack) | **(a) Plain CSS + custom properties**: one global `tokens.css` + `base.css`, then per-component class files imported next to each component (BEM-lite names like `.btn`, `.btn--primary`). *Pros:* no dependency, the tokens here map 1:1 to `--color-brand-primary` etc., smallest bundle, easy to review. *Cons:* global namespace, so a naming discipline is needed. **(b) CSS Modules:** built into Vite, so still no new dependency. *Pros:* scoped class names, same tokens via custom properties. *Cons:* a bit more ceremony; tests that query by class need care. **(c) Tailwind:** new build dependency and config. *Pros:* matches Stitch's HTML export, fast to prototype. *Cons:* a locked-stack change, the token names get restated in the Tailwind config, long class strings in JSX, and a harder design-review diff | **(a) Plain CSS with custom properties** in a single global stylesheet + per-component classes. (b) is an acceptable upgrade with no dependency cost. **Needs owner decision** (the orchestrator has the Architect rule on it) |
| D2 | Dark mode | In or out | **Out for v1** (§4) |
| D3 | Web font | System stack vs a hosted/self-hosted font | **System stack**; no web font |
| D4 | Icon source | In-house inline SVG vs vendored paths from an ISC/MIT set | **In-house** 16-icon set; vendoring (no package) acceptable with licence notice |
| D5 | Map theme | Stock OSM tiles vs a styled provider (key, limits, dependency) | **Stock OSM**. Revisit after the trip |
| D6 | Bottom "Add stop" bar vs floating button | Full-width bar (clear label, glove-friendly) vs round FAB | **Full-width bar** |
| D7 | `/` for a returning rider | Discover with "Your trips" on top vs auto-redirect to the last trip | **Discover + Your trips**; one tap, no surprise redirects. `ba` confirms (C10) |
| D8 | Stitch generation | Owner runs `docs/design/stitch/HANDOFF.md` locally | Optional; the specs here are sufficient to build |

## 15. Handoff checklist (for `qa`)

Global
- [ ] Every colour in the built CSS comes from a §4.1 token (`grep` the stylesheet for hex values outside `tokens.css`).
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
- [ ] Rider: Add stop bar present; Members tab absent.
- [ ] Leader: Members tab with a count badge when requests are pending; a settings icon; no Revoke or Demote control on
  another leader's row.
- [ ] Private or unknown trip as a non-member: identical page text for both (compare the DOM text).

States
- [ ] Cold start: the "Waking up the server…" notice appears only after 3 s of loading.
- [ ] Offline with a queue: the Offline notice and "Waiting to send: N stops, M photos" both show; nothing claims "sent".
- [ ] 401 on the queue: "Sign in to send N items" with a Sign in button; the entries remain "Waiting", not failed.
- [ ] Revoked: failed photo rows show "Save photo to this device" and it downloads a `.jpg`; Dismiss on an unsaved
  photo asks for confirmation.
- [ ] 429 on sign-in: "Try again in N minutes" and submit disabled until then.
- [ ] Held entries for another account: the count shows, and no username is shown.

Privacy
- [ ] The Publish dialog shows the first-stop/home warning and the 24-hour delay fact.
- [ ] Viewer captions state the delay; the member notice states "You see stops live".
- [ ] No username appears anywhere a non-member can see it (Discover, trip, stop, photo credit).

Onboarding
- [ ] The Discover onboarding card contains the exact line "Anyone can view public trips. Adding stops and photos
  needs an account and a leader's approval."
- [ ] iOS Safari (not standalone) shows "sign in inside the installed app" on `/signin` and `/signup`.
