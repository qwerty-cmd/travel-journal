/// <reference types="vite/client" />
import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import type { MapFeatureCollection } from "../api/gen/types/MapFeatureCollection";
import type { StopFeatureProperties } from "../api/gen/types/StopFeatureProperties";

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
  el.appendChild(document.createTextNode(new Date(arrivedAt).toLocaleString()));
  return el;
}

/**
 * Trip map (spec Section 6, screen 2): one pin per stop plus the trail, from
 * `GET /api/trips/{slug}/map`. GeoJSON is [lng, lat]; L.geoJSON converts it,
 * so coordinates are never swapped by hand. `collection` undefined (still
 * loading) or empty shows Australia.
 */
export function TripMap({ collection }: { collection?: MapFeatureCollection }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);

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
        if (feature.geometry.type === "Point") l.bindPopup(popupFor(feature.properties));
      },
    }).addTo(map);
    if (collection.features.length) map.fitBounds(layer.getBounds(), { maxZoom: 12 });
    else map.setView(AUSTRALIA, 4);
    return () => {
      layer.remove();
    };
  }, [collection]);

  return <div ref={containerRef} style={{ height: "50vh" }} />;
}
