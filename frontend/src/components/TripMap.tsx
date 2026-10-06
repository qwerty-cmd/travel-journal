/// <reference types="vite/client" />
import { useEffect, useRef } from "react";
import { useNavigate } from "@tanstack/react-router";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./TripMap.css";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import type { MapFeatureCollection } from "../api/gen/types/MapFeatureCollection";
import type { StopFeatureProperties } from "../api/gen/types/StopFeatureProperties";
import { formatInstant } from "../format";
import { CrosshairIcon } from "../icons";
import { Button } from "./Button";

// Leaflet guesses its marker image path from the CSS at runtime, which breaks
// under Vite's hashed assets. Hand it the bundled URLs instead; dropping
// _getIconUrl stops Default from prefixing them with the guessed path.
delete (L.Icon.Default.prototype as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({ iconUrl, iconRetinaUrl, shadowUrl });

const AUSTRALIA: L.LatLngTuple = [-25, 134];

function popupFor({ name, arrivedAt }: StopFeatureProperties) {
  // Built with textContent, not an HTML string: stop names are user input.
  const el = document.createElement("div");
  const title = el.appendChild(document.createElement("strong"));
  title.textContent = name;
  el.appendChild(document.createElement("br"));
  el.appendChild(document.createTextNode(formatInstant(arrivedAt)));
  return el;
}

/**
 * Trip map (spec Section 6, screen 2): one pin per stop plus the trail, from
 * `GET /api/trips/{slug}/map`. GeoJSON is [lng, lat]; L.geoJSON converts it,
 * so coordinates are never swapped by hand. `collection` undefined (still
 * loading) or empty shows Australia. Clicking a pin opens that stop's detail
 * screen, matched on the GeoJSON Feature.id (the stop id): `onOpenStop` when
 * given (used by /trips/$tripId), else /t/$slug/stops/$stopId. `onMapClick`, when
 * given, receives each tap's position (longitude wrapped into -180..180), used
 * by the add-stop form's manual-location fallback.
 *
 * Picker mode (`onUseCentre` given; /trips/$tripId/add, DESIGN.md C15): a
 * shorter map on the grid background with a decorative centre crosshair, and
 * under it a "Use map centre" button that hands back the map's current centre
 * (wrapped like a tap), so the location can be set with the keyboard alone
 * (Leaflet's arrow-key panning, then the button). `approxPoint` draws the
 * chosen point as the hollow dashed approximate pin.
 */
export function TripMap({
  collection,
  onMapClick,
  onOpenStop,
  onUseCentre,
  approxPoint,
}: {
  collection?: MapFeatureCollection;
  onMapClick?: (lat: number, lng: number) => void;
  onOpenStop?: (stopId: string) => void;
  onUseCentre?: (lat: number, lng: number) => void;
  approxPoint?: { lat: number; lng: number } | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    const map = L.map(containerRef.current!).setView(AUSTRALIA, 4);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !collection) return;
    const layer = L.geoJSON(collection as GeoJSON.FeatureCollection, {
      onEachFeature: (feature, l) => {
        if (feature.geometry.type !== "Point") return;
        l.bindPopup(popupFor(feature.properties));
        const stopId = String(feature.id);
        l.on("click", () =>
          onOpenStop
            ? onOpenStop(stopId)
            : navigate({ from: "/t/$slug", to: "/t/$slug/stops/$stopId", params: (p) => ({ ...p, stopId }) }),
        );
      },
    }).addTo(map);
    if (collection.features.length) map.fitBounds(layer.getBounds(), { maxZoom: 12 });
    else map.setView(AUSTRALIA, 4);
    return () => {
      layer.remove();
    };
  }, [collection, navigate, onOpenStop]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !onMapClick) return;
    const handler = (e: L.LeafletMouseEvent) => {
      const { lat, lng } = e.latlng.wrap();
      onMapClick(lat, lng);
    };
    map.on("click", handler);
    return () => {
      map.off("click", handler);
    };
  }, [onMapClick]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !approxPoint) return;
    const icon = L.divIcon({ className: "trip-map__approx-pin", iconSize: [24, 24] });
    const marker = L.marker([approxPoint.lat, approxPoint.lng], { icon, keyboard: false, interactive: false }).addTo(map);
    return () => {
      marker.remove();
    };
  }, [approxPoint]);

  if (!onUseCentre) return <div ref={containerRef} className="trip-map" />;

  function useCentre() {
    const map = mapRef.current;
    if (!map) return;
    const { lat, lng } = map.getCenter().wrap();
    onUseCentre!(lat, lng);
  }

  return (
    <div className="trip-map__picker">
      <div className="trip-map__frame">
        <div ref={containerRef} className="trip-map trip-map--picker" aria-label="Map: use the arrow keys to pan" />
        <span className="trip-map__crosshair" aria-hidden="true">
          <CrosshairIcon />
        </span>
      </div>
      <Button type="button" variant="secondary" className="trip-map__centre" onClick={useCentre}>
        Use map centre
      </Button>
    </div>
  );
}
