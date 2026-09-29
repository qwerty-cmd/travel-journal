# Components

Shared UI used by the routes:

- `TripMap.tsx`: Leaflet map of pins and trail; also the add-stop location picker.
- `Timeline.tsx`: the chronological stop list on the trip home.
- `DisplayNamePrompt.tsx`: the rider's one-time "Your name" form.

The offline queue notice (`QueueNotice.tsx`) is in `../offline/`, next to the
queue it reads. The stop photo thumbnails and enlarged view live inline in
`routes/t.$slug.stops.$stopId.tsx`, and the bike cards inline in
`routes/t.$slug.bikes.tsx`. There is no separate gallery, badge or bike-card
component.

Each component's top doc comment follows the "Design feature → Design format →
APIs called" format. TripMap's is repeated below.

## TripMap (`TripMap.tsx`)

- **Design feature.** The Leaflet map of a trip: one pin per stop plus the
  trail. With no stops (or while loading) it shows Australia. Clicking a pin
  opens that stop's detail screen. Used by the trip home (`/t/$slug/`) and,
  as a location picker, by the add-stop form (`/t/$slug/add`) when GPS is
  unavailable.
- **Design format.** Leaflet directly, no React wrapper. Props:
  - `collection?: MapFeatureCollection`: the pins and trail. Undefined or
    empty shows Australia.
  - `onMapClick?: (lat: number, lng: number) => void`: optional. When given,
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
