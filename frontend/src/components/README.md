# Components

Shared UI: map view (Leaflet), photo gallery/lightbox, stop timeline card,
offline-indicator badge, bike spec card. Built alongside the routes that use
them, Weeks 3–4.

## TripMap (`TripMap.tsx`)

- **Design feature.** The Leaflet map of a trip: one pin per stop plus the
  trail. With no stops (or while loading) it shows Australia. Clicking a pin
  opens that stop's detail screen. Used by the trip home (`/t/$slug/`) and,
  as a location picker, by the add-stop form (`/t/$slug/add`) when GPS is
  unavailable.
- **Design format.** Leaflet directly, no React wrapper. Props:
  - `collection?: MapFeatureCollection` — the pins and trail. Undefined or
    empty shows Australia.
  - `onMapClick?: (lat: number, lng: number) => void` — optional. When given,
    every map tap calls it with the tapped position. The longitude is wrapped
    into -180..180 (`latlng.wrap()`), so a tap on a panned-around copy of the
    world still gives a valid coordinate. The add-stop form uses it to set a
    `"manual"` location. The click listener is removed on unmount or when the
    callback changes.
- **APIs called.** None itself. The trip home passes the result of
  `GET /api/trips/{slug}/map`; the add-stop form passes no collection.

Full per-task history: `docs/progress-notes.md`, sections
`t-frontend-map-pins-trail`, `t-frontend-stop-detail-gallery` and
`t-frontend-add-stop-form`.
