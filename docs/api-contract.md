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

On the server:

- **Unseen id** → create the record, return `201`.
- **Id that already exists** → return the **existing** record with `200`. Do not create a duplicate. Do not return a conflict error.

Applies to all three create endpoints:

- `POST /trips/{slug}/stops` (`StopCreate.id`)
- `POST /trips/{slug}/stops/{id}/photos` (`PhotoCreateForm.id`)
- `POST /trips/{slug}/bikes` (`BikeCreate.id`)

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

`code` is one of exactly three values:

| `code` | Meaning |
|---|---|
| `FORBIDDEN` | Valid slug, but it's the viewer slug on a write endpoint |
| `NOT_FOUND` | No trip has this slug at all — or the stop/bike id doesn't exist |
| `VALIDATION_ERROR` | Request body or form failed schema validation |

`message` is human-readable and safe to show a rider directly.

One shape for every failure is what lets the frontend have a single error path. Kubb generates the client from the OpenAPI spec, so if half the endpoints failed in one shape and half in another, every call site would need to branch on which.

**FastAPI's default 422 does not look like this.** Out of the box, a validation failure returns FastAPI's own `{"detail": [...]}` structure. That has to be overridden by a global exception handler that maps validation errors onto the envelope above with `code: "VALIDATION_ERROR"` — otherwise a `422` is the one response the generated client can't parse like the others. **That handler is Week 2 work and is not built yet** (see Outstanding, below). The envelope model exists; the wiring does not.

---

## Endpoints

Eight endpoints, matching spec Section 5. No others. There are no update or delete endpoints for photos in v1 — upload is the only photo write path.

| Method | Path | Slug accepted | Request model | Response model | Status codes |
|---|---|---|---|---|---|
| `GET` | `/trips/{slug}` | either | — | `TripOut` | 200, 404 |
| `GET` | `/trips/{slug}/stops` | either | — | `StopOut[]` | 200, 404 |
| `POST` | `/trips/{slug}/stops` | rider only | `StopCreate` | `StopOut` | 201 new, 200 replay, 403, 404, 422 |
| `POST` | `/trips/{slug}/stops/{id}/photos` | rider only | multipart: `PhotoCreateForm` fields + `file` | `PhotoOut` | 201 new, 200 replay, 403, 404, 422 |
| `GET` | `/trips/{slug}/stops/{id}/photos` | either | — | `PhotoOut[]` | 200, 404 |
| `POST` | `/trips/{slug}/bikes` | rider only | `BikeCreate` | `BikeOut` | 201 new, 200 replay, 403, 404, 422 |
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

**`POST /trips/{slug}/stops`** — `StopCreate` carries the client-generated id, coordinates, `locationSource`, `arrivedAt` and optional notes. Replay returns the existing stop with `200`.

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

## Related work

- **Introduced by:** story `s-api-contract` (Session 1), tasks `t-error-envelope-model`, `t-trip-model`, `t-stop-model`, `t-bike-model`, `t-photo-model`, `t-map-model`, and this document, `t-api-contract-doc`.
- **Depends on:** `docs/architecture-diagram.md` (Session 0) for where `data/` and `storage/` sit; spec Sections 3, 4, 5.
- **Unblocks:** milestone `m2-core-api` — stories `s-trip-metadata-endpoint`, `s-stop-crud`, `s-photo-upload-onedrive-sync`, `s-bike-management`, `s-map-geojson-endpoint` — and, through the generated client, Week 3's frontend stories including `s-offline-queue`.
- **Consumed by:** the frontend API client in `frontend/src/api/`, generated by Kubb from the backend's OpenAPI spec. Nothing on the frontend hand-writes a fetch call or a duplicate of these types.

---

## Outstanding / not yet built

Everything below is known and deliberate, not an oversight:

1. **No route handler exists.** Not one of the eight endpoints is implemented. The six files in `backend/app/models/` *are* the contract as it stands; Week 2 implements against them. Until handlers exist there is no OpenAPI spec to generate a Kubb client from either.
2. **The global exception handler is not written.** FastAPI's default `422` body does not match `ErrorEnvelope`, and nothing currently normalises it. Until that handler lands, a validation failure is the one response shape that differs from every other failure. `backend/app/models/common.py` defines the shape only — the wiring is Week 2 work.
3. **Handler-side map behaviour is unenforced by the models.** Sorting stops by `arrivedAt` before building the trail, and omitting the trail below 2 stops, are both route-handler responsibilities. `backend/app/models/map.py` encodes the shape and the ">= 2 positions" constraint; it cannot enforce that positions arrive in the right order.
4. **Replay detection is a handler responsibility too.** The models carry the client-generated `id`; recognising an already-seen id and returning the stored record with `200` is implemented in the route + `data/` layer, and needs its own tests per spec Section 12's offline-queue priority.
5. **No trip record is seeded yet.** Story `s-seed-trip-record` still has to put the single trip and its two slugs in Postgres before any of these endpoints can return anything. Slug values live only in the database — none appear in this document, and `{slug}` throughout is a placeholder.
