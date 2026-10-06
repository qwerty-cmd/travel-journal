import { AlertTriangleIcon, CrosshairIcon, MapPinIcon, PinApproxIcon } from "../icons";
import "./LocationStatus.css";

// Design feature: where the stop being added is (docs/design/screens/add-stop.md,
// DESIGN.md §5 LocationStatus), so the rider knows whether to wait for GPS or
// set the point on the map.
// Design format: a card row, 24 px icon + title + detail, polite live region.
//   - pending: crosshair (slow pulse, static under reduced motion) "Getting your location…"
//   - gps: map-pin (success) "Location found (GPS)" + coords to 5 dp, tabular
//   - fallback: alert-triangle (warning) "GPS unavailable: tap the map to set the location"
//   - manual: pin-approx "Location set on the map" + coords + "Shown as approximate location."
// APIs called: none.
export type LocationState =
  | { kind: "pending" }
  | { kind: "fallback" }
  | { kind: "gps" | "manual"; lat: number; lng: number };

const coords = (lat: number, lng: number) => `${lat.toFixed(5)}, ${lng.toFixed(5)}`;

export function LocationStatus({ state }: { state: LocationState }) {
  const [icon, title, detail] =
    state.kind === "pending"
      ? [<CrosshairIcon key="i" />, "Getting your location…", "This can take a few seconds outdoors."]
      : state.kind === "fallback"
        ? [<AlertTriangleIcon key="i" />, "GPS unavailable: tap the map to set the location", "Or pan the map and tap Use map centre."]
        : state.kind === "gps"
          ? [<MapPinIcon key="i" />, "Location found (GPS)", <span className="location__coords">{coords(state.lat, state.lng)}</span>]
          : [
              <PinApproxIcon key="i" />,
              "Location set on the map",
              <>
                <span className="location__coords">{coords(state.lat, state.lng)}</span> Shown as approximate location. Move it:
                tap the map again.
              </>,
            ];
  return (
    <div className={`location location--${state.kind}`} role="status">
      <span className="location__icon">{icon}</span>
      <div className="location__text">
        <p className="location__title">{title}</p>
        <p className="location__detail">{detail}</p>
      </div>
    </div>
  );
}
