// The root route mounts the real QueueNotice, which reads the queue's IndexedDB.
import "fake-indexeddb/auto";
import { StrictMode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import L from "leaflet";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import { routeTree } from "../routeTree.gen";
import { TripMap } from "./TripMap";
import { formatInstant } from "../format";
import type { MapFeatureCollection } from "../api/gen/types/MapFeatureCollection";
import type { TripOut } from "../api/gen/types/TripOut";

// t-frontend-map-pins-trail: plain Leaflet trip map. Assertions go through
// Leaflet objects (jsdom has no layout, so no pixels or real tiles).

// Record every L.Map constructed, so tests can reach the component's instance.
const maps: L.Map[] = [];
L.Map.addInitHook(function (this: L.Map) {
  maps.push(this);
});

const ARRIVED = "2026-10-03T08:30:00Z";
const stop = (id: string, lng: number, lat: number, name = `Stop ${id}`) => ({
  type: "Feature" as const,
  id,
  geometry: { type: "Point" as const, coordinates: [lng, lat] },
  properties: { name, arrivedAt: ARRIVED },
});
const trail = (...coordinates: number[][]) => ({
  type: "Feature" as const,
  geometry: { type: "LineString" as const, coordinates },
  properties: {},
});
const fc = (...features: unknown[]) => ({ type: "FeatureCollection", features }) as MapFeatureCollection;

const layers = <T,>(map: L.Map, cls: new (...a: never[]) => T) => {
  const out: T[] = [];
  map.eachLayer((l) => {
    if (l instanceof cls) out.push(l as T);
  });
  return out;
};
const markers = (map: L.Map) => layers(map, L.Marker);
// Leaflet's L.Polygon extends L.Polyline; the contract never sends polygons.
const polylines = (map: L.Map) => layers(map, L.Polyline);
const liveMap = () => maps[maps.length - 1];

beforeEach(() => {
  maps.length = 0;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("AC1: OSM tile layer with attribution", () => {
  render(<TripMap />);
  const [tiles] = layers(liveMap(), L.TileLayer) as (L.TileLayer & { _url: string })[];
  expect(tiles._url).toBe("https://tile.openstreetmap.org/{z}/{x}/{y}.png");
  expect(tiles.getAttribution!()).toBe("© OpenStreetMap contributors");
});

test("AC2: GeoJSON [lng, lat] lands as Leaflet lat/lng, not swapped", () => {
  render(<TripMap collection={fc(stop("s1", 134.1, -19.6))} />);
  const [m] = markers(liveMap());
  expect(m.getLatLng().lat).toBe(-19.6);
  expect(m.getLatLng().lng).toBe(134.1);
});

test("AC3: LineString becomes exactly one polyline with points in order", () => {
  render(
    <TripMap
      collection={fc(stop("a", 130, -12), stop("b", 132, -14), trail([130, -12], [132, -14], [134.1, -19.6]))}
    />,
  );
  const lines = polylines(liveMap());
  expect(lines).toHaveLength(1);
  const pts = (lines[0].getLatLngs() as L.LatLng[]).map((p) => [p.lat, p.lng]);
  expect(pts).toEqual([
    [-12, 130],
    [-14, 132],
    [-19.6, 134.1],
  ]);
  expect(markers(liveMap())).toHaveLength(2); // the trail adds no pin
});

test.each([
  ["empty collection", fc()],
  ["no data yet", undefined],
])("AC4: %s centres on Australia at zoom 4 without throwing", (_, collection) => {
  const fit = vi.spyOn(L.Map.prototype, "fitBounds");
  expect(() => render(<TripMap collection={collection} />)).not.toThrow();
  const c = liveMap().getCenter();
  expect([c.lat, c.lng]).toEqual([-25, 134]);
  expect(liveMap().getZoom()).toBe(4);
  expect(fit).not.toHaveBeenCalled();
});

test("AC4: non-empty collection fits the feature bounds with maxZoom 12", () => {
  const fit = vi.spyOn(L.Map.prototype, "fitBounds");
  render(<TripMap collection={fc(stop("a", 130, -12), stop("b", 134.1, -19.6), trail([130, -12], [134.1, -19.6]))} />);
  expect(fit).toHaveBeenCalledTimes(1);
  const [bounds, opts] = fit.mock.calls[0] as [L.LatLngBounds, L.FitBoundsOptions];
  expect(bounds.equals(L.latLngBounds([-19.6, 130], [-12, 134.1]))).toBe(true);
  expect(opts).toEqual({ maxZoom: 12 });
});

test("AC5: pin popup shows the stop name and formatted arrivedAt", () => {
  render(<TripMap collection={fc(stop("s1", 134.1, -19.6, "Tennant Creek"))} />);
  const content = markers(liveMap())[0].getPopup()!.getContent() as HTMLElement;
  expect(content.querySelector("strong")!.textContent).toBe("Tennant Creek");
  expect(content.textContent).toContain(formatInstant(ARRIVED));
});

test("AC5: a stop name is rendered as text, never parsed as HTML", () => {
  const evil = '<img src=x onerror="alert(1)">';
  render(<TripMap collection={fc(stop("s1", 134.1, -19.6, evil))} />);
  const content = markers(liveMap())[0].getPopup()!.getContent() as HTMLElement;
  expect(content.querySelector("img")).toBeNull();
  expect(content.querySelector("strong")!.textContent).toBe(evil);
});

test("AC6: a new collection swaps the feature layer on the same map", () => {
  const { rerender } = render(<TripMap collection={fc(stop("a", 130, -12))} />);
  const map = liveMap();
  rerender(<TripMap collection={fc(stop("b", 134.1, -19.6), stop("c", 135, -20))} />);
  expect(maps).toHaveLength(1);
  const lngs = markers(map).map((m) => m.getLatLng().lng).sort();
  expect(lngs).toEqual([134.1, 135]); // old pin gone, both new pins present
  expect(layers(map, L.GeoJSON)).toHaveLength(1);
});

test("AC6: unmount calls map.remove()", () => {
  const remove = vi.spyOn(L.Map.prototype, "remove");
  const { unmount } = render(<TripMap collection={fc(stop("a", 130, -12))} />);
  unmount();
  expect(remove).toHaveBeenCalled();
});

test("AC6: StrictMode double-mount doesn't throw 'Map container is already initialized'", () => {
  expect(() =>
    render(
      <StrictMode>
        <TripMap collection={fc(stop("a", 134.1, -19.6))} />
      </StrictMode>,
    ),
  ).not.toThrow();
  expect(markers(liveMap())).toHaveLength(1);
});

test("AC8: default marker icon uses the bundled image, not Leaflet's guessed path", () => {
  render(<TripMap collection={fc(stop("a", 134.1, -19.6))} />);
  const img = markers(liveMap())[0].getIcon().createIcon() as HTMLImageElement;
  expect(img.getAttribute("src")).toBe(iconUrl);
});

test("AC7: map request failure shows 'Map unavailable' and the trip home still renders", async () => {
  const TRIP: TripOut = { id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", bikes: [], access: "viewer", visibility: "private", publicDelayHours: 24, riderCount: 1, lastPublicStopAt: null, viewer: { role: "none" } };
  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("scrollTo", () => {});
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) =>
    String(input).endsWith("/map")
      ? json(500, { error: { code: "INTERNAL_ERROR", message: "boom" } })
      : String(input).endsWith("/stops")
        ? json(200, [])
        : json(200, TRIP),
  );
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/t/abc"] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Map unavailable")).toBeTruthy();
  expect(screen.getByRole("heading", { name: TRIP.name })).toBeTruthy();
  expect(screen.getByRole("main")).toBeTruthy();
});
