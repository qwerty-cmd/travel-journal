# Stitch handoff: Bike Trip Journal redesign

> Written by `designer`, 2026-09-29, for the redesign that follows decision-log **Entry 29** (the access-model ADR).
> Design source of truth: `docs/design/DESIGN.md` and `docs/design/screens/*.md`. Stitch output is **input** to those
> specs. It never replaces them.

## 1. Was Stitch available in the session that wrote this?

**No: Stitch was not used.** In the cloud session that produced `docs/design/`:
- no `mcp__stitch__*` tools were in the agent's tool list;
- none of the Stitch skills (`enhance-prompt`, `design-md`, `stitch::generate-design`,
  `stitch::manage-design-system`) were in the available-skills list;
- the Stitch MCP server is configured at user level on the owner's machine, not in this cloud environment.

No Stitch project, screen id or export exists for this work. Every file under `docs/design/` was written by hand
from the code, the ADR, `docs/user-guide.md` and `docs/real-device-test-plan.md`. The mockups in
`docs/design/mockups/` are hand-written HTML, not Stitch exports.

## 2. Steps for the owner (VS Code, Claude Code, local machine)

1. **Check the MCP server.** In a terminal in the repo: `claude mcp list`. Confirm a `stitch` server is listed and
   connected. In a Claude Code session, `/mcp` shows its tools. **Copy the exact tool names it shows** (they look
   like `mcp__stitch__<tool_name>`). This document doesn't guess them, because they depend on the server version.
2. **Check the skills.** Confirm the user-level `google-labs-code/stitch-skills` install provides `enhance-prompt`,
   `design-md`, `stitch::generate-design` and `stitch::manage-design-system` (they appear in the session's skills
   list). Don't use `stitch-loop`, `react-vite-dashboard` or `shadcn-ui`.
3. **Give the designer agent the tools.** Edit `.claude/agents/designer.md` (the orchestrator owns this file under
   decision-log Entry 12; you, the owner, can edit it directly). In its front-matter `tools:` list, append each Stitch
   tool name exactly as `/mcp` printed it, e.g.
   `tools: Read, Grep, Glob, Write, Edit, WebSearch, WebFetch, Skill, mcp__stitch__<tool_a>, mcp__stitch__<tool_b>, …`
   Keep every existing entry. Don't add Bash. Don't put the API key anywhere in the repo; it stays in your
   user-level MCP config.
4. **Start a fresh Claude Code session** (agent definitions load at start) and ask the orchestrator to spawn
   `designer` with a task like:
   > "Run the Stitch loop from `docs/design/stitch/HANDOFF.md` §3–§6: upload `docs/design/DESIGN.md` as the theme,
   > generate prompts P01–P16, export to `docs/design/stitch/<project>-<date>/`, run the §7 review checklist, and fill
   > in the §8 mapping table. Sample content only."
5. **Review** the exports against §7 before any spec is updated from them.

## 3. Theme first: `stitch::manage-design-system`

Before generating anything, upload the design system so every screen starts on-brand:
1. Create a Stitch project named **`btj-redesign`**.
2. Run `stitch::manage-design-system` with `docs/design/DESIGN.md` as the design-system source and apply it to the
   project as its theme. DESIGN.md contains only tokens, rules and sample copy: no slugs, names, URLs or `.env`
   values. Re-check that before uploading if the file has changed.
3. If the skill takes a shorter DESIGN.md, give it §4 (visual system) and §5 (components) only.

## 4. Generation rules (apply to every prompt)

- Run each prompt through **`enhance-prompt`** first, then **`stitch::generate-design`**.
- One screen per prompt. Make **variants** (rider vs viewer, online vs offline, state) as separate screens via the
  prompt's "Variants" line, not crammed into one.
- Phone frame **390 × 844** first; ask for the ≥ 1024 variant only where the prompt says so.
- **Sample content only.** Trip names such as "Desert Loop (sample)"; people "Sam", "Alex", "Jo", "Kim"; fake
  coordinates such as -26.51234, 133.20871. Never a real trip link or slug, rider name, location trail, photo, or any
  `.env` value. No real photos: grey or gradient placeholders.
- Tell Stitch **not** to add: likes, comments, shares, follows, avatars/profile photos, search, filters, notifications,
  chat, email fields, social sign-in, dark mode (DESIGN.md §12).

## 5. Prompts (enhance-prompt-ready)

Each prompt begins with the same **token line**. It's repeated in full so each one can be pasted alone.

> **Token line (included in each prompt below):** Mobile PWA, 390 px wide, light mode only. System font stack
> (system-ui). Colours: brand primary #1f5c40 (buttons, links, active tab), pressed #164430, brand-subtle #e3efe8,
> page background #f4f6f3, cards #ffffff, sunken #eceeeb, text #17201b, muted text #4d5a52 (never lighter), field
> borders #6b776f 2 px, dividers #d5dbd6, danger #b3261e on #fbe9e7, warning text #7a4b00 on #fdf0d5, dark notice
> #17201b with white text, focus ring #1d4ed8 3 px with 2 px offset. Type: title 24/30 bold, heading 20/26 semibold,
> body 17/24, small 15/20. Spacing on a 4 px grid, page padding 16. Radii 6 / 10 / 16. All tap targets at least
> 48 px tall; primary bottom buttons 56 px. Icons: simple 24 px outline, 2 px stroke. Every status shows an icon and
> a word, never colour alone. High contrast for outdoor sunlight. No avatars, likes, comments, share buttons, search
> or social sign-in.

**P01: Discover (`/`)**
[Token line] Screen: "Discover", the public list of bike trips. Top bar: wordmark "Bike Trip Journal" left, "Sign in"
text button right. An intro card titled "Follow a bike trip" with the sentence "Anyone can view public trips. Adding
stops and photos needs an account and a leader's approval." and "Pick a trip below to follow it. No account
needed.", buttons "Create an account" (outlined) and "Sign in" (text), and a close X. Section "Public trips" with the
subtitle "Public trips, most recently updated first." Three trip cards: name, a green "Public" badge with a globe
icon, "Started 12 Oct 2026 · 3 riders", "Last public stop 2 days ago". A full-width outlined "Show more trips"
button and a text link "Have an old trip link?". Variants: (a) signed out as described; (b) signed in: an account
icon replaces Sign in, a "Your trips" section on top with an emphasised card (role badge "Rider", a full-width green
"Add stop" button), a "Leader" card with "3 requests to review", a "Pending" card; (c) loading skeleton cards;
(d) empty: "No public trips yet".

**P02: Trip detail, public viewer (`/trips/$tripId`)**
[Token line] Screen: one bike trip for an anonymous public viewer. Top bar: back arrow, trip name, "Sign in". Header
card: H1 "Desert Loop (sample)", "Starts 12 Oct 2026 · 3 riders", "Public" badge, link "Who can see this trip?",
link "Riding on this trip? Sign in to ask to join". Tabs: Map (active, green underline), Timeline, Bikes. Caption
with a clock icon: "Stops appear here 24 hours after they're added." A map card 300 px tall with a dark-green trail
line with a white halo, filled green pins, one hollow dashed pin meaning approximate location, and a "Latest" label;
OpenStreetMap attribution bottom-right. "Latest stops" list: a vertical timeline rail, stop name bold, "Mon 13 Oct,
4:05 pm ACST", one row ending " · approximate location". No add or edit controls. Variants: (a) as described; (b)
≥ 1024 px wide: split view, map left 7 columns, timeline right 5 columns, tabs "Journey · Bikes"; (c) offline: a dark
notice "You're offline…", the map area a grey grid with "Map needs a connection".

**P03: Trip detail, rider (`/trips/$tripId/timeline`)**
[Token line] Screen: the same trip for an approved rider, Timeline tab. The header shows "Public" and "Rider" badges.
A light-green notice: "You see stops live. The public sees them after 24 hours." Four timeline stops; the newest has
a small grey chip "Not public yet". A fixed bottom bar with a full-width 56 px green "Add stop" button with a plus
icon. Variants: (a) online; (b) offline with a queue: a dark notice "You're offline. New stops are saved on this
phone and sent later." and a light-green notice "Waiting to send: 1 stop, 2 photos" with a "Details" text button;
(c) leader: a "Leader" badge, a settings icon in the header, a fourth tab "Members" with a round count badge "3".

**P04: Bikes tab (`/trips/$tripId/bikes`)**
[Token line] Screen: trip Bikes tab. Cards per bike: rider display name (heading), "2019 Make Model", multi-line
specs. Variants: (a) viewer: no controls; (b) rider: an outlined "Add bike" button at the top and "Edit" on each
card; (c) rider offline, form error "You're offline — try again when connected." with the input kept.

**P05: Stop detail and gallery (`/trips/$tripId/stops/$stopId`)**
[Token line] Screen: one stop. Top bar with a back arrow "Back to trip". H1 "Roadhouse fuel stop", meta "Mon 13 Oct
2026, 4:05 pm ACST". Notes paragraph. "Photos (6)" and a 3-column square photo grid with 4 px gaps (grey/gradient
placeholders only). Variants: (a) as described; (b) approximate location: a dashed-ring icon and " · approximate
location"; (c) full-screen photo viewer on near-black: a close X (48 px), "3 of 6", previous/next buttons, caption
"Photo by Sam · 4:02 pm ACST", the image fitted whole; (d) "No photos yet"; (e) offline "Photos need a connection."

**P06: Sign in (`/signin`)**
[Token line] Screen: "Sign in". Fields with visible labels above: "Username", "Password" with a show/hide eye button
inside the field. A full-width 56 px green "Sign in" button. Links: "Forgot your password? Use your recovery code",
"New here? Create an account". Variants: (a) default; (b) error summary in red "Username or password is incorrect.";
(c) rate limited: an amber notice "Too many attempts. Try again in 12 minutes." with the button disabled;
(d) iOS Safari: a light-green notice "On iPhone, add this app to your Home Screen and sign in inside the installed
app. The installed app keeps its own sign-in and its own unsent stops, separate from Safari."

**P07: Reset with recovery code (`/recover`)**
[Token line] Screen: "Reset your password". Body "Enter your username and the recovery code you saved when you
created your account." Fields "Username", "Recovery code" (monospace, helper "Spaces don't matter."), "New password"
(helper "15 to 128 characters…"). Green "Reset password" button. Small muted help: "Lost your recovery code too? Ask
the person who runs this site to reset your account." Variant: success step, identical to P09 but titled "Save your
new recovery code".

**P08: Sign up (`/signup`)**
[Token line] Screen: "Create an account". Intro "An account lets you add stops and photos to a trip once a leader
approves you. You don't need one to follow public trips." Fields: "Display name" (helper "Shown on photos you add
and to trip leaders."), "Username" (helper "Private. Only used to sign in."), "Password" with an eye toggle, helper
"15 to 128 characters. A short sentence you'll remember works well." and a live hint "5 more characters needed". A
56 px green "Create account" button. Link "Already have an account? Sign in". No strength meter, no email field.
Variants: (a) default; (b) field error "That username is taken." in red under Username with a red border.

**P09: Recovery code shown once (`/signup`, step 2)**
[Token line] Screen: a success notice with a check icon "Account created. You're signed in as Sam." H1 "Save your
recovery code". Body "If you forget your password, this code is the only way back in. We can't show it again." A grey
sunken block with a monospace code in groups of four (use the obviously fake "SAMP LE00 CODE 4X7Q 9KDM 2RTW").
Buttons "Copy code" (outlined) and "Download as text file" (text, download icon). Tips list. Checkbox "I've saved my
recovery code". A 56 px "Continue" button, disabled grey, with the helper "Tick the box once you've saved the code."
above it. Variant: checkbox ticked, Continue green.

**P10: Account (`/account`)**
[Token line] Screen: "Account". Identity card: "Display name: Sam, shown on photos you add", "Username:
sample-rider-07, private. Only you see this." "Your trips" compact list with role badges. "Password" card with
Current and New password fields and a "Change password" button, helper "Changing your password signs you out on all
other devices." "Sign out" (outlined) and "Sign out everywhere" (red outline). No avatar, no delete account.

**P11: Create trip (`/trips/new`)**
[Token line] Screen: "Create a trip". Fields "Trip name" (helper "For example: Spring loop 2026"), "Start date". A
fieldset "Who can see it?" with two large radio cards: "Public" (globe, selected, green border and light-green fill:
"Anyone can find it and follow along. Stops appear publicly 24 hours after they're added.") and "Private" (lock:
"Only members can see it."). An amber warning: "Check your first stop. Trips often start at someone's home. Public
viewers see each stop's exact location." A 56 px green "Create trip" button. Variant: after creation, the trip screen
with a "Leader" badge, the success notice "Your trip is ready. You're its leader." and a "Next steps" card with
"Copy trip link".

**P12: Request to join (`/trips/$tripId/join`)**
[Token line] Screen: "Ask to join this trip" for "Desert Loop (sample)" (Public badge). Body "A leader of this trip
will see your display name, Sam, and your message. Once they approve you, you can add stops and photos." A numbered
how-it-works list. Textarea "Message to the leaders (optional)" with the counter "55 / 280". A 56 px "Send request"
button. Variants: (a) form; (b) pending: an amber "Pending" badge, H1 "Request sent", a light-green notice "Waiting
for a leader to approve you. You'll see Add stop on the trip once you're approved.", the quoted message, "Sent Fri 10
Oct, 9:12 am ACST", "Back to the trip" and "Cancel request"; (c) rejected: an amber notice "A leader didn't approve
your request. You can ask again 7 days after it was declined." with no form; (d) blocked: "You can't request to join
this trip…".

**P13: Leader review (`/trips/$tripId/members?view=requests`)**
[Token line] Screen: the trip Members tab for a leader. A segmented control "Requests 3 · Members · Blocked". Muted
intro "Approve people you know are riding…". Request cards: checkbox + display name ("Alex"), "Requested 2 hours ago",
a quoted message, a grey badge "Via old rider link" on one, buttons "Approve" (green) and "Reject" (outlined), and a
"More options" (three dots) button. A bulk bar with a green border: "2 selected", "Approve 2", "Reject 2". Variants:
(a) with selection; (b) empty "No requests right now" with "Copy trip link"; (c) a bottom-sheet confirm dialog
"Reject and block Kim?" with bullets and a red "Reject and block" button over an outlined "Cancel" that shows a blue
focus ring.

**P14: Members management (`/trips/$tripId/members?view=members`)**
[Token line] Screen: segmented "Members" selected. "Leaders (2)": your own row "Sam (You)" with "Step down" (red
outline) and "Leave trip" (red text link); the other leader's row has no buttons and a muted line "Leaders can't
remove each other." "Riders (3)": rows with "Make leader" (outlined) and "Revoke" (red outline). Variants: (a) default;
(b) you're the only leader: Step down and Leave disabled with the text "You're the only leader. Make someone a leader
first."; (c) bottom-sheet confirm "Remove Sam as a rider?" listing the consequences, with a red "Remove Sam".

**P15: Add stop (`/trips/$tripId/add`)**
[Token line] Screen: "Add stop" with an X close button. A location status card. Fields "Name (required)" (helper
"e.g. Roadhouse fuel stop"), "Notes", a "Photos" heading and a full-width outlined 56 px "Add photos" button with a
camera icon. A fixed bottom bar with a full-width 56 px "Save stop" button. Variants: (a) "Getting your location…"
with a crosshair icon, Save disabled grey, and the helper "Add a name and a location to save."; (b) GPS unavailable:
an amber card "GPS unavailable: tap the map to set the location" / "Or pan the map and tap Use map centre.", a 280 px
map on a grey grid with a centre crosshair and a hollow dashed pin, an outlined "Use map centre" button, "Location set
on the map · -26.51234, 133.20871 · Shown as approximate location.", two 72 px photo previews each with a round
remove X, Save green, and above it "This stop is visible to the public 24 hours after it's saved."; (c) "Location
found (GPS)", "Processing 1 photo…", Save disabled with "Wait for photos to finish processing."

**P16: Global states**
[Token line] Screen set, each a phone frame: (a) "Waking up the server…" with a clock icon and "The first visit after
a quiet spell can take a little while." over skeleton cards; (b) "Can't reach the server" with a cloud-off icon and
"Try again"; (c) "Trip not found" with "Check the link. If this is a private trip, sign in with an account that
belongs to it.", "Go to Discover" and "Sign in"; (d) a status stack under the top bar: an amber "Sign in to send 3
items" with a Sign in button, then a red group "Your rider access was removed" with rows "Photo for Camp by the dry
lake couldn't be sent / You're no longer a rider on this trip", buttons "Save photo to this device" (download icon)
and "Dismiss"; (e) a light-green "2 items were saved by another account on this phone. Sign in as that account to
send them."; (f) a dark toast "Request sent" at the bottom.

## 6. Extract and save

1. After generating, run **`design-md`** on the `btj-redesign` project to produce Stitch's DESIGN.md.
2. Save the exports to **`docs/design/stitch/btj-redesign-<YYYY-MM-DD>/`**:
   - `DESIGN.md` (Stitch's extraction, kept verbatim, **not** a replacement for `docs/design/DESIGN.md`);
   - `P01-discover.html`, `P02-trip-public.html`, … (one HTML file per generated screen, named by prompt id);
   - `screens.md`: a list of prompt id → Stitch screen reference (as shown in Stitch).
   Don't save Figma files or screenshots containing anything but sample content.
3. Stitch's HTML uses Tailwind. Treat the classes as a visual reference only until the owner rules on the styling
   approach (DESIGN.md §14 D1).

## 7. Review checklist for the generated screens

Run this on every exported screen, then note the results in `screens.md` next to each prompt id.

- [ ] **Tokens:** every colour in the export is in DESIGN.md §4.1. List any new colour and either map it to a token
  or reject it.
- [ ] **Contrast:** body text ≥ 4.5:1, borders and focus ≥ 3:1. Muted text is never lighter than #4d5a52.
- [ ] **Target size:** every tappable element ≥ 48 px tall (primary bottom actions 56 px).
- [ ] **Status not by colour alone:** badges and notices have an icon and a word.
- [ ] **Audience correctness:** viewer frames have no Add stop, Members, settings or edit controls; the pending
  frame has no Add stop; other leaders' rows have no remove/demote.
- [ ] **Copy:** the ADR sentence on Discover is verbatim; times carry a zone label; no "slug", "session" or HTTP codes.
- [ ] **Privacy:** no username outside Account and sign-in; the 24-hour delay is stated where DESIGN.md §9 says.
- [ ] **No invented features.** Anything Stitch added goes under "Not in scope — invented by Stitch" in `screens.md`
  and is **not** carried into any spec: likes, comments, shares, follows, avatars, search, filters, notifications,
  chat, email, social sign-in, dark mode, per-stop hide, photo delete, stop edit, trip cover images, weather widgets,
  distance or speed stats, route planning.
- [ ] **Sample data only:** no real names, trip links, coordinates of real homes, or real photos.
- [ ] **Translate, don't copy:** any accepted improvement updates `docs/design/DESIGN.md` or
  `docs/design/screens/*.md` in our format (Design feature → Design format → States → APIs called), by `designer`.

## 8. Mapping table (owner fills the last two columns)

| Prompt id | Screen | Route | Spec | Stitch project | Stitch screen reference |
|---|---|---|---|---|---|
| P01 | Discover (+ Your trips) | `/` | `screens/discover.md`, `screens/rider-home.md` | | |
| P02 | Trip detail, public viewer | `/trips/$tripId` | `screens/trip-detail.md` | | |
| P03 | Trip detail, rider / leader | `/trips/$tripId/timeline` | `screens/trip-detail.md` | | |
| P04 | Bikes tab | `/trips/$tripId/bikes` | `screens/trip-detail.md` | | |
| P05 | Stop detail and gallery | `/trips/$tripId/stops/$stopId` | `screens/stop-detail.md` | | |
| P06 | Sign in | `/signin` | `screens/signin.md` | | |
| P07 | Reset with recovery code | `/recover` | `screens/signin.md` | | |
| P08 | Sign up | `/signup` | `screens/signup.md` | | |
| P09 | Recovery code shown once | `/signup` (step 2) | `screens/signup.md` | | |
| P10 | Account | `/account` | `screens/account.md` | | |
| P11 | Create trip + initial leader state | `/trips/new` | `screens/create-trip.md` | | |
| P12 | Request to join + pending | `/trips/$tripId/join` | `screens/join-request.md` | | |
| P13 | Leader review | `/trips/$tripId/members?view=requests` | `screens/leader-review.md` | | |
| P14 | Members management | `/trips/$tripId/members?view=members` | `screens/members.md` | | |
| P15 | Add stop + photos | `/trips/$tripId/add` | `screens/add-stop.md` | | |
| P16 | Global states | (all) | `screens/global-states.md` | | |
| (none) | Trip settings + Publish dialog | `/trips/$tripId/settings` | `screens/trip-settings.md` | | |
| (none) | Legacy link landing | `/t/$slug` | `screens/legacy-link.md` | | |

Trip settings and legacy landing have no prompt because they're small variations of existing layouts. Add P17/P18
if you want them generated.
