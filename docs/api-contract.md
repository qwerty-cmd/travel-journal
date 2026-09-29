# API Contract — Session 1

The consolidated, human-readable record of the API contract agreed in Session 1 (spec Section 10). It sits alongside the Pydantic models in `backend/app/models/` — the models are the machine-readable contract, this file is the reasoning behind them.

Status: **settled**. Weeks 2–3 build against this. Once route handlers exist, FastAPI's auto-generated OpenAPI spec becomes the always-in-sync version of the same information (and is what Kubb generates the frontend client from); this document stays as the record of *why* the shapes are what they are.

**Accounts and membership (decision-log Entry 29, 2026-09-29).** The access model below is no longer "two slugs, no accounts". Entry 29 adds accounts, cookie sessions, public/private trips, per-trip membership and the `/api/v2` surface. The sections that carry it are "Access: public trips, members and leaders", "Sessions", "CSRF", "Rate limits and lockout", "Idempotency: additions", the updated "Error envelope", the v2 endpoint tables, "Access control: 401, 403 and 404 are three different answers" and "Data model: migration 0003". That contract is **written but not yet built** (milestone `m5-accounts-membership`). Until `t-am-write-gate-legacy` lands, the running code still implements the slug-only model.

This file deliberately does not restate every field of every model — field lists drift the moment code changes. It names models and points at the file they live in. The endpoint table is the one exception, and it names models, not fields.

---

## Context

Spec Section 5 lists eight endpoints as a rough sketch. This contract turns that sketch into something both sides can be built against independently: request/response models, status codes, one error shape, and a defined answer to "what happens when the offline queue retries a write the server already accepted."

That last question is the reason this document exists at all. The app is used on the Stuart Hwy with long dead zones — a write is captured on the device first and sent later, sometimes much later, sometimes twice. If the contract didn't pin down retry behaviour, the frontend's offline queue and the backend's route handlers would each invent their own answer, and the disagreement would only show up as duplicate stops in the finished trip journal.

The build gate (spec Section 10, CLAUDE.md) is architecture → contract → build. This document closes the contract stage for milestone `m2-core-api`.

---

## How it works

### Access: public trips, members and leaders

*Replaces the pre-Entry 29 section "Access: two slugs, no accounts" (a slug was the whole authorisation: rider slug read + write, viewer slug read only). Code docstrings and route descriptions that still cite that title describe the running slug-only implementation until `t-am-write-gate-legacy` rewrites them.*

**Ruling:** decision-log Entry 29 (Architect ADR, 2026-09-29, accepted by the orchestrator). It supersedes spec §2 "Friend access: two links" and reopens Entry 21. A slug is no longer a write credential for anything.

Access answers two separate questions:
- **Who is asking?** The session cookie answers this.
- **What may they do on this trip?** Their row in `trip_members` answers this.

**Trip visibility.** `trips.visibility` is `public` or `private`.
- `POST /api/v2/trips` creates trips as `public` unless the body says otherwise.
- Every trip that existed before migration `0003` is `private`. Its riders shared it under the old link-only terms, so publishing it takes a leader's explicit change.

**Roles relative to one trip** (`ViewerRole`, reported in `TripOut.viewer.role`):

| Role | Who | Reads | Writes |
|---|---|---|---|
| `anonymous` | No valid session | Public trips, delayed | None |
| `none` | Signed in, with no active membership and no pending request. This includes revoked, rejected, blocked and self-departed users. | Public trips, delayed | None |
| `pending` | Signed in, with a pending join request on this trip | Public trips, delayed | None |
| `rider` | Active membership with role `rider` | Everything, no delay | Stops, photos, bikes. Can leave. Can list members. |
| `leader` | Active membership with role `leader` | Everything, no delay | Rider writes, plus: trip settings, deciding requests, promoting, revoking riders, stepping down |

**Public delay.** `trips.public_delay_hours` is an integer from 0 to 168 and defaults to 24.
- A non-member sees a stop only when `arrivedAt <= now() - public_delay_hours`, evaluated on the server clock at request time.
- A photo is visible to a non-member only when its stop is visible. `takenAt` plays no part.
- `/map` builds its pins and its trail from visible stops only, and draws the trail only when 2 or more are visible.
- `lastPublicStopAt` is the latest `arrivedAt` among visible stops. It is the same value for every caller, members included.
- Members see everything with no delay.
- A stop hidden by the delay returns the same `404` as a stop that doesn't exist.
- Bikes are not delayed.

**Private means invisible, not forbidden.** A private trip answers a non-member with a `404` that is **byte-identical** to the one for a trip id that doesn't exist. Same body, same message constant, same headers. A trip id is non-secret, so anything else would let anyone test whether a private trip exists.

**Never in any response a non-member can receive:** usernames, user ids, member lists, join-request lists, anything to do with sessions, either slug.

**The gates** (`backend/app/core/security.py`). Every route declares exactly one of these:

| Gate | Checked in order | Used by |
|---|---|---|
| none (anonymous) | — | signup, signin, signout, recover, `GET /api/v2/trips` |
| `require_session` | valid session, else `401` | `/api/v2/auth/*` (signed-in routes), `/api/v2/me/*`, `POST /api/v2/trips`, claim, cancel |
| `require_trip_reader` | the trip exists and is either public or the caller is an active member, else `404`. **Never `401`.** | v2 GET reads |
| `require_trip_member_read` | session, else `401` → trip exists and is either public or the caller has *any* membership row (active or revoked), else `404` → active member, else `403` | `GET .../members` |
| `require_trip_writer` | session, else `401` → trip located (rule below), else `404` → active membership as `rider` or `leader`, else `403` | rider writes, leave |
| `require_trip_leader` | same as writer, but the role must be `leader`, else `403` | leader actions, leader reads |
| `require_trip_access` (legacy, reads only) | either slug, else `404`. The session is optional and only fills `viewer`. | legacy GETs |

Membership is read from Postgres on every request, with no cache. A revocation takes effect on the next request.

**Where the session check sits depends on how the trip is located:**
- **v2 (`{tripId}`): session first.** An anonymous unsafe request gets `401` for every trip id, existing or not, so the `401` tells an attacker nothing. After that, a trip is located only if it exists and is either public or the caller has any membership row on it (active or revoked). Anything else gets the byte-identical `404`. Checking the trip first would give an expired session a `404` on a private trip. `404` is never-retry, so the queue would permanently fail items that only needed the rider to sign in.
- **Legacy (`{slug}`): slug first**, then session (`401`), then membership (`403`). This is the ADR's order unchanged. A trip found by slug is never hidden, so this order cannot reveal anything.

**Legacy slug routes locate the trip. They don't authorise anything.**
- `POST /api/trips/{slug}/stops`, `/stops/{stop_id}/photos`, `/bikes` and `PATCH /bikes/{id}` keep their paths so that queued items from before the upgrade still reach them.
- Either slug finds the trip. Access then goes through the same membership gate as v2.
- Legacy GETs still accept either slug and give full, undelayed reads. They stay until the legacy removal task (Entry 29 §12).

**`TripOut.viewer.role` and `TripOut.access` only tell the UI what to show.** The server enforces access on every write no matter what the UI rendered.
- `access` is deprecated. It is `"rider"` when `viewer.role` is `rider` or `leader`, and `"viewer"` otherwise. It is kept so that clients from before the upgrade show write UI only to members.

### Sessions

- **Identity.** An account has:
  - a `username`: unique, private, stored in lowercase;
  - a `displayName`: public;
  - a password: argon2id at the OWASP minimum (m=19 MiB, t=2, p=1), hashed in a worker thread behind `asyncio.Semaphore(2)`.
- **Cookie.** `__Host-btj_session=<token>; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=7776000`.
  - The token is 32 random bytes, base64url-encoded.
  - Only its SHA-256 is stored, as `sessions.token_hash`.
  - There is no signing secret and no environment variable.
  - It is a cookie, not a bearer token, so JavaScript can't read it and it never enters an IndexedDB queue entry. The existing client already sends it (`credentials: 'same-origin'`).
- **Lifetime.** 90 days sliding, capped at 365 days from sign-in.
  - `last_used_at` is written at most once every 24 h. Each such write also re-issues the cookie with a fresh `Max-Age`.
  - A session is valid when `now < last_used_at + 90 days` and `now < absolute_expires_at`.
- **Revocation.**
  - `signout` deletes the current row.
  - `signout-all`, a password change, a successful recovery, an operator reset and an account disable each delete **all** of that user's rows.
  - A password change and a recovery then issue one fresh session to the device that made the request.
- **Missing, unknown, expired or deleted token.** The answer is `401 UNAUTHENTICATED`. If a cookie was sent, the response also clears it with `Max-Age=0`.
- **`WWW-Authenticate`.** RFC 9110 requires it on a `401`, so every 401 carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`. This scheme does not trigger a browser login dialog.
- **Recovery code.** 128 random bits, shown **once**: at signup, after a recovery, and after a rotation.
  - It is 26 Crockford base32 characters.
  - On input it is case-insensitive, hyphens and spaces are ignored, and `O`, `I` and `L` are read as `0`, `1` and `1`.
  - Only its SHA-256 is stored.
  - It is single-use: a successful recovery replaces it.
- **Nothing sensitive in logs or messages.** No password, recovery code, session token or cookie header appears in any log line, error message or response other than the one that issues it.

### CSRF

A pure-ASGI middleware (`core/csrf.py`) checks every `POST`, `PUT`, `PATCH` and `DELETE` whose path starts with `/api`:
1. If `Sec-Fetch-Site` is present, it must be `same-origin` or `none`.
2. If it is absent, `Origin` must be present and its host and port must equal the `Host` header. The scheme is ignored because TLS terminates at the ingress. `Origin: null` fails.
3. A failure gets `403 FORBIDDEN` with "This request came from another site and was blocked."

This works together with `SameSite=Lax`. No custom header is required, so clients from before the upgrade still pass.

Like `405`, the CSRF `403` is middleware-level. It is reachable on every unsafe `/api` route and is not listed per endpoint.

### Rate limits and lockout

- **Mechanism.** `core/ratelimit.py` keeps in-process token buckets with no new dependency.
  - A bucket of N per window holds N tokens and refills continuously.
  - A request that finds the bucket empty gets `429 RATE_LIMITED`, with `Retry-After` set to the whole seconds until one token is available (at least 1).
- **IP key.** The right-most `X-Forwarded-For` entry, controlled by `TRUSTED_PROXY_HOPS` (default `1`: Azure Container Apps ingress appends one hop). Left-most entries are ignored as spoofable. With no `X-Forwarded-For`, or with `TRUSTED_PROXY_HOPS=0`, the key is the socket address.
- **Restarts.** Buckets reset on restart and on scale-to-zero.

| Bucket | Limit | Key | Applies to |
|---|---|---|---|
| `public-read` | 120/min | IP | every `GET`/`HEAD` under `/api` except `/api/health` |
| `signup-ip` + `signup-global` | 5/hour; 50/day | IP; global | `POST /api/v2/auth/signup` |
| `signin` | 10 per 15 min | IP | signin, recover, password change, recovery-code rotation |
| `trip-create` | 3/day, plus a lifetime cap of 20 trips created (`409`) | user | `POST /api/v2/trips`. **A replay spends no token.** |
| `join` | 10/hour | user | join-request create, claim |
| `writes` | 600/hour | user | every other session-gated unsafe route, legacy and v2 |

`/api/health`, the `/api` catch-all and `signout` have no limiter.

**Per-account lockout (persisted in Postgres, so it survives restarts):**
- Each failed signin, failed recovery, wrong current password and wrong recovery-code rotation password increments `users.failed_logins`.
- At 10 failures, `locked_until = now() + 15 min` and the counter resets.
- A successful signin or recovery resets the counter.
- While the account is locked, signin and recover return `429 RATE_LIMITED`, with `Retry-After` equal to the seconds left, **even for correct credentials**.
- For an unknown username, signin still runs argon2 verification against a fixed dummy hash, so response timing doesn't reveal whether the account exists.

### Idempotency — client-generated ids

**This is the most consequential decision in the contract, and the mechanism that makes the offline queue safe.**

The client generates the entity's `id` — a UUID4 — at capture time. Not at send time: at the moment the rider taps "Add stop" or takes a photo, which may be hours earlier and entirely offline. That id travels in the create request body.

On the server, a create request's id falls into exactly one of three branches:

- **Unseen id** → create the record, return `201`.
- **Id already exists under this same parent** → a replay. Return the **existing** record with `200`. Do not create a duplicate, and do not return an error.
- **Id exists under a *different* parent** → `409` / `CONFLICT`. Nothing is created, and nothing about the conflicting record is disclosed.

This three-way branch applies identically to all three create endpoints. It is stated once here rather than per endpoint, because it follows from the schema rather than from any one route:

- `POST /trips/{slug}/stops` (`StopCreate.id`) — parent is the trip
- `POST /trips/{slug}/stops/{id}/photos` (the `id` **form field**) — parent is the stop
- `POST /trips/{slug}/bikes` (`BikeCreate.id`) — parent is the trip

**Why the third branch exists at all.** Each of `stops`, `photos` and `bikes` has a **global** primary key on `id` plus a *separate* parent foreign key — `stops.trip_id`, `photos.stop_id`, `bikes.trip_id` (`backend/app/data/tables.py`). An id is therefore unique across the whole table, while the parent it belongs to is a different column entirely. So "this id already exists" and "this id already exists *here*" are two different questions, and only the second one means replay.

**The lookup rule.** Replay is matched on the **parent-and-id pair** — `(trip_id, id)` for stops and bikes, `(stop_id, id)` for photos — and **never on `id` alone.** A lookup by `id` alone would happily return another trip's stop through this trip's slug: a cross-trip leak, which spec Section 12 ranks as the top-priority failure class.

**The cross-parent branch is detected by an explicit check, never by letting an `INSERT` fail.** Allowing the primary-key violation to surface from the driver renders `500` / `INTERNAL_ERROR` — and the offline queue branches on `code` to decide retry-vs-never-retry, so it would retry forever a request that can never succeed. The handler looks first and decides which of the three branches applies. The database constraint stays as the backstop it is, not the mechanism.

Why it matters: the failure mode this protects against is not a user double-tapping. It's the connection dropping *after* the server committed the write but *before* the response reached the phone. From the queue's point of view that is indistinguishable from a total failure, so it retries — correctly. Because the id was fixed on the device before the first attempt, the retry carries the same id, and the server recognises it. The queue can therefore retry any create, any number of times, without needing to know whether the earlier attempt actually landed. That is the entire safety argument for retrying writes at all.

A replay returning `200` with the stored record (rather than `204`, or an error) also means the queue always has a real entity to reconcile its local copy against, whichever attempt succeeded.

Note that `id` is typed `str` in the models, with "client-generated UUID4" expressed in the field description rather than in the type. The generating side is the client; the models do not enforce the format.

### Idempotency: additions

The three-way branch (unseen id → `201`, same parent → `200` replay, different parent → `409`) applies to the v2 creates, with the same rules as before: matched on `(parent, id)` and detected by a check, never by a failed INSERT.
- `POST /api/v2/trips/{tripId}/stops`: the parent is the trip.
- `POST /api/v2/trips/{tripId}/stops/{stopId}/photos`: the parent is the stop.
- `POST /api/v2/trips/{tripId}/bikes`: the parent is the trip.
- `POST /api/v2/trips` (`TripCreate.id`): the parent is the **creating user**.
  - The replay is `200` only if `trips.created_by` is the caller **and** the caller is still an active member. Any other existing id is `409`.
  - `TripCreate.id` must be a canonical lowercase UUID, `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`, or the request gets `422`. This is the one create that validates id format, because a trip id becomes a public URL and an S3 key prefix.

**The gate runs before the replay lookup.** A replay from a revoked rider, including an id that is already stored, gets `403` with nothing written to the database or S3. A replay is not an exception to authorisation.

Join requests have no client id. Their idempotency comes from the one-pending-per-(trip, user) partial unique index: a duplicate create while one is pending returns the existing request with `200`.

### Error envelope

Every non-2xx response uses one shape:

```json
{
  "error": {
    "code": "FORBIDDEN",
    "message": "This link is read-only."
  }
}
```

`ErrorEnvelope` / `ErrorDetail` / `ErrorCode` — `backend/app/models/common.py`.

`code` is one of exactly **eight** values. `UNAUTHENTICATED` and `RATE_LIMITED` were added by decision-log Entry 29, whose orchestrator ruling is the `ErrorCode` ruling Entries 6 and 14 require. Until `t-am-contract-models` lands, `backend/app/models/common.py` still has the original six.

| `code` | Meaning |
|---|---|
| `UNAUTHENTICATED` | No valid session where one is required, or the signin/recover credentials are wrong |
| `FORBIDDEN` | Signed in, but not permitted: a non-member or revoked member writing, a non-leader doing a leader action, a leader removing another leader, a wrong current password. Also a cross-site unsafe request blocked by CSRF. |
| `NOT_FOUND` | No such trip, slug, stop, bike, request or member — **or** a private trip the caller may not see |
| `VALIDATION_ERROR` | The request failed validation. This includes a non-JPEG photo and a photo over 15 MiB. |
| `METHOD_NOT_ALLOWED` | The path exists but not for this HTTP method — a client mistake, not a server fault. Unchanged. |
| `CONFLICT` | A client-generated id under a different parent. **Also a state conflict**: username taken, already a member, a request inside the cooldown, a blocked requester, a pending cap, the last leader leaving or stepping down, deciding an already-decided request differently, unblocking a request that isn't blocked, the 20-trip lifetime cap. Each of these fails identically on retry. |
| `RATE_LIMITED` | A rate limit or an account lockout. Always carries `Retry-After`. |
| `INTERNAL_ERROR` | Anything else, including an unhandled server-side exception. Unchanged. |

The `CONFLICT` leak boundary (below) still holds for every one of these meanings: the message contains no value from any other record. A taken username is reported as "That username is taken.", with no echo of the name.

Before Entry 29, `FORBIDDEN` meant only "valid slug, but it's the viewer slug on a write endpoint" and `NOT_FOUND` meant only "no trip has this slug, or the stop/bike id doesn't exist". Those remain true of the running code until the write gate lands.

#### Status → code mapping

This is the mapping the global exception handler implements. It is exhaustive by construction: there is no status a response can carry that does not land on a code.

| HTTP status | `code` |
|---|---|
| 401 | `UNAUTHENTICATED` |
| 403 | `FORBIDDEN` |
| 404 | `NOT_FOUND` |
| 405 | `METHOD_NOT_ALLOWED` |
| 409 | `CONFLICT` |
| 422 | `VALIDATION_ERROR` |
| 429 | `RATE_LIMITED` |
| anything else, including an unhandled 500 | `INTERNAL_ERROR` |

- `ApiError` gains two constructors:
  - `ApiError.unauthenticated(message)` attaches `WWW-Authenticate`.
  - `ApiError.rate_limited(message, retry_after_seconds)` attaches `Retry-After`.
- The handler sends those headers.
- `ERROR_RESPONSES` gains `401` and `429` entries. The `429` entry declares the `Retry-After` response header in OpenAPI.

`METHOD_NOT_ALLOWED` and `INTERNAL_ERROR` were added after the original three proved insufficient to keep the "every non-2xx uses this envelope" promise. Both statuses are reachable today: `POST /api/health` hits a real route that accepts `GET` and `HEAD` only, so it returns a `405`, and an unhandled exception is a `500` by definition. With only three codes, neither had a legal value to report.

`CONFLICT` was added later still, for the same reason and by the same route. A create whose client-generated id already exists under a *different* parent is **neither a validation failure nor a server fault** — the request is well-formed and the server is healthy — so among the five existing codes it had no legal value either. `ba` escalated rather than inventing one, exactly as it had for the first two; the user ruled. See `docs/decision-log.md` Entry 14 for the four readings that were rejected, including the composite-primary-key option that would have made the branch unreachable.

A `405` requires a **registered path with a different method** — it is not what an unregistered path returns. `DELETE /trips/{slug}/stops` returns `405` with `Allow: GET, HEAD, POST`, because the stops router registers `GET` (plus its schema-excluded `HEAD` sibling) and `POST` on that path: Starlette matches the route, and the method is what mismatches. The same request returned `404` for as long as the stops router was an empty `APIRouter` stub with no methods registered — Starlette matched no route at all, so there was nothing for the method to mismatch against. (An earlier revision of this document used that request as the `405` example *while it was still a `404`*; `qa` ran it and found `404`. See `docs/decision-log.md` Entry 6.)

> **Expiry fired (2026-09-15).** The forward-note that stood here warned that this paragraph's
> example had an expiry date, and it has now come due — the paragraph above is the re-worded version.
> `t-stops-list-endpoint` registered `GET` and its `HEAD` sibling on `/trips/{slug}/stops`
> (`3ebded7`), so the path exists and the response is `405` rather than `404`.
> **The new value was verified, not predicted:** `test_spa_delete_on_the_registered_stops_path_is_405`
> asserts `405` with `Allow` exactly `{"GET", "HEAD"}`, and it passes. That verification was the point
> of the old note — `t-405-router-route-collapse` landing is what stops a registered path *silently
> staying* `404` for the wrong reason, and the two failures look identical from the outside.
> The `Allow` set became `{"GET", "HEAD", "POST"}` when `t-stops-create-endpoint` landed (`c80f011`);
> the same test now asserts that exact set.
> The surrounding rule ("a `405` requires a registered path with a different method") did not expire —
> only the example did. Closed as `t-stops-405-doc-revisit`.

`METHOD_NOT_ALLOWED` and `INTERNAL_ERROR` are two codes rather than one catch-all because **a `405` is a client error and a `500` is a server fault** — collapsing them would make the envelope inaccurate about which side went wrong. That distinction is not cosmetic: the offline queue decides retry-vs-never-retry programmatically from `code`, and "the server is broken, try later" and "this request can never succeed as written" are opposite answers.

**Which side of that branch each code falls on is contract, not handler preference:**

- **Never retry** — `VALIDATION_ERROR`, `METHOD_NOT_ALLOWED`, `CONFLICT`. The request cannot succeed as written, however long the queue waits. `CONFLICT` belongs here because a client-generated id that already belongs to another parent will still belong to it on the next attempt; retrying is guaranteed to produce the same `409`.
- **Retry** — `INTERNAL_ERROR`. The opposite answer: the server broke, and a later attempt may work.
- **Never retry** also covers `FORBIDDEN` and `NOT_FOUND`. A write is only queued under a slug the client has already seen resolve to `access: "rider"`, and FIFO order guarantees a photo's stop is sent first. So a `403` or `404` means the slug or parent is gone for good, and waiting cannot change that. When a stop fails never-retry, its queued photos fail with it, without being sent.
- **Retry** also covers any failure without a parseable `ErrorEnvelope`: a network error, a timeout, or a `502`/`503`/`504` HTML page from an intermediary. Classification is by `code` only, and a response with no envelope has no code. The timeout is partly the client's own: the queue aborts a stop create after **30 s** and a photo upload after **120 s** (`STOP_TIMEOUT_MS` / `PHOTO_TIMEOUT_MS` in `frontend/src/offline/queue.ts`), so a request that never settles cannot hold the cross-tab drain lock forever. An abort carries no envelope, so it retries with backoff like any network failure. That is safe for the same reason every retry is: the server may already have committed the write, and the replay returns `200`.

**A never-retry outcome is not a silent drop.** The queued item is **dequeued *and* surfaced to the rider.** This is a data-integrity rule, not a UX nicety: the rider tapped "Add stop", believes that stop was captured, and may be hours from anywhere. Dropping it quietly is a data-loss path — and the rider is the only one who can decide what to do with a capture that cannot be sent as it stands.

"Dequeued and surfaced" means the item moves to a failed state that stays in IndexedDB, **photo blob included**, until the rider dismisses it. It is never deleted automatically. A retrying item that has failed 10 or more times is shown in the offline indicator with its last error, and it keeps retrying. Queue shape and drain rules: `docs/decision-log.md` Entry 19.

#### Offline-queue classification (Entry 29)

This is contract, not a handler preference. It extends the rules above to the two new codes:

| Code | Queue action |
|---|---|
| `VALIDATION_ERROR`, `METHOD_NOT_ALLOWED`, `CONFLICT`, `FORBIDDEN`, `NOT_FOUND` | **Never retry.** Unchanged. The item is marked failed, kept, and shown to the rider with its blob. A revoked rider's items land here with "You're no longer a rider on this trip". A failed photo item offers "Save photo to this device". |
| `UNAUTHENTICATED` | **Pause.** The item is not marked failed and the attempt is not counted. The drain stops and the notice reads "Sign in to send N items". The drain resumes on sign-in and on the usual triggers. |
| `RATE_LIMITED` | **Retry after `Retry-After`** seconds. The attempt is counted. It does not advance the doubling backoff. |
| `INTERNAL_ERROR`, or no envelope | Retry with backoff (5 s doubling to 300 s). Unchanged. |

- Every new entry records the `userId` it was captured under. That is an identifier, not a secret.
- The drain **holds**, and neither sends nor fails, any entry whose `userId` differs from the signed-in user or that has a `userId` while nobody is signed in.
- Entries from before the upgrade carry no `userId` and are sent under whatever session is current.
- **Clients from before the upgrade** (`isNeverRetry` with the old five-code set) treat `UNAUTHENTICATED` and `RATE_LIMITED` as retryable, so they lose nothing.

The `FORBIDDEN`/`NOT_FOUND` reasoning above ("a write is only queued under a slug the client has already seen resolve to `access: "rider"`") becomes, under Entry 29: a write is only queued by a signed-in member, so a `403` means the membership is gone and a `404` means the trip or parent is gone. Neither is fixed by waiting. An expired session is a `401`, never a `404`, because v2 checks the session before locating the trip (see "Access: public trips, members and leaders", above).

#### `message` and the `INTERNAL_ERROR` leak boundary

`message` is human-readable and **documented as safe to show a rider directly**. That guarantee is what makes the single frontend error path possible — the UI renders `message` without sanitising or whitelisting it.

It follows, and the implementation is bound by it, that **the `INTERNAL_ERROR` message is always a fixed generic string.** The originating exception text is logged server-side and never appears in the response body. This is a leak boundary, not politeness: a raw database error can carry the database host, user and query fragments, and the rider-facing error surface is a public one — before Entry 29 the slug in the URL was the only access control there was, and after it anonymous callers read public trips.

The other seven codes carry messages written for the situation, because they describe conditions the caller is allowed to know about.

**A `VALIDATION_ERROR` message never invents a field it cannot name.** A 422 normally renders as a list of `field: what is wrong with it` clauses — `lat: Field required; locationSource: Input should be 'gps' or 'manual'; arrivedAt: Input should have timezone info`. A body that never parsed as JSON has no field list at all: FastAPI reports it as `{"type": "json_invalid", "loc": ("body", <byte offset>)}`, and that offset is a position in the raw bytes, not a field. Rendering the location as a dotted path therefore produced `The request could not be validated. 1: JSON decode error` — a field named `1`, which no rider or client can act on. An unparseable body now renders the fixed clause **`The request body could not be read as JSON.`**, byte-identical whichever offset the parser failed at, and field-level messages are unchanged. Built at `_format_validation_errors` in `backend/app/core/errors.py` — the single place every 422 message is assembled — so all four body-taking routes (`POST /trips/{slug}/stops`, `POST /trips/{slug}/bikes`, `PATCH /trips/{slug}/bikes/{id}`, `POST /trips/{slug}/stops/{id}/photos`) are covered by one guard rather than four. Task `t-validation-message-offset`. An **empty** body is a different FastAPI error type (`missing` with `loc == ("body",)`) and used to render a bare `Field required` with no subject. It now renders the fixed clause **`The request body is missing.`**, from a type-keyed branch in the same function (`t-validation-empty-body-message`, closed 2026-09-29, `91c6e40`).

**`CONFLICT` carries a second, narrower leak boundary.** Its `message` is rider-facing and returned **verbatim**, like the rest — and it **must contain no value whatsoever from the conflicting record**: not the other trip's slug, id or name, and not the other stop's name, notes, coordinates or timestamp. A `409` already tells the caller that the id exists somewhere; the message must add nothing to that. This is enforced **at the raise site** — the code that raises it does not read the conflicting row in the first place — and not by filtering in the global handler, which only ever sees the string it was handed. Because the `409` → `CONFLICT` mapping is keyed on status, a bare `HTTPException(409)` would render a well-formed `CONFLICT` envelope while skipping `ApiError.conflict`. `backend/tests/test_bare_409_raise_audit.py` guards against that: it walks every `HTTPException` call under `backend/app/` and fails on a 409 or on any status it can't resolve to a literal (`t-bare-409-envelope-bypass`, closed 2026-09-29, `91c6e40`). The whole point of this branch is that the other trip stays invisible (it may be private, which under Entry 29 must be indistinguishable from nonexistent), and a conflict on a shared global id namespace is the one place where a request about *this* trip is evaluated against a row belonging to *another*.

One shape for every failure is what lets the frontend have a single error path. Kubb generates the client from the OpenAPI spec, so if half the endpoints failed in one shape and half in another, every call site would need to branch on which — including the offline queue, which spec Section 12 ranks in the top three for testing rigor. Two parsing paths there would mean two places for retry logic to be wrong.

**FastAPI's default 422 does not look like this.** Out of the box, a validation failure returns FastAPI's own `{"detail": [...]}` structure, and a `405` or an unhandled `500` returns `{"detail": "..."}`. All of those have to be overridden by global exception handlers that map onto the envelope above — otherwise they are the responses the generated client can't parse like the others. **Those handlers are implemented and wired** (task `t-error-envelope-handlers`, closed), so the envelope above is what every non-2xx response actually returns today. Read that precisely: the shape is not free and it is not FastAPI's. It holds *because* those global handlers override the defaults — anything that bypasses them returns FastAPI's structure, not this one, which is what makes the override the load-bearing part rather than a formality. The narrower gaps that remain are tracked by id under Outstanding, below.

**`405` and `500` are framework-level responses, not endpoint contract.** They are reachable from any path and are therefore deliberately *not* listed in the per-endpoint status codes below. No endpoint declares a 405 or a 500 row. The envelope guarantee still covers them — that guarantee is global, which is precisely why it doesn't belong in a per-endpoint column.

**`409` is the counterpart case: it *is* endpoint contract, and it is listed per-endpoint.** It is raised by our own handler code rather than by the framework, and it is reachable only on the **three create endpoints** — the only places that accept a client-generated id. It therefore appears in the per-endpoint status codes below, on exactly those three rows and nowhere else.

**Entry 29 widens `409` and adds `401`/`429` as endpoint contract.** On the v2 surface `409` is also a state conflict (see the `CONFLICT` row above), so it appears on the v2 rows that can produce one, not only on creates. `401` and `429` are raised by our own gates and limiters, and are listed per endpoint wherever they are reachable. The CSRF `403` is middleware-level, like `405`, and is not listed per endpoint.

**What the OpenAPI document declares differs from the table below — deliberately.** The table lists only statuses a caller can actually receive. The OpenAPI document additionally declares a `422` on the four `GET` operations (`/trips/{slug}`, `/trips/{slug}/stops`, `/trips/{slug}/stops/{id}/photos`, `/trips/{slug}/map`), described as a path-parameter failure, even though it is unreachable today: every path parameter is a plain string, and Starlette's `[^/]+` converter has already guaranteed a non-empty value before validation runs. It is declared for one reason only — an operation with parameters that declares no `422` gets FastAPI's auto-injected `HTTPValidationError`, which Kubb would turn into an `ErrorEnvelope | HTTPValidationError` union with no `code` for the offline queue to branch on. Every declared non-2xx on every operation `$ref`s `ErrorEnvelope`, built from the single `ERROR_RESPONSES` constant in `backend/app/api/responses.py` (the route supplies only the description), and `405`/`500` remain undeclared per operation, as above. Guarded by `backend/tests/test_openapi_error_responses.py`, which walks the whole document rather than a hard-coded route list. Task `t-openapi-error-responses`; `docs/decision-log.md` Entry 17.

### Security headers on every response

Every response carries four fixed headers (`d2913ad`, `t-security-headers`): API responses, the SPA fallback and `/assets` alike, success or error. They are set by `SecurityHeadersMiddleware` in `backend/app/main.py`, a pure ASGI middleware, so streamed file responses pass through unbuffered.

| Header | Value | Why |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | The browser uses the declared content type and does not guess one |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Keeps `/t/<slug>` (the trip's access credential) out of the `Referer` sent to third parties, such as the OSM tile servers, which receive only the origin. Not `no-referrer`, because OSM's tile usage policy wants a `Referer` |
| `X-Frame-Options` | `DENY` | No framing, for older browsers |
| `Content-Security-Policy` | `frame-ancestors 'none'` | No framing, for current browsers. Deliberately **not** a full CSP: the SPA's script, style and tile sources would need their own review |

An unhandled exception is answered by Starlette's outermost error layer, which sits outside this middleware, so the project's catch-all `500` handler (`app/core/errors.py`) attaches the same four headers itself (`724bc9c`). Both read one list in `app/core/headers.py`.

---

## Endpoints

Two surfaces. The **legacy slug routes** are the eight endpoints matching spec Section 5, all under `/api/trips/{slug}`. The **v2 routes** under `/api/v2` were added by decision-log Entry 29 (see "Endpoints: v2 (new)", below). There are no update or delete endpoints for photos on either surface — upload is the only photo write path.

### Endpoints: legacy slug routes

The table carries the Entry 29 rows: a slug only locates the trip, and writes go through the membership gate. Paths are shown without the `/api` prefix, as before. Every row can also return the middleware-level CSRF `403` (unsafe methods), `405` and `500`, none of them listed.

| Method | Path | Slug / auth | Request model | Response model | Status codes |
|---|---|---|---|---|---|
| `GET` | `/trips/{slug}` | either slug; session optional (fills `viewer`) | — | `TripOut` | 200, 404, 429 |
| `GET` | `/trips/{slug}/stops` | either slug | — | `StopOut[]` | 200, 404, 429 |
| `POST` | `/trips/{slug}/stops` | either slug locates; writer | `StopCreate` | `StopOut` | 201 new, 200 replay, 401, 403, 404, 409, 422, 429 |
| `POST` | `/trips/{slug}/stops/{id}/photos` | either slug locates; writer | multipart form fields `id`, `takenAt`, optional ignored `uploadedBy` + binary `file` part | `PhotoOut` | 201 new, 200 replay, 401, 403, 404, 409, 422, 429 |
| `GET` | `/trips/{slug}/stops/{id}/photos` | either slug | — | `PhotoOut[]` | 200, 404, 429 |
| `POST` | `/trips/{slug}/bikes` | either slug locates; writer | `BikeCreate` | `BikeOut` | 201 new, 200 replay, 401, 403, 404, 409, 422, 429 |
| `PATCH` | `/trips/{slug}/bikes/{id}` | either slug locates; writer | `BikePatch` | `BikeOut` | 200, 401, 403, 404, 422, 429 |
| `GET` | `/trips/{slug}/map` | either slug | — | `MapFeatureCollection` | 200, 404, 429 |

Before Entry 29 the third column read "either" for reads and "rider only" for writes, and no row listed `401` or `429`. That is still what the running code does until `t-am-write-gate-legacy` and `t-am-rate-limits` land.

On the legacy photo form, `uploadedBy` becomes **optional and ignored**. Queued items from before the upgrade still send it. The stored name comes from the account (see "Rider writes (v2)", below).

Models by file:

- `TripOut`, `Access` — `backend/app/models/trip.py`
- `StopCreate`, `StopOut`, `LocationSource` — `backend/app/models/stop.py`
- `PhotoOut` — `backend/app/models/photo.py`. **There is no photo *request* model.** The upload's form fields are declared inline on the route as `Annotated[..., Form(description=...)]` parameters in `backend/app/api/routes/photos.py`, and those parameter declarations *are* the request contract — that is where their descriptions live and what the OpenAPI document is built from. A `PhotoCreateForm` class existed until `t-takenat-tz-question`; binding it was proven impossible on the pinned FastAPI without changing the wire format, so it was deleted. See "`takenAt` on the photo upload form must be timezone-aware", below, and `docs/decision-log.md` Entry 16.
- `BikeCreate`, `BikePatch`, `BikeOut` — `backend/app/models/bike.py`
- `MapFeatureCollection` and its feature/geometry/properties models — `backend/app/models/map.py`
- `ErrorCode`, `ErrorDetail`, `ErrorEnvelope` — `backend/app/models/common.py`

### Notes per endpoint

**`GET /trips/{slug}`** — returns trip metadata plus **this trip's** bikes embedded in `TripOut.bikes`, so the app's first load is one request rather than two. It also carries `access`, which is how the frontend knows whether to render write UI at all. Under Entry 29 `access` is deprecated and derived from membership, and `viewer.role` replaces it (see "Access: public trips, members and leaders", above).

**`GET /trips/{slug}/stops`** — the full stop list, including `notes` and `locationSource`. The map endpoint deliberately doesn't duplicate those; this is where they come from.

**`POST /trips/{slug}/stops`** — `StopCreate` carries the client-generated id, coordinates, `locationSource`, `arrivedAt` and optional notes. The id decides a three-way branch (see Idempotency, above): an **unseen** id creates the stop and returns `201`; an id **already on this trip** is a replay and returns the existing stop with `200`; an id that exists **on a different trip** returns `409` / `CONFLICT` with nothing created and nothing about the other trip disclosed — not in the body, not in the message. The replay lookup is on `(trip_id, id)`, and the cross-trip case is found by checking, not by letting the insert fail.

Coordinates are bounded (`d2913ad`, `t-stop-coordinate-bounds`): `lat` must be in **[-90, 90]** and `lng` in **[-180, 180]**, both inclusive, and both must be finite. A value out of range, `NaN` or `Infinity` is a `422` / `VALIDATION_ERROR`. Plain JSON cannot spell `NaN` or `Infinity`, but Python's JSON parser accepts them, so the model rejects them explicitly (`allow_inf_nan=False` in `backend/app/models/stop.py`). This is the same `422` already on the row, not a new status. Because `VALIDATION_ERROR` is never-retry, a stop with impossible coordinates fails visibly in the queue rather than landing on the map somewhere that does not exist.

**`POST /trips/{slug}/stops/{id}/photos`** — `multipart/form-data`. Three form fields — `id`, `uploadedBy`, `takenAt` — plus `file`, the binary part, as a fourth part. They are declared as inline `Form(...)` / `File(...)` parameters on the handler rather than as a request model: there is no Pydantic class for this request body (see "Models by file", above). `takenAt` must carry a UTC offset — see "`takenAt` on the photo upload form must be timezone-aware", below. `{id}` here is the stop id the photo attaches to. Replay of a known photo id returns the existing photo with `200` rather than storing the bytes twice. Upload is a single request per photo. An interrupted upload is retried in full with the same `id`. This is safe: the object key is deterministic, and a replay after the row exists returns `200` without touching storage. There is no S3 multipart or part-level resume: a compressed ~1600px JPEG is under S3's 5 MiB minimum part size, so it is always one part — `docs/decision-log.md` Entry 20.

**`GET /trips/{slug}/stops/{id}/photos`** — photos for one stop. See Photo serving, below, for what `url` actually is.

**`POST /trips/{slug}/bikes`** — same replay semantics as stops.

**`PATCH /trips/{slug}/bikes/{id}`** — every field on `BikePatch` is optional; only fields actually present in the request body change. Absent fields are left alone. **An explicit `null` is rejected** with `422` / `VALIDATION_ERROR` (`d2913ad`, `t-bike-patch-null-422`): every bike column is `NOT NULL`, so a null used to reach the database and come back as a `500`. Omitting a field is how "no change" is spelled; to clear `specs`, send `""`. The OpenAPI schema carries no `default: null` for these fields, so the generated client types them optional, never nullable. There is no replay/`201` case here — a patch against a known bike is a plain `200`, and against an unknown bike id a `404`. Two riders editing the same bike resolve last-write-wins by design (spec Section 4); there is no conflict status code.

**`GET /trips/{slug}/map`** — see Map response, below.

### Endpoints: v2 (new)

"Reader", "writer" and "leader" are the gates in "Access: public trips, members and leaders", above. Every row can also return the middleware-level CSRF `403` (unsafe methods), `405` and `500`. None of those are listed.

| Method | Path | Auth | Request | Response | Status codes | Limit |
|---|---|---|---|---|---|---|
| `POST` | `/api/v2/auth/signup` | anonymous | `AccountCreate` | `RecoveryCodeIssuedOut` + Set-Cookie | 201, 409, 422, 429 | signup-ip, signup-global |
| `POST` | `/api/v2/auth/signin` | anonymous | `SessionCreate` | `MeOut` + Set-Cookie | 200, 401, 422, 429 | signin |
| `POST` | `/api/v2/auth/signout` | anonymous (session optional) | — | — | 204 | none |
| `POST` | `/api/v2/auth/signout-all` | session | — | — | 204, 401, 429 | writes |
| `POST` | `/api/v2/auth/recover` | anonymous | `AccountRecover` | `RecoveryCodeIssuedOut` + Set-Cookie | 200, 401, 422, 429 | signin |
| `GET` | `/api/v2/auth/me` | session | — | `MeOut` | 200, 401, 429 | public-read |
| `POST` | `/api/v2/auth/password` | session | `PasswordChange` | `MeOut` + Set-Cookie | 200, 401, 403, 422, 429 | signin |
| `POST` | `/api/v2/auth/recovery-code` | session | `RecoveryCodeCreate` | `RecoveryCodeIssuedOut` | 200, 401, 403, 422, 429 | signin |
| `GET` | `/api/v2/me/trips` | session | — | `MyTripOut[]` | 200, 401, 429 | public-read |
| `GET` | `/api/v2/me/join-requests` | session | — | `MyJoinRequestOut[]` | 200, 401, 429 | public-read |
| `POST` | `/api/v2/join-requests/{requestId}/cancel` | session | — | `MyJoinRequestOut` | 200, 401, 404, 409, 429 | writes |
| `GET` | `/api/v2/trips` | anonymous | query `cursor?`, `limit?` (1–50, default 20) | `TripPageOut` | 200, 422, 429 | public-read |
| `POST` | `/api/v2/trips` | session | `TripCreate` | `TripOut` | 201 new, 200 replay, 401, 409, 422, 429 | trip-create |
| `POST` | `/api/v2/trips/claim` | session | `JoinClaimCreate` | `MyJoinRequestOut` | 201 new, 200 existing pending, 401, 404, 409, 422, 429 | join |
| `GET` | `/api/v2/trips/{tripId}` | reader | — | `TripOut` | 200, 404, 429 | public-read |
| `PATCH` | `/api/v2/trips/{tripId}` | leader | `TripPatch` | `TripOut` | 200, 401, 403, 404, 422, 429 | writes |
| `GET` | `/api/v2/trips/{tripId}/stops` | reader | — | `StopOut[]` | 200, 404, 429 | public-read |
| `POST` | `/api/v2/trips/{tripId}/stops` | writer | `StopCreate` | `StopOut` | 201, 200 replay, 401, 403, 404, 409, 422, 429 | writes |
| `GET` | `/api/v2/trips/{tripId}/stops/{stopId}/photos` | reader | — | `PhotoOut[]` | 200, 404, 429 | public-read |
| `POST` | `/api/v2/trips/{tripId}/stops/{stopId}/photos` | writer | multipart: `id`, `takenAt`, `file` | `PhotoOut` | 201, 200 replay, 401, 403, 404, 409, 422, 429 | writes |
| `GET` | `/api/v2/trips/{tripId}/map` | reader | — | `MapFeatureCollection` | 200, 404, 429 | public-read |
| `GET` | `/api/v2/trips/{tripId}/bikes` | reader | — | `BikeOut[]` | 200, 404, 429 | public-read |
| `POST` | `/api/v2/trips/{tripId}/bikes` | writer | `BikeCreate` | `BikeOut` | 201, 200 replay, 401, 403, 404, 409, 422, 429 | writes |
| `PATCH` | `/api/v2/trips/{tripId}/bikes/{bikeId}` | writer | `BikePatch` | `BikeOut` | 200, 401, 403, 404, 422, 429 | writes |
| `GET` | `/api/v2/trips/{tripId}/members` | member-read | — | `MemberOut[]` | 200, 401, 403, 404, 429 | public-read |
| `POST` | `/api/v2/trips/{tripId}/members/{userId}/promote` | leader | — | `MemberOut` | 200, 401, 403, 404, 429 | writes |
| `DELETE` | `/api/v2/trips/{tripId}/members/{userId}` | leader | — | — | 204, 401, 403, 404, 429 | writes |
| `POST` | `/api/v2/trips/{tripId}/step-down` | leader | — | `MemberOut` | 200, 401, 403, 404, 409, 429 | writes |
| `POST` | `/api/v2/trips/{tripId}/leave` | writer | — | — | 204, 401, 403, 404, 409, 429 | writes |
| `POST` | `/api/v2/trips/{tripId}/join-requests` | session, then reader | `JoinRequestCreate` | `MyJoinRequestOut` | 201 new, 200 existing pending, 401, 404, 409, 422, 429 | join |
| `GET` | `/api/v2/trips/{tripId}/join-requests` | leader | query `state?` = `pending` (default) or `blocked` | `TripJoinRequestOut[]` | 200, 401, 403, 404, 422, 429 | public-read |
| `POST` | `/api/v2/trips/{tripId}/join-requests/{requestId}/decision` | leader | `JoinDecisionCreate` | `TripJoinRequestOut` | 200, 401, 403, 404, 409, 422, 429 | writes |
| `POST` | `/api/v2/trips/{tripId}/join-requests/{requestId}/unblock` | leader | — | `TripJoinRequestOut` | 200, 401, 403, 404, 409, 429 | writes |

Every `GET` also answers `HEAD` through a second, schema-excluded registration of the same handler (Entry 11). `/claim` is registered before the `{tripId}` routes.

### Notes per endpoint (v2)

**Signup**
- `username`: lowercased, then must match `^[a-z0-9][a-z0-9_.-]{2,31}$`, or `422`. Taken → `409`.
- `displayName`: trimmed, 1–40 characters, no control characters.
- `password`: NFKC-normalised, 15–128 code points, no control characters (category `Cc`), not trimmed.
- `201` returns the new account and the recovery code (**the only time it is shown**) and signs the device in. Any session already on the request is deleted first.
- Not idempotent. A retried signup whose first response was lost gets `409`. The user then signs in and rotates the recovery code from Account.

**Signin**
- Wrong username or password → `401` with "Username or password is incorrect.", identical in both cases.
- Locked account → `429` + `Retry-After`.
- A disabled account → the same `401`.
- Success resets `failed_logins` and deletes this user's expired session rows.

**Signout** returns `204` whether or not a session was present. It deletes the row if there is one and always clears the cookie.

**Signout-all** deletes every session row for the user, including this one, and clears the cookie.

**Recover** (`username`, `recoveryCode`, `newPassword`)
- Wrong code or username → `401`, counted against the lockout.
- On success, in one transaction: set the new password, revoke every session, issue a new recovery code, reset the lockout. Then sign this device in.
- It works for operator reset codes too (`reset_account` writes the same column).

**Password change** (`currentPassword`, `newPassword`)
- Wrong current password → `403`, counted against the lockout.
- `newPassword` equal to the current one → `422`.
- On success: revoke every session, then issue a fresh one for this device.

**Recovery-code rotation** (`password`): wrong password → `403`, counted against the lockout. On success the old code stops working and the new one is shown once.

**Me** returns the caller's own `id`, `username`, `displayName` and `createdAt`. This is the only response that ever contains a username.

**`GET /api/v2/trips`**
- Returns public trips only, the same for every caller. A session is ignored.
- Sorted by `lastPublicStopAt` descending with nulls last, then `id` ascending.
- `nextCursor` is opaque (base64url of the last row's sort key) and `null` on the last page. A malformed cursor → `422`.
- Public trips with no visible stops are included, sorted last.

**`GET /api/v2/trips/{tripId}`** returns `TripOut`, delay-applied for non-members. `viewer.role` is computed from the session.

**`POST /api/v2/trips`**
- Body: `id` (UUID), `name` (1–100 after trimming), `startDate`, and `visibility` (default `public`).
- In one transaction: the trip, with both slugs `NULL`, `created_by` set to the caller and `public_delay_hours` 24, plus the caller's `trip_members` row with role `leader`.
- Returns `201` with `viewer.role = "leader"`.

**`PATCH /api/v2/trips/{tripId}`**
- Fields: `name`, `visibility`, `publicDelayHours` (0–168). All optional.
- Omitting a field leaves it alone. An explicit `null` → `422` (the `BikePatch` rule). An empty body → `200` with nothing changed.
- There is no server-side publish confirmation. The Publish dialog warns in the UI.

**Rider writes (v2)** behave exactly like their legacy counterparts, except for the locator and the gate. The server sets `created_by` from the session. `PhotoOut.uploadedBy` is the account's `displayName` at upload time, stored in `photos.uploaded_by`.

**Members**
- `GET .../members` returns active members in `joinedAt` order, and only to active members.
- **Promote**: the target must be an active rider, else `404`. Promoting an existing leader returns `200` and is idempotent.
- **Revoke** (`DELETE`):
  - The target must be an active rider. A leader target (including yourself) → `403` "Leaders can't remove another leader".
  - Never a member → `404`. Already revoked → `204` (idempotent).
  - Sets `revoked_at` and `revoked_by`. The row is kept.
- **Step-down** and **leave**: the last active leader → `409` "Promote another rider to leader first".
  - Leave sets `revoked_at` with `revoked_by` = self. It does **not** start the 7-day cooldown.
- Every membership-changing statement first locks the trip row (`SELECT … FOR UPDATE`), so concurrent step-downs can't leave a trip with no leader.

**Join request create**
- `message` is optional, trimmed, and at most 280 characters. An empty message is stored as `null`.
- A pending request already exists → `200` with it, and its message is not changed.
- `409` for:
  - an active member, leaders included;
  - any `blocked` request on this (trip, user);
  - a rejection, or a revocation by someone else, less than 7 days ago;
  - the user already holding 20 pending requests;
  - the trip already holding 100 pending requests.
- A private trip → the byte-identical `404`. Private trips therefore can't be joined by id. The legacy claim is their only way in.

**Cancel**
- Your own pending request → `cancelled`.
- Already cancelled → `200`, idempotent.
- Already decided → `409`.
- Not yours, or no such request → `404`.
- You may request again immediately.

**Me / join-requests** lists your requests, newest first, at most 100. The fields are `tripName`, `state` and `message`. `blocked` is reported to the requester as `rejected`.

**Trip join-requests (leader)**
- Shows `requester: {userId, displayName}`, never a username, plus `message` and `via` (`direct` or `legacy_rider_link`).
- `state=blocked` lists blocked requests so a leader can unblock them.

**Decision** (`approve`, `reject` or `reject_and_block`)
- Pending → the matching state (`approved`, `rejected` or `blocked`), with `decided_at` and `decided_by` set.
- `approve` inserts a `rider` membership, or does nothing if the user is already an active member.
- Repeating the decision that produced the current state → `200`, idempotent. Any other decision on a decided request → `409`.
- A request not on this trip → `404`.
- Bulk approve and reject is the UI looping over this endpoint.

**Unblock**: `blocked` → `rejected`, keeping the original `decided_at`. The 7-day cooldown from the original decision still applies. A request that isn't blocked → `409`.

**Claim** (`riderSlug` in the body, never the URL)
- Matches `trips.rider_slug` only. A viewer slug or an unknown slug → `404`, with the same message as an unknown legacy link.
- Creates a **pending** request with `via='legacy_rider_link'`, subject to every join-request rule above. It **never** grants membership.
- The slug is never logged and never echoed back.

### Photo upload: JPEG only, metadata stripped, 15 MiB cap

This applies identically to the legacy and v2 upload routes, through one implementation (`core/jpeg.py`):

- **Non-JPEG → `422 VALIDATION_ERROR`.** The file must start `FF D8 FF` and walk cleanly to a Start-of-Scan (`SOS`) marker. Anything truncated or malformed → `422`.
- **Metadata stripped before storage.** A plain-Python walk over the JPEG segments drops APP1–APP15 (`FFE1`–`FFEF`, which include EXIF, XMP and ICC) and COM (`FFFE`). It keeps SOI, APP0/JFIF, DQT, SOF*, DHT, DRI and the SOS header, and copies the compressed image data after SOS unchanged.
- **The stripped bytes are what goes to S3.** A replay performs no storage write, as before.
- **Orientation.** Stripping removes the EXIF Orientation tag. The client's canvas re-encode already applies orientation to the pixels, so nothing changes visually.
- **Size cap: 15 MiB (15,728,640 bytes) → `422 VALIDATION_ERROR`, not `413`.** Three reasons:
  - `code_for_status` maps any unlisted status to `INTERNAL_ERROR`, which the queue retries forever. An oversized photo would retry forever.
  - A dedicated code would be a ninth `ErrorCode`, which needs its own ruling (Entries 6 and 14). Entry 29 admitted only two.
  - `422` is already on the upload rows and is never-retry: the rider is told, and the blob is kept.

  The file part's size is checked, and a request whose declared `Content-Length` is over 16 MiB is refused with the same `422` before its body is read.

This is the same `422` already on both upload rows, not a new status, in the same way as the `takenAt` ruling below.

### Models by file (additions)

- `ErrorCode`, extended by `UNAUTHENTICATED` and `RATE_LIMITED` — `backend/app/models/common.py`
- `AccountCreate`, `SessionCreate`, `AccountRecover`, `PasswordChange`, `RecoveryCodeCreate`, `MeOut`, `RecoveryCodeIssuedOut` — `backend/app/models/account.py`
- `Visibility`, `ViewerRole`, `ViewerOut`, `TripCreate`, `TripPatch`, `TripSummaryOut`, `TripPageOut`, `MyTripOut` — `backend/app/models/trip.py`
  - `TripOut` gains `visibility`, `publicDelayHours`, `riderCount`, `lastPublicStopAt` and `viewer`.
  - `access` is kept and deprecated.
- `JoinRequestState`, `JoinRequestVia`, `JoinDecisionAction`, `JoinRequestCreate`, `JoinClaimCreate`, `JoinDecisionCreate`, `MyJoinRequestOut`, `TripJoinRequestOut`, `PersonOut` — `backend/app/models/join_request.py`
- `MemberRole`, `MemberOut` — `backend/app/models/member.py`
- `PhotoOut` keeps its shape. `uploadedBy`'s description changes to say it is the uploading account's `displayName` at upload time, or the stored free-text label for photos uploaded before accounts existed.

---

## Access control: 401, 403 and 404 are three different answers

*Replaces the pre-Entry 29 section "Access control: 403 and 404 are different answers" (`403` = viewer slug on a write, `404` = no trip has this slug). Route descriptions and test docstrings that still cite that title describe the running slug-only implementation until the write gate lands. Its core rule survives here: collapsing `403` and `404` in either direction is a real bug.*

- **`401 UNAUTHENTICATED`**: we don't know who you are. There is no valid session where one is required. Nothing about the trip is disclosed, because on v2 it is decided before the trip is looked up.
- **`403 FORBIDDEN`**: we know who you are, the trip is visible to you (public, or you have a membership row, including a revoked one), and you may not do this.
- **`404 NOT_FOUND`**: nothing you may see exists here. A private trip is byte-identical to one that doesn't exist.

`NOT_FOUND` is also used for an unknown stop, bike, request or member id on a trip you may see, and for a stop hidden by the public delay.

**Required identity × trip matrix** (Entry 29 §15.1). "Write" is any writer route; "leader" is any leader route.

| Identity | Public read | Private read | Public write | Private write | Public leader | Private leader |
|---|---|---|---|---|---|---|
| anonymous | 200, delayed | 404 | 401 | 401 | 401 | 401 |
| signed-in non-member | 200, delayed | 404 | 403 | 404 | 403 | 404 |
| pending | 200, delayed | 404 | 403 | 404 | 403 | 404 |
| rider | 200, full | 200, full | 2xx | 2xx | 403 | 403 |
| revoked | 200, delayed | 404 | 403 | 403 | 403 | 403 |
| leader | 200, full | 200, full | 2xx | 2xx | 2xx | 2xx |

For legacy slug writes, the rows are the same with the trip always located: an unknown slug is `404`, then `401` / `403` as above.

---

## Data model: migration 0003

`backend/migrations/0003_accounts_membership.sql` (to be created by `t-am-migration-0003`). Decision-log Entry 29 §11.

### Plan

- **Forward-only, one file, applied in one transaction by the existing runner.** Every statement is guarded with `IF NOT EXISTS` or a `pg_constraint` check, in the style of `0002`.
- **Additive and relaxing only.** No column is dropped or renamed and no data is deleted.
- **Backfill happens through column defaults.** Existing trips get `visibility='private'` and `public_delay_hours=24`. `created_by` and `created_at` are `NULL` on existing rows. Slugs are untouched. No memberships and no join requests are created.
- **The image before this change still works on the new schema:**
  - Every new `trips` column is nullable or has a default, so the old `seed_trip` INSERT succeeds.
  - `stops`, `photos` and `bikes` `created_by` are nullable, so the old write routes' INSERTs succeed.
  - The old `get_by_slug` never matches a row with `NULL` slugs.
  - The old `migrate.py` ignores ledger versions it doesn't know about (it only skips recorded files).
- **Users are never deleted by the app.**
  - Session rows cascade with their user.
  - `trip_members` and `join_requests` reference users with `ON DELETE RESTRICT`, so an operator's manual delete fails loudly instead of silently dropping memberships.
  - Authorship columns use `ON DELETE SET NULL`, so photos and stops are never lost with an account.
- `tables.py` mirrors every table, column, constraint and index in the same patch, and `tests/test_schema.py` must stay green.

### SQL

```sql
-- 0003_accounts_membership
-- Decision-log Entry 29 (accounts, public trips, leader-gated membership).
-- Forward-only, additive/relaxing: the pre-0003 image runs unchanged on this schema.

-- Accounts. id is a server-generated UUID4 string (text, matching 0001's id convention).
CREATE TABLE IF NOT EXISTS users (
    id                  text        NOT NULL PRIMARY KEY,
    username            text        NOT NULL,           -- stored lowercased; private login handle
    display_name        text        NOT NULL,           -- public
    password_hash       text        NOT NULL,           -- argon2id PHC string
    recovery_code_hash  text        NULL,               -- sha256 hex of the current one-time code
    failed_logins       integer     NOT NULL DEFAULT 0,
    locked_until        timestamptz NULL,
    disabled_at         timestamptz NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_username_key UNIQUE (username),
    CONSTRAINT users_username_format_check CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
    CONSTRAINT users_display_name_length_check CHECK (char_length(display_name) BETWEEN 1 AND 40),
    CONSTRAINT users_failed_logins_check CHECK (failed_logins >= 0)
);

-- Sessions: only the SHA-256 of the cookie token is stored.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash          bytea       NOT NULL PRIMARY KEY,
    user_id             text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_used_at        timestamptz NOT NULL DEFAULT now(),
    absolute_expires_at timestamptz NOT NULL,
    CONSTRAINT sessions_token_hash_length_check CHECK (octet_length(token_hash) = 32),
    CONSTRAINT sessions_expiry_check CHECK (absolute_expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions (user_id);

-- Trips: visibility, delay, provenance. Existing rows become private via the default.
ALTER TABLE trips ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'private';
ALTER TABLE trips ADD COLUMN IF NOT EXISTS public_delay_hours integer NOT NULL DEFAULT 24;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS created_at timestamptz NULL;   -- NULL = pre-0003, unknown
ALTER TABLE trips ALTER COLUMN created_at SET DEFAULT now();              -- new rows only
ALTER TABLE trips ALTER COLUMN rider_slug  DROP NOT NULL;                 -- app-created trips have none
ALTER TABLE trips ALTER COLUMN viewer_slug DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trips_visibility_check' AND conrelid = 'trips'::regclass) THEN
        ALTER TABLE trips ADD CONSTRAINT trips_visibility_check CHECK (visibility IN ('public', 'private'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trips_public_delay_hours_check' AND conrelid = 'trips'::regclass) THEN
        ALTER TABLE trips ADD CONSTRAINT trips_public_delay_hours_check CHECK (public_delay_hours BETWEEN 0 AND 168);
    END IF;
    -- Both slugs or neither: a half-slugged trip has no meaning under either model.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'trips_slugs_paired_check' AND conrelid = 'trips'::regclass) THEN
        ALTER TABLE trips ADD CONSTRAINT trips_slugs_paired_check CHECK ((rider_slug IS NULL) = (viewer_slug IS NULL));
    END IF;
END
$$;
-- UNIQUE(rider_slug), UNIQUE(viewer_slug) and trips_slugs_differ_check still hold: NULLs are distinct / CHECK passes on NULL.

CREATE INDEX IF NOT EXISTS ix_trips_created_by ON trips (created_by);
CREATE INDEX IF NOT EXISTS ix_trips_public ON trips (id) WHERE visibility = 'public';

-- Authorship on children (NULL for pre-0003 rows and for writes by the pre-0003 image).
ALTER TABLE stops  ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;
ALTER TABLE bikes  ADD COLUMN IF NOT EXISTS created_by text NULL REFERENCES users (id) ON DELETE SET NULL;

-- Delay filter + lastPublicStopAt: max(arrived_at) per trip under a bound.
CREATE INDEX IF NOT EXISTS ix_stops_trip_id_arrived_at ON stops (trip_id, arrived_at);

-- Memberships: surrogate id so revoked rows are kept as history; one ACTIVE row per (trip, user).
CREATE TABLE IF NOT EXISTS trip_members (
    id          text        NOT NULL PRIMARY KEY,
    trip_id     text        NOT NULL REFERENCES trips (id) ON DELETE CASCADE,
    user_id     text        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    role        text        NOT NULL,
    joined_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz NULL,
    revoked_by  text        NULL REFERENCES users (id) ON DELETE SET NULL,  -- NULL with revoked_at set = operator CLI
    CONSTRAINT trip_members_role_check CHECK (role IN ('rider', 'leader')),
    CONSTRAINT trip_members_revoked_by_check CHECK (revoked_by IS NULL OR revoked_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trip_members_active ON trip_members (trip_id, user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_trip_members_user_id ON trip_members (user_id);

-- Join requests: per person. One pending per (trip, user) via partial unique index.
CREATE TABLE IF NOT EXISTS join_requests (
    id          text        NOT NULL PRIMARY KEY,   -- server-generated UUID4
    trip_id     text        NOT NULL REFERENCES trips (id) ON DELETE CASCADE,
    user_id     text        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    state       text        NOT NULL DEFAULT 'pending',
    via         text        NOT NULL DEFAULT 'direct',
    message     text        NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz NULL,                   -- set on every exit from pending (incl. cancel)
    decided_by  text        NULL REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT join_requests_state_check CHECK (state IN ('pending', 'approved', 'rejected', 'cancelled', 'blocked')),
    CONSTRAINT join_requests_via_check CHECK (via IN ('direct', 'legacy_rider_link')),
    CONSTRAINT join_requests_message_length_check CHECK (message IS NULL OR char_length(message) <= 280),
    CONSTRAINT join_requests_decided_check CHECK ((state = 'pending') = (decided_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_join_requests_one_pending ON join_requests (trip_id, user_id) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS ix_join_requests_trip_state ON join_requests (trip_id, state);
CREATE INDEX IF NOT EXISTS ix_join_requests_user_state ON join_requests (user_id, state);

COMMENT ON COLUMN trips.rider_slug IS
    'Legacy locator only (Entry 29). Grants nothing; writes need an active trip_members row. NULL for app-created trips.';
COMMENT ON COLUMN trips.viewer_slug IS
    'Legacy read link for pre-0003 trips; kept until the legacy removal window closes. NULL for app-created trips.';
COMMENT ON COLUMN photos.uploaded_by IS
    'Display name at upload time: the uploading account''s display_name since 0003, free text before it.';
```

The `COMMENT ON COLUMN` for `rider_slug` replaces 0001's comment ("Possession of this value is the whole write authorisation"), which is false after Entry 29.

---

## Photo serving

`PhotoOut.url` is a **presigned GET URL** into the S3-compatible object store, generated fresh at read time by the `storage/` module (`backend/app/storage/`). Three consequences worth being explicit about:

- It is **never persisted in the database.** The DB holds the object key; the URL is derived per request. A URL that appears in an old response is not a stable identifier and should not be stored or cached long-term by the client.
- It is **never a OneDrive URL.** OneDrive is a write-only archive reached by a background sync job. It is never in a read path, and nothing user-facing waits on it (spec Section 4, and `docs/architecture-diagram.md`).
- The object store is the source of truth the app always reads from, which is why an unhealthy or lapsed archive sync degrades nothing a viewer can see.

**Which host the URL points at** (`263fcea`, `t-presign-public-endpoint`). A presigned URL's signature covers its host, so the URL must be signed for an address the *browser* can reach, which is not always the address the API uses. `S3_PUBLIC_ENDPOINT_URL` (optional) names that browser-facing host; presigned GETs are signed for it. When it is unset, they are signed for `S3_ENDPOINT_URL`. Uploads and object reads always use `S3_ENDPOINT_URL`.

- **Production (R2):** leave it unset. The R2 endpoint is the same for the API and the browser.
- **docker compose:** compose sets `S3_PUBLIC_ENDPOINT_URL=http://localhost:9000`, because the API reaches MinIO as `http://minio:9000`, a name no browser can resolve.
- **The image run outside compose** (for example the runbook §3 smoke test): set it yourself, or every photo `url` points at a host the browser cannot reach.

A client that sees a photo fail to load should treat it as an expired URL, not a missing photo: the stop detail screen refetches the photo list once on an image error to get fresh URLs.

`PhotoOut.archived` is a boolean reflecting whether the background sync has landed that photo in the archive yet. It is status reporting, not a read dependency — `false` means "not archived yet", never "unavailable".

---

## Map response

`GET /trips/{slug}/map` returns a **single GeoJSON `FeatureCollection`** (`MapFeatureCollection`), handed straight to Leaflet. It holds two kinds of feature, told apart by `geometry.type` alone — there is deliberately no extra `kind` or `featureType` tag:

**Stop features (Point)** — one per stop.
- The stop's id goes on GeoJSON's own top-level `Feature.id` member, not inside `properties`. That's what the frontend matches against the `/stops` list when a pin is clicked.
- `properties` is limited to `name` and `arrivedAt` — the two values needed to label a pin.

**Trail feature (LineString)** — at most one, and **only when the trip has 2+ stops.** A trip with 0 or 1 stops omits it entirely; a LineString with fewer than two positions is invalid GeoJSON, so there is no such thing as an empty trail. Coordinates are sorted chronologically by `arrivedAt`. Its `properties` is always an empty object — the trail is derived from the stops, not stored, and carries no metadata of its own.

A trip with no stops is a valid `200` with `features: []` — an empty map, not an error.

### Coordinate order — silent-bug risk

GeoJSON positions are **`[longitude, latitude]`** — the **reverse** of the `lat` / `lng` field order used everywhere else in this API, and the reverse of Leaflet's own `LatLng`.

Both are plain floats. Swapping them raises nothing, validates fine, and returns `200`. The only symptom is a pin plotted somewhere else on Earth. This is worth an explicit assertion in tests rather than trusting review to catch it.

### Why `notes` and `locationSource` are absent from map properties

Deliberate. On pin click the frontend resolves the full stop by id from the already-fetched `/trips/{slug}/stops` list. Duplicating those fields into map properties would mean the same data served by two endpoints — two chances to drift, for no saved request.

---

## `Stop.locationSource`

`LocationSource` — `backend/app/models/stop.py` — records **how** a stop's coordinates were obtained, not just what they are:

- `"gps"` — an automatic geolocation fix, captured on "Add stop".
- `"manual"` — geolocation was denied or unavailable, and the rider tapped a point on the map instead.

It exists so a viewer can tell an exact fix from an approximate tap, and so the GPS-denied fallback path is something tests can assert actually ran rather than inferring it from coordinates (spec Sections 6 and 12). It's required on `StopCreate` and always present on `StopOut` — there is no "unknown" third value, because both capture paths in the frontend know which one they are.

---

## `StopCreate.arrivedAt` must be timezone-aware

**Ruling (Architect, `t-arrivedat-tz-contract`).** `arrivedAt` on `StopCreate` is a timezone-aware instant. A **naive** datetime — one carrying no UTC offset — is **rejected** with `422` / `VALIDATION_ERROR`. It is not defaulted, not assumed to be UTC, and not assumed to be the server's local time.

**Why this is a ruling and not a preference.** `arrivedAt` is captured **on the device**, potentially hours offline, and the rider crosses timezone boundaries mid-trip and syncs later. A naive value therefore has no correct offset to assume: the server's offset is wrong (the request may arrive days later from somewhere else), UTC is a guess, and a fixed "trip offset" is wrong the moment the rider crosses a border. Accepting one would silently place the stop at the **wrong hour in the timeline** — wrong pin label, wrong position in the chronological trail — with nothing to flag it. This is the same failure shape as the coordinate-order note above: it validates, it returns a success status, and the only symptom is data that is quietly wrong.

**The endpoint table does not change.** This is the **same `422` already listed** on the `POST /trips/{slug}/stops` row. A naive value fails Pydantic schema validation *before the handler runs*, exactly like a missing `lat` or a non-numeric `lng` — it is not a new failure mode, a new code, or a new status. **Do not add a second `422` row for it.** That row stays byte-identical, and the same applies to the other two create rows.

**JSON Schema has no vocabulary for timezone-awareness — so this is server-enforced only.** Whether it is spelled as Pydantic's `AwareDatetime` or as a hand-written field validator, the emitted schema is identical: `{"type": "string", "format": "date-time"}`. There is no keyword that says "offset required". Consequences, in order:

- The OpenAPI document cannot express the constraint, so **Kubb types this `arrivedAt: string`.**
- The generated client therefore **cannot catch a naive value.** No type error, no client-side validation failure, nothing at the call site.
- The **only** thing that rejects a naive value is the server. This subsection exists because that asymmetry is invisible from the endpoint table, from the OpenAPI spec, and from the generated client — the three places a reader would normally look.

**What that costs the offline queue.** `VALIDATION_ERROR` is a **never-retry** code (see "Error envelope", above), which means the queued item is **dequeued permanently and surfaced to the rider**. So a frontend bug that sends a naive `arrivedAt` does not retry the stop — it **loses** it, and shows the rider an error instead.

That is the **accepted trade**, made deliberately: a surfaced error is recoverable, because the rider is told and can act; a stop silently filed at the wrong hour is not, because nothing ever flags it. But it puts real weight on the frontend actually sending an offset — this is the create field where a client-side mistake is most expensive, and the generated client will not warn anyone about it.

**`StopOut.arrivedAt` needs no change.** Its description already reads "a timezone-aware ISO 8601 instant"; as of this ruling that sentence is true **on input as well as output**, rather than describing only what the server happens to return. It is already pinned by `test_arrived_at_is_timezone_aware` in `backend/tests/test_stops_list_endpoint.py`.

The reasoning this replaces — that the description must *not* claim timezone-awareness precisely because nothing enforced it — is recorded in `docs/decision-log.md` Entry 15. It was correct when it was made; read it there before re-arguing either side.

**The offset is not kept, and that is deliberate (decision-log Entry 26).** "Aware on input" does not mean "the rider's offset is stored". `timestamptz` stores an instant, so `arrivedAt` (and the photo `takenAt`) come back in UTC and the offset the device sent is gone. The frontend shows every time in the **reader's** zone **with the zone labelled** (`formatInstant` in `frontend/src/format.ts`, e.g. "ACST"), so a reader in another zone is not misled. Showing the *rider's* local time would need the offset stored in new columns (`arrived_offset_minutes` / `taken_offset_minutes`); that is filed as triggered debt `t-stop-rider-offset`, and its trigger is the spec adding a requirement to show rider-local time.

---

## `takenAt` on the photo upload form must be timezone-aware

**Ruling (`t-takenat-tz-question`).** `takenAt` on `POST /api/trips/{slug}/stops/{stop_id}/photos` is an **offset-aware ISO 8601 instant**. A **naive** value — one carrying no UTC offset — is **rejected** with `422` / `VALIDATION_ERROR`. It is not defaulted, not assumed to be UTC, and not assumed to be the server's local time.

Read the next two subsections before the third. The conclusion here is identical to `StopCreate.arrivedAt`'s, and it was **not** reached by copying it across — it was reached by measuring the server and by testing the one premise that was supposed to make photos different, which failed. Anyone skimming will assume the analogy did the work. It did not.

### Why this is a live bug and not a style preference — the asyncpg measurement

A naive `takenAt` did not land in the database as "a naive value". It landed as a **different instant depending on which machine served the request.**

SQLAlchemy's asyncpg dialect has **no bind processor** for `timestamptz`, so a naive `datetime` reaches asyncpg untouched, and asyncpg's `timestamptz_encode` calls `obj.astimezone(utc)` — which resolves a naive datetime in the **host process's local zone**. Measured, same input both times:

| Host zone | Input `takenAt` | Stored instant |
|---|---|---|
| `Malay Peninsula Standard Time` (UTC+8) | `2026-06-14T10:00` | `2026-06-14T02:00Z` |
| UTC (the container) | `2026-06-14T10:00` | `2026-06-14T10:00Z` |

**Eight hours of divergence for identical input, decided by where the API happens to run.** A developer's local `uvicorn` and the deployed container wrote different instants from the same upload, on a shipped route, with no error anywhere. That is why this was a Gate 1 (**CURRENTLY BROKEN**) fix rather than debt: it is not that a naive value *could* be misread, it is that two deployments of the same code already read it two ways.

### Why photos were thought to be different — the EXIF argument, tested and failed

The question was filed separately from the `arrivedAt` ruling on one specific premise, and the filing said in as many words: do not close this by copying Entry 15 across, because the input is a different kind of input. The premise:

> EXIF `DateTimeOriginal` is **naive by design** — the offset lives in a separate `OffsetTimeOriginal` tag that is frequently absent on imported, exported or edited images. So "reject naive" may be **unsatisfiable** for a large share of real photos, and the answer may have to be something weaker.

**That premise was tested and it failed.** EXIF is never the wire source. The frontend composes this field before the request is built, and `Date.prototype.getTimezoneOffset()` is available unconditionally in every browser the app runs in. There is no case where the client has a capture time and no offset to pair with it — at worst it has the *device's current* offset, which for a photo just picked on that device is the right one.

**The frontend ladder — contract text, not a suggestion.** Whoever builds `s-frontend-add-stop-flow` implements this, in order, stopping at the first rung that yields a value:

- **(a)** `DateTimeOriginal` **and** `OffsetTimeOriginal` both present → combine them. Most faithful: the offset the camera itself recorded.
- **(b)** `DateTimeOriginal` present, no `OffsetTimeOriginal` → combine the wall-clock reading with the **device's current** UTC offset from `getTimezoneOffset()`.
- **(c)** No usable EXIF at all → `new Date().toISOString()` at pick time.

(b) and (c) can be wrong — a photo imported from a camera whose clock was set in another timezone, or picked from the library weeks later. They are wrong by a bounded, explainable amount. An unenforced naive value was wrong by *wherever the server was running*, which is not a property of the photo at all.

### Only now: it lands where `arrivedAt` landed

With the EXIF objection gone, the field is structurally what `StopCreate.arrivedAt` is — captured on-device, possibly hours offline, by a rider crossing timezone boundaries, synced later, stored in a `timestamptz` column — and it gets the same answer for the same reasons. See decision-log Entry 15 for that reasoning and Entry 16 for this one. The three consequences below carry over intact.

**The endpoint table does not change.** This is the **same `422` already listed** on the `POST /trips/{slug}/stops/{id}/photos` row. A naive value fails schema validation *before the handler runs*, exactly like a missing `uploadedBy` — it is not a new failure mode, a new code, or a new status. **Do not add a second `422` row for it.** That row stays byte-identical.

**JSON Schema has no vocabulary for timezone-awareness — so this is server-enforced only.** Pydantic's `AwareDatetime` and a bare `datetime` emit the *identical* schema: `{"type": "string", "format": "date-time"}`. There is no keyword meaning "offset required". So:

- The OpenAPI document cannot express the constraint, and **Kubb types this `takenAt: string`.**
- The generated client therefore **cannot catch a naive value** — no type error, no client-side validation failure, nothing at the call site.
- The **only** thing that rejects a naive value is the server.

The corollary a maintainer needs: because the emitted schema is byte-identical either way, **relaxing `AwareDatetime` back to `datetime` produces no visible diff in the OpenAPI document.** It looks free. It is the exact change that reintroduces the host-zone bug above. The route file carries this warning beside the parameter for that reason.

**What that costs the offline queue.** `VALIDATION_ERROR` is a **never-retry** code (see "Error envelope", above), so the queued item is **dequeued permanently and surfaced to the rider**. A frontend bug that sends a naive `takenAt` does not retry the upload — it **loses** it, and shows the rider an error instead. That is the accepted trade, the same one made for `arrivedAt`: a surfaced error is recoverable because the rider is told; a photo silently filed hours off in the timeline is not.

### `takenAt` comes back in UTC, not in the spelling the device sent

Every response builds `PhotoOut` from the stored row, so `takenAt` is always the `timestamptz` column read back, which is UTC with a `Z`:

| Request sent | Response | `takenAt` in body |
|---|---|---|
| `2026-06-14T10:00:00+09:30` | `POST` → `201` (fresh upload) | `2026-06-14T00:30:00Z` |
| same | `POST` → `200` (replay) | `2026-06-14T00:30:00Z` |
| — | `GET` (list) | `2026-06-14T00:30:00Z` |

Corrected 2026-09-29. Until `d534ea7` (`t-photo-insert-echoes-argument`), the `201` was built from the insert's arguments and echoed `+09:30`, so a `201` and its own replay spelled one instant two ways. That is fixed: the repository's `insert()` now returns the row as stored, and `backend/tests/test_photo_upload_storage.py` pins the `201` and `200` bodies as identical. The body still does not repeat the offset the device sent. **Compare `takenAt` against the value you submitted as a datetime, never as a string.** The contract promises **an instant, not a spelling**.


---

## Related work

- **Introduced by:** story `s-api-contract` (Session 1), tasks `t-error-envelope-model`, `t-trip-model`, `t-stop-model`, `t-bike-model`, `t-photo-model`, `t-map-model`, and this document, `t-api-contract-doc`.
- **Depends on:** `docs/architecture-diagram.md` (Session 0) for where `data/` and `storage/` sit; spec Sections 3, 4, 5.
- **Unblocks:** milestone `m2-core-api` — stories `s-trip-metadata-endpoint`, `s-stop-crud`, `s-photo-upload-onedrive-sync`, `s-bike-management`, `s-map-geojson-endpoint` — and, through the generated client, Week 3's frontend stories including `s-offline-queue`.
- **Consumed by:** the frontend API client in `frontend/src/api/`, generated by Kubb from the backend's OpenAPI spec. Nothing on the frontend hand-writes a fetch call or a duplicate of these types.
- **Accounts and membership additions:** decision-log Entry 29 (the ADR and the orchestrator ruling, including `ba`'s contract-level defaults), written into this document by `t-am-contract-doc` (story `s-am-contract`, milestone `m5-accounts-membership`). They unblock `t-am-contract-models`, `t-am-migration-0003` and `t-am-design-spec`, and through them every `t-am-*` build task.

---

## Outstanding / not yet built

Everything below is known and deliberate, not an oversight:

1. **All eight endpoints are built — this item is no longer outstanding, and is kept only to record which task built what.** Corrected 2026-09-16: this entry previously read "six of the eight are still unbuilt", which stopped being true across milestone `m2-core-api` and was not updated as each one landed. Suite green at 395 tests, verified 2026-09-16.

   | Endpoint | Task | Commit |
   |---|---|---|
   | `GET /api/trips/{slug}` | `t-trip-metadata-endpoint` | `90408f4` |
   | `GET /api/trips/{slug}/stops` | `t-stops-list-endpoint` | `3ebded7` |
   | `POST /api/trips/{slug}/stops` | `t-stops-create-endpoint` | `c80f011` |
   | `POST /api/trips/{slug}/stops/{id}/photos` | `t-photo-upload-endpoint` | `a628259` |
   | `GET /api/trips/{slug}/stops/{id}/photos` | `t-photo-list-endpoint` | `a628259` |
   | `POST /api/trips/{slug}/bikes` | `t-bikes-create-endpoint` | `ae96533` |
   | `PATCH /api/trips/{slug}/bikes/{id}` | `t-bikes-patch-endpoint` | `ae96533` |
   | `GET /api/trips/{slug}/map` | `t-map-geojson-endpoint` | `f8fdc9b` |

   **"Built" is not "the story is closed."** Three of the M2 stories were reverted from `done` to `in_progress` on 2026-09-16 because they still hold open tasks. Most of those have since closed: the undescribed-field tasks (`BikeCreate`/`BikePatch` 2026-09-17; `PhotoOut` 2026-09-17 by `t-photoout-field-descriptions`; the photo **form** fields 2026-09-17, absorbed into `t-takenat-tz-question`) and the `takenAt` timezone question (item 5 below, now **ruled**). The rest closed later. The two access-control guards under `s-stop-crud` (`t-route-dependency-audit`, `t-trip-context-slug-exposure`) are done, and `s-stop-crud` is closed (2026-09-29). The OneDrive sync half of `s-photo-upload-onedrive-sync` is **built and scheduled**: `t-onedrive-sync-job` shipped `backend/app/storage/onedrive_sync.py`, and `t-onedrive-sync-scheduler` defined the Container Apps Job that runs it every 30 minutes (`infra/azure/README.md`; defined, provisioned at cutover). What remains on that story is verification against real Graph (`t-onedrive-preflight-check`, an owner step, and `t-onedrive-graph-name-charset`). See `docs/progress-notes.md` under `m2-core-api` for the history. The six files in `backend/app/models/` remain the contract; the OpenAPI document now carries all eight routes plus `ErrorEnvelope`, so a generated Kubb client covers the whole surface.
2. **The global exception handlers are done.** Task `t-error-envelope-handlers` (`backend/app/core/errors.py` + `main.py` wiring) closed after QA. FastAPI's default `422`, `405` and `500` bodies are normalised onto `ErrorEnvelope` by those handlers, so they are no longer response shapes that differ from every other failure. `backend/app/models/common.py` defines the shape — including the two codes added for `405`/`500` (see Error envelope, above) — and the wiring renders it. `t-validation-message-offset` — a `422` message exposing a pydantic byte offset as the field path — **closed 2026-09-17**: an unparseable body now returns a fixed clause, verified on real uvicorn at three different decode offsets (see "`message` and the `INTERNAL_ERROR` leak boundary", above). The OpenAPI half of that remainder, `t-openapi-error-responses`, **closed 2026-09-28**: every declared non-2xx on all eight operations now `$ref`s `ErrorEnvelope` through one shared constant (`backend/app/api/responses.py`), and FastAPI's auto-injected `HTTPValidationError`/`ValidationError` are gone from `components.schemas` — see "What the OpenAPI document declares", above, and `docs/decision-log.md` Entry 17. Corrected 2026-09-16: `t-conflict-code-impl` used to head that list as "contract, not yet code". It is code — `HTTPStatus.CONFLICT: ErrorCode.CONFLICT` is wired at `backend/app/core/errors.py:80` (commit `7ca82ab`; the line has since moved), so a `409` renders a `CONFLICT` envelope and no longer falls through to `INTERNAL_ERROR`. The one consequence of that mapping being keyed on **status** was that a bare `HTTPException(409)` gets a well-formed envelope without passing through `ApiError.conflict`, where the message leak boundary lives. That is now guarded at the raise site by `backend/tests/test_bare_409_raise_audit.py` (`t-bare-409-envelope-bypass`, closed 2026-09-29); the mapping row stays (Entry 14).
3. **Handler-side map behaviour is unenforced by the models.** Sorting stops by `arrivedAt` before building the trail, and omitting the trail below 2 stops, are both route-handler responsibilities. `backend/app/models/map.py` encodes the shape and the ">= 2 positions" constraint; it cannot enforce that positions arrive in the right order.
4. **Replay detection is a handler responsibility too.** The models carry the client-generated `id`; recognising an already-seen id and returning the stored record with `200` is implemented in the route + `data/` layer, and needs its own tests per spec Section 12's offline-queue priority. **The cross-parent branch is part of that same responsibility** — nothing in the models can express it. `id` is a global primary key and the parent is a separate column, so distinguishing "replay" from "this id belongs to another trip" is a `(parent, id)` lookup the handler must perform before inserting; a model can neither see the parent nor stop the wrong lookup being written.
5. **The photo form's `takenAt` — RULED 2026-09-17, no longer outstanding.** It is an offset-aware instant; a naive value is a `422` / `VALIDATION_ERROR`. See "`takenAt` on the photo upload form must be timezone-aware", above, for the ruling and `docs/decision-log.md` Entry 16 for the two arguments it overruled. This item is kept only to record that the question was open and how it closed.

   The EXIF premise that kept it open — `DateTimeOriginal` is naive by design, so "reject naive" may be unsatisfiable for photos — was **tested and failed**: EXIF is never the wire source, the frontend composes the field, and `getTimezoneOffset()` is always available. The contract section carries the resulting frontend ladder.

   Two things the earlier text said that are **no longer true**, called out because they were deliberate and someone may remember them: `takenAt` is not "unruled", and `PhotoOut.takenAt`'s description no longer makes "no timezone claim at all". That silence was correct while nothing enforced an offset — a description is contract text Kubb ships into the generated client, and a rule stated there would have settled the question by the back door. Now the server enforces it, so the description states it, and the ratchet test that pinned the silence word-by-word was **inverted into a positive pin**. Both strings changed in the same patch that changed the type (`t-takenat-tz-question`), together with the inline form-field description on the route, which previously read "ISO 8601 timestamp when the photo was taken." — a phrase that settled nothing, since ISO 8601 admits both naive and offset-carrying forms.
6. **The production trip is not seeded yet — only the human run is pending.** The seed script is done (`t-seed-trip-script`, `backend/app/data/seed_trip.py`). What remains is the owner running it once against Neon (`t-owner-production-seed`, runbook §4), which is deliberately not agent work because it prints the permanent slugs. Until then the production database has no trip, and every endpoint there answers `404`. Slug values live only in the database — none appear in this document, and `{slug}` throughout is a placeholder.
7. **The Entry 29 surface is contract only — nothing of it is built yet.** Accounts, sessions, CSRF, rate limits, the membership gate, the `/api/v2` routes, the photo JPEG/EXIF/15 MiB rules, the two new `ErrorCode`s and migration 0003 are all specified above and tracked as the `t-am-*` tasks in `docs/progress.json` (milestone `m5-accounts-membership`). Until each lands, the running code behaves as the pre-Entry 29 text described. Three gaps are filed as debt rather than contract: private app-created trips have no join path (`t-am-private-trip-invites`), `displayName` can't be changed after signup (`t-am-display-name-edit`), and there is no per-stop "hide from public" (`t-am-stop-hide-from-public`).
