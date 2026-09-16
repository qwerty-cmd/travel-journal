# API Contract — Session 1

The consolidated, human-readable record of the API contract agreed in Session 1 (spec Section 10). It sits alongside the Pydantic models in `backend/app/models/` — the models are the machine-readable contract, this file is the reasoning behind them.

Status: **settled**. Weeks 2–3 build against this. Once route handlers exist, FastAPI's auto-generated OpenAPI spec becomes the always-in-sync version of the same information (and is what Kubb generates the frontend client from); this document stays as the record of *why* the shapes are what they are.

This file deliberately does not restate every field of every model — field lists drift the moment code changes. It names models and points at the file they live in. The endpoint table is the one exception, and it names models, not fields.

---

## Context

Spec Section 5 lists eight endpoints as a rough sketch. This contract turns that sketch into something both sides can be built against independently: request/response models, status codes, one error shape, and a defined answer to "what happens when the offline queue retries a write the server already accepted."

That last question is the reason this document exists at all. The app is used on the Stuart Hwy with long dead zones — a write is captured on the device first and sent later, sometimes much later, sometimes twice. If the contract didn't pin down retry behaviour, the frontend's offline queue and the backend's route handlers would each invent their own answer, and the disagreement would only show up as duplicate stops in the finished trip journal.

The build gate (spec Section 10, CLAUDE.md) is architecture → contract → build. This document closes the contract stage for milestone `m2-core-api`.

---

## How it works

### Access: two slugs, no accounts

Every path is scoped by `{slug}`. A trip has two unguessable slugs — a rider slug (read + write) and a viewer slug (read only). There are no accounts, no passwords, and no sessions; the slug in the URL *is* the authorisation. Whoever was given which link is the whole access model.

Read endpoints accept either slug. Write endpoints accept the rider slug only.

The server tells the frontend which kind of slug was used via `TripOut.access` (`"rider"` | `"viewer"` — `Access` in `backend/app/models/trip.py`). The frontend decides whether to render write UI (Add stop, upload photo, edit bikes) from that value, not from anything stored on the device. The API still rejects writes made against a viewer slug regardless of what the UI chose to render — the flag is a UI hint, not the enforcement point.

### Idempotency — client-generated ids

**This is the most consequential decision in the contract, and the mechanism that makes the offline queue safe.**

The client generates the entity's `id` — a UUID4 — at capture time. Not at send time: at the moment the rider taps "Add stop" or takes a photo, which may be hours earlier and entirely offline. That id travels in the create request body.

On the server, a create request's id falls into exactly one of three branches:

- **Unseen id** → create the record, return `201`.
- **Id already exists under this same parent** → a replay. Return the **existing** record with `200`. Do not create a duplicate, and do not return an error.
- **Id exists under a *different* parent** → `409` / `CONFLICT`. Nothing is created, and nothing about the conflicting record is disclosed.

This three-way branch applies identically to all three create endpoints. It is stated once here rather than per endpoint, because it follows from the schema rather than from any one route:

- `POST /trips/{slug}/stops` (`StopCreate.id`) — parent is the trip
- `POST /trips/{slug}/stops/{id}/photos` (`PhotoCreateForm.id`) — parent is the stop
- `POST /trips/{slug}/bikes` (`BikeCreate.id`) — parent is the trip

**Why the third branch exists at all.** Each of `stops`, `photos` and `bikes` has a **global** primary key on `id` plus a *separate* parent foreign key — `stops.trip_id`, `photos.stop_id`, `bikes.trip_id` (`backend/app/data/tables.py`). An id is therefore unique across the whole table, while the parent it belongs to is a different column entirely. So "this id already exists" and "this id already exists *here*" are two different questions, and only the second one means replay.

**The lookup rule.** Replay is matched on the **parent-and-id pair** — `(trip_id, id)` for stops and bikes, `(stop_id, id)` for photos — and **never on `id` alone.** A lookup by `id` alone would happily return another trip's stop through this trip's slug: a cross-trip leak, which spec Section 12 ranks as the top-priority failure class.

**The cross-parent branch is detected by an explicit check, never by letting an `INSERT` fail.** Allowing the primary-key violation to surface from the driver renders `500` / `INTERNAL_ERROR` — and the offline queue branches on `code` to decide retry-vs-never-retry, so it would retry forever a request that can never succeed. The handler looks first and decides which of the three branches applies. The database constraint stays as the backstop it is, not the mechanism.

Why it matters: the failure mode this protects against is not a user double-tapping. It's the connection dropping *after* the server committed the write but *before* the response reached the phone. From the queue's point of view that is indistinguishable from a total failure, so it retries — correctly. Because the id was fixed on the device before the first attempt, the retry carries the same id, and the server recognises it. The queue can therefore retry any create, any number of times, without needing to know whether the earlier attempt actually landed. That is the entire safety argument for retrying writes at all.

A replay returning `200` with the stored record (rather than `204`, or an error) also means the queue always has a real entity to reconcile its local copy against, whichever attempt succeeded.

Note that `id` is typed `str` in the models, with "client-generated UUID4" expressed in the field description rather than in the type. The generating side is the client; the models do not enforce the format.

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

`code` is one of exactly six values:

| `code` | Meaning |
|---|---|
| `FORBIDDEN` | Valid slug, but it's the viewer slug on a write endpoint |
| `NOT_FOUND` | No trip has this slug at all — or the stop/bike id doesn't exist |
| `VALIDATION_ERROR` | Request body or form failed schema validation |
| `METHOD_NOT_ALLOWED` | The path exists but not for this HTTP method — a client mistake, not a server fault |
| `CONFLICT` | A client-generated id in a create request already exists under a **different** parent record |
| `INTERNAL_ERROR` | Anything else, including an unhandled server-side exception |

#### Status → code mapping

This is the mapping the global exception handler implements. It is exhaustive by construction: there is no status a response can carry that does not land on a code.

| HTTP status | `code` |
|---|---|
| 403 | `FORBIDDEN` |
| 404 | `NOT_FOUND` |
| 422 | `VALIDATION_ERROR` |
| 405 | `METHOD_NOT_ALLOWED` |
| 409 | `CONFLICT` |
| anything else, including an unhandled 500 | `INTERNAL_ERROR` |

`METHOD_NOT_ALLOWED` and `INTERNAL_ERROR` were added after the original three proved insufficient to keep the "every non-2xx uses this envelope" promise. Both statuses are reachable today: `POST /api/health` hits a real route that accepts `GET` and `HEAD` only, so it returns a `405`, and an unhandled exception is a `500` by definition. With only three codes, neither had a legal value to report.

`CONFLICT` was added later still, for the same reason and by the same route. A create whose client-generated id already exists under a *different* parent is **neither a validation failure nor a server fault** — the request is well-formed and the server is healthy — so among the five existing codes it had no legal value either. `ba` escalated rather than inventing one, exactly as it had for the first two; the user ruled. See `docs/decision-log.md` Entry 14 for the four readings that were rejected, including the composite-primary-key option that would have made the branch unreachable.

A `405` requires a **registered path with a different method** — it is not what an unregistered path returns. `DELETE /trips/{slug}/stops` returns `405` with `Allow: GET, HEAD`, because the stops router registers `GET` on that path (plus its schema-excluded `HEAD` sibling): Starlette matches the route, and the method is what mismatches. The same request returned `404` for as long as the stops router was an empty `APIRouter` stub with no methods registered — Starlette matched no route at all, so there was nothing for the method to mismatch against. (An earlier revision of this document used that request as the `405` example *while it was still a `404`*; `qa` ran it and found `404`. See `docs/decision-log.md` Entry 6.)

> **Expiry fired (2026-09-15).** The forward-note that stood here warned that this paragraph's
> example had an expiry date, and it has now come due — the paragraph above is the re-worded version.
> `t-stops-list-endpoint` registered `GET` and its `HEAD` sibling on `/trips/{slug}/stops`
> (`3ebded7`), so the path exists and the response is `405` rather than `404`.
> **The new value was verified, not predicted:** `test_spa_delete_on_the_registered_stops_path_is_405`
> asserts `405` with `Allow` exactly `{"GET", "HEAD"}`, and it passes. That verification was the point
> of the old note — `t-405-router-route-collapse` landing is what stops a registered path *silently
> staying* `404` for the wrong reason, and the two failures look identical from the outside.
> The `Allow` set becomes `{"GET", "HEAD", "POST"}` when `t-stops-create-endpoint` lands.
> The surrounding rule ("a `405` requires a registered path with a different method") did not expire —
> only the example did. Closed as `t-stops-405-doc-revisit`.

`METHOD_NOT_ALLOWED` and `INTERNAL_ERROR` are two codes rather than one catch-all because **a `405` is a client error and a `500` is a server fault** — collapsing them would make the envelope inaccurate about which side went wrong. That distinction is not cosmetic: the offline queue decides retry-vs-never-retry programmatically from `code`, and "the server is broken, try later" and "this request can never succeed as written" are opposite answers.

**Which side of that branch each code falls on is contract, not handler preference:**

- **Never retry** — `VALIDATION_ERROR`, `METHOD_NOT_ALLOWED`, `CONFLICT`. The request cannot succeed as written, however long the queue waits. `CONFLICT` belongs here because a client-generated id that already belongs to another parent will still belong to it on the next attempt; retrying is guaranteed to produce the same `409`.
- **Retry** — `INTERNAL_ERROR`. The opposite answer: the server broke, and a later attempt may work.

**A never-retry outcome is not a silent drop.** The queued item is **dequeued *and* surfaced to the rider.** This is a data-integrity rule, not a UX nicety: the rider tapped "Add stop", believes that stop was captured, and may be hours from anywhere. Dropping it quietly is a data-loss path — and the rider is the only one who can decide what to do with a capture that cannot be sent as it stands.

#### `message` and the `INTERNAL_ERROR` leak boundary

`message` is human-readable and **documented as safe to show a rider directly**. That guarantee is what makes the single frontend error path possible — the UI renders `message` without sanitising or whitelisting it.

It follows, and the implementation is bound by it, that **the `INTERNAL_ERROR` message is always a fixed generic string.** The originating exception text is logged server-side and never appears in the response body. This is a leak boundary, not politeness: a raw database error can carry the database host, user and query fragments, and the rider-facing error surface is a public one — the slug in the URL is the only access control there is.

The other five codes carry messages written for the situation, because they describe conditions the caller is allowed to know about.

**`CONFLICT` carries a second, narrower leak boundary.** Its `message` is rider-facing and returned **verbatim**, like the rest — and it **must contain no value whatsoever from the conflicting record**: not the other trip's slug, id or name, and not the other stop's name, notes, coordinates or timestamp. A `409` already tells the caller that the id exists somewhere; the message must add nothing to that. This is enforced **at the raise site** — the code that raises it does not read the conflicting row in the first place — and not by filtering in the global handler, which only ever sees the string it was handed. The whole point of this branch is that the other trip stays invisible: the unguessable slug is the entire access model, and a conflict on a shared global id namespace is the one place where a request about *this* trip is evaluated against a row belonging to *another*.

One shape for every failure is what lets the frontend have a single error path. Kubb generates the client from the OpenAPI spec, so if half the endpoints failed in one shape and half in another, every call site would need to branch on which — including the offline queue, which spec Section 12 ranks in the top three for testing rigor. Two parsing paths there would mean two places for retry logic to be wrong.

**FastAPI's default 422 does not look like this.** Out of the box, a validation failure returns FastAPI's own `{"detail": [...]}` structure, and a `405` or an unhandled `500` returns `{"detail": "..."}`. All of those have to be overridden by global exception handlers that map onto the envelope above — otherwise they are the responses the generated client can't parse like the others. **Those handlers are implemented and wired** (task `t-error-envelope-handlers`, closed), so the envelope above is what every non-2xx response actually returns today. Read that precisely: the shape is not free and it is not FastAPI's. It holds *because* those global handlers override the defaults — anything that bypasses them returns FastAPI's structure, not this one, which is what makes the override the load-bearing part rather than a formality. The narrower gaps that remain are tracked by id under Outstanding, below.

**`405` and `500` are framework-level responses, not endpoint contract.** They are reachable from any path and are therefore deliberately *not* listed in the per-endpoint status codes below. No endpoint declares a 405 or a 500 row. The envelope guarantee still covers them — that guarantee is global, which is precisely why it doesn't belong in a per-endpoint column.

**`409` is the counterpart case: it *is* endpoint contract, and it is listed per-endpoint.** It is raised by our own handler code rather than by the framework, and it is reachable only on the **three create endpoints** — the only places that accept a client-generated id. It therefore appears in the per-endpoint status codes below, on exactly those three rows and nowhere else.

---

## Endpoints

Eight endpoints, matching spec Section 5. No others. There are no update or delete endpoints for photos in v1 — upload is the only photo write path.

| Method | Path | Slug accepted | Request model | Response model | Status codes |
|---|---|---|---|---|---|
| `GET` | `/trips/{slug}` | either | — | `TripOut` | 200, 404 |
| `GET` | `/trips/{slug}/stops` | either | — | `StopOut[]` | 200, 404 |
| `POST` | `/trips/{slug}/stops` | rider only | `StopCreate` | `StopOut` | 201 new, 200 replay, 403, 404, 409, 422 |
| `POST` | `/trips/{slug}/stops/{id}/photos` | rider only | multipart: `PhotoCreateForm` fields + `file` | `PhotoOut` | 201 new, 200 replay, 403, 404, 409, 422 |
| `GET` | `/trips/{slug}/stops/{id}/photos` | either | — | `PhotoOut[]` | 200, 404 |
| `POST` | `/trips/{slug}/bikes` | rider only | `BikeCreate` | `BikeOut` | 201 new, 200 replay, 403, 404, 409, 422 |
| `PATCH` | `/trips/{slug}/bikes/{id}` | rider only | `BikePatch` | `BikeOut` | 200, 403, 404, 422 |
| `GET` | `/trips/{slug}/map` | either | — | `MapFeatureCollection` | 200, 404 |

Models by file:

- `TripOut`, `Access` — `backend/app/models/trip.py`
- `StopCreate`, `StopOut`, `LocationSource` — `backend/app/models/stop.py`
- `PhotoCreateForm`, `PhotoOut` — `backend/app/models/photo.py`
- `BikeCreate`, `BikePatch`, `BikeOut` — `backend/app/models/bike.py`
- `MapFeatureCollection` and its feature/geometry/properties models — `backend/app/models/map.py`
- `ErrorCode`, `ErrorDetail`, `ErrorEnvelope` — `backend/app/models/common.py`

### Notes per endpoint

**`GET /trips/{slug}`** — returns trip metadata plus **this trip's** bikes embedded in `TripOut.bikes`, so the app's first load is one request rather than two. It also carries `access`, which is how the frontend knows whether to render write UI at all (see Access, above).

**`GET /trips/{slug}/stops`** — the full stop list, including `notes` and `locationSource`. The map endpoint deliberately doesn't duplicate those; this is where they come from.

**`POST /trips/{slug}/stops`** — `StopCreate` carries the client-generated id, coordinates, `locationSource`, `arrivedAt` and optional notes. The id decides a three-way branch (see Idempotency, above): an **unseen** id creates the stop and returns `201`; an id **already on this trip** is a replay and returns the existing stop with `200`; an id that exists **on a different trip** returns `409` / `CONFLICT` with nothing created and nothing about the other trip disclosed — not in the body, not in the message. The replay lookup is on `(trip_id, id)`, and the cross-trip case is found by checking, not by letting the insert fail.

**`POST /trips/{slug}/stops/{id}/photos`** — multipart. `PhotoCreateForm` describes the form *fields* only; the binary part (`file`) is a separate multipart part and is not a field on the Pydantic model, because Pydantic models don't carry binary parts. `{id}` here is the stop id the photo attaches to. Replay of a known photo id returns the existing photo with `200` rather than storing the bytes twice.

**`GET /trips/{slug}/stops/{id}/photos`** — photos for one stop. See Photo serving, below, for what `url` actually is.

**`POST /trips/{slug}/bikes`** — same replay semantics as stops.

**`PATCH /trips/{slug}/bikes/{id}`** — every field on `BikePatch` is optional; only fields actually present in the request body change. Absent fields are left alone, and are not the same as a field explicitly set to null. There is no replay/`201` case here — a patch against a known bike is a plain `200`, and against an unknown bike id a `404`. Two riders editing the same bike resolve last-write-wins by design (spec Section 4); there is no conflict status code.

**`GET /trips/{slug}/map`** — see Map response, below.

---

## Access control: 403 and 404 are different answers

This distinction is spec Section 12's top-priority test area, and the two must never be conflated:

- **`403` / `FORBIDDEN`** — the slug is valid and resolves to a real trip, but it's the **viewer** slug and this is a write endpoint. The caller is looking at a real trip; they just can't write to it.
- **`404` / `NOT_FOUND`** — **no trip has this slug at all.** Nothing resolves.

Collapsing them in either direction is a real bug, not a cosmetic one. Returning `404` for a viewer-slug write makes a read-only guest think their link is broken. Returning `403` for a nonexistent slug confirms to anyone guessing URLs that they can distinguish "wrong slug" from "right slug, wrong permission", which is exactly the signal the unguessable-slug model relies on not leaking.

`NOT_FOUND` is also used for an unknown stop or bike id under a valid slug.

---

## Photo serving

`PhotoOut.url` is a **presigned GET URL** into the S3-compatible object store, generated fresh at read time by the `storage/` module (`backend/app/storage/`). Three consequences worth being explicit about:

- It is **never persisted in the database.** The DB holds the object key; the URL is derived per request. A URL that appears in an old response is not a stable identifier and should not be stored or cached long-term by the client.
- It is **never a OneDrive URL.** OneDrive is a write-only archive reached by a background sync job. It is never in a read path, and nothing user-facing waits on it (spec Section 4, and `docs/architecture-diagram.md`).
- The object store is the source of truth the app always reads from, which is why an unhealthy or lapsed archive sync degrades nothing a viewer can see.

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

---

## Related work

- **Introduced by:** story `s-api-contract` (Session 1), tasks `t-error-envelope-model`, `t-trip-model`, `t-stop-model`, `t-bike-model`, `t-photo-model`, `t-map-model`, and this document, `t-api-contract-doc`.
- **Depends on:** `docs/architecture-diagram.md` (Session 0) for where `data/` and `storage/` sit; spec Sections 3, 4, 5.
- **Unblocks:** milestone `m2-core-api` — stories `s-trip-metadata-endpoint`, `s-stop-crud`, `s-photo-upload-onedrive-sync`, `s-bike-management`, `s-map-geojson-endpoint` — and, through the generated client, Week 3's frontend stories including `s-offline-queue`.
- **Consumed by:** the frontend API client in `frontend/src/api/`, generated by Kubb from the backend's OpenAPI spec. Nothing on the frontend hand-writes a fetch call or a duplicate of these types.

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

   **"Built" is not "the story is closed."** Three of the M2 stories were reverted from `done` to `in_progress` on 2026-09-16 because they still hold open tasks — undescribed fields on `BikeCreate`, `BikePatch`, `PhotoOut` and `PhotoCreateForm`, the unruled `PhotoCreateForm.takenAt` question (item 5 below), the two access-control guards under `s-stop-crud`, and the OneDrive sync half of `s-photo-upload-onedrive-sync`, which has no task at all. See `docs/progress-notes.md` under `m2-core-api`. The six files in `backend/app/models/` remain the contract; the OpenAPI document now carries all eight routes plus `ErrorEnvelope`, so a generated Kubb client covers the whole surface — including the models whose fields are still undescribed.
2. **The global exception handlers are done.** Task `t-error-envelope-handlers` (`backend/app/core/errors.py` + `main.py` wiring) closed after QA. FastAPI's default `422`, `405` and `500` bodies are normalised onto `ErrorEnvelope` by those handlers, so they are no longer response shapes that differ from every other failure. `backend/app/models/common.py` defines the shape — including the two codes added for `405`/`500` (see Error envelope, above) — and the wiring renders it. Two narrower remainders are tracked separately rather than here, both **promoted and scoped**: `t-validation-message-offset` (a `422` message can expose a pydantic byte offset as the field path) and `t-openapi-error-responses` (the shared `responses=` constant across the eight routes, and FastAPI's auto-injected, here-unreachable `422`; scoping it is itself the `ba` task, per its tracker row). Corrected 2026-09-16: `t-conflict-code-impl` used to head that list as "contract, not yet code". It is code — `HTTPStatus.CONFLICT: ErrorCode.CONFLICT` is wired at `backend/app/core/errors.py:72` (commit `7ca82ab`), so a `409` renders a `CONFLICT` envelope and no longer falls through to `INTERNAL_ERROR`. The one consequence of that mapping being keyed on **status** is tracked as `t-bare-409-envelope-bypass`: a bare `HTTPException(409)` gets a well-formed envelope without passing through `ApiError.conflict`, where the message leak boundary lives.
3. **Handler-side map behaviour is unenforced by the models.** Sorting stops by `arrivedAt` before building the trail, and omitting the trail below 2 stops, are both route-handler responsibilities. `backend/app/models/map.py` encodes the shape and the ">= 2 positions" constraint; it cannot enforce that positions arrive in the right order.
4. **Replay detection is a handler responsibility too.** The models carry the client-generated `id`; recognising an already-seen id and returning the stored record with `200` is implemented in the route + `data/` layer, and needs its own tests per spec Section 12's offline-queue priority. **The cross-parent branch is part of that same responsibility** — nothing in the models can express it. `id` is a global primary key and the parent is a separate column, so distinguishing "replay" from "this id belongs to another trip" is a `(parent, id)` lookup the handler must perform before inserting; a model can neither see the parent nor stop the wrong lookup being written.
5. **`PhotoCreateForm.takenAt` — an open question, not an answer.** Structurally it is the same field as `StopCreate.arrivedAt`: a bare `datetime` on a create model, landing in a `timestamptz` column, captured on-device and potentially offline. The obvious move is to apply the ruling above to it verbatim. **That has deliberately not been done**, because photos have a constraint stops do not: the timestamp's natural source is EXIF `DateTimeOriginal`, which is **naive by design** — the offset lives in a *separate* `OffsetTimeOriginal` tag that is frequently absent on imported, exported or edited images. So "reject naive" may be materially harder for the frontend to satisfy here than it is for stops, and the answer may have to be that the **frontend supplies the device offset at capture time** rather than trusting EXIF to carry one. Which of those it is gets decided when the photo endpoints are scoped (`s-photo-upload-onedrive-sync`), not before. Filed as `t-takenat-tz-question`. Until that lands, `PhotoCreateForm.takenAt` is **unruled** — do not read the `arrivedAt` subsection as having settled it by analogy.
6. **No trip record is seeded yet.** Story `s-seed-trip-record` still has to put the single trip and its two slugs in Postgres before any of these endpoints can return anything. Slug values live only in the database — none appear in this document, and `{slug}` throughout is a placeholder.
