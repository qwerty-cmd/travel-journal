import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import L from "leaflet";
import { routeTree } from "./routeTree.gen";
import { formatInstant } from "./format";
import type { PhotoOut } from "./api/gen/types/PhotoOut";
import type { StopOut } from "./api/gen/types/StopOut";
import type { TripOut } from "./api/gen/types/TripOut";

// The root route mounts <QueueNotice/>, which reads the offline queue's own
// IndexedDB on mount. That read is not the stop-detail path, so stub it out:
// the AC7 IndexedDB assertion below stays about presigned photo urls only.
vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));

// t-frontend-stop-detail-gallery: /t/$slug/stops/$stopId, plus pin and
// timeline navigation into it. Driven through the real route tree with fetch
// stubbed per path (build first so routeTree.gen.ts has the new route).

const maps: L.Map[] = [];
L.Map.addInitHook(function (this: L.Map) {
  maps.push(this);
});

const TRIP: TripOut = { id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", bikes: [], access: "viewer", visibility: "private", publicDelayHours: 24, riderCount: 1, lastPublicStopAt: null, viewer: { role: "none" } };
const GPS: StopOut = {
  id: "s-gps",
  name: "Tennant Creek",
  lat: -19.6,
  lng: 134.1,
  locationSource: "gps",
  arrivedAt: "2026-10-03T08:30:00Z",
  notes: "Headwind all day",
};
const MANUAL: StopOut = {
  id: "s-man",
  name: "Barrow Creek",
  lat: -21.5,
  lng: 133.9,
  locationSource: "manual",
  arrivedAt: "2026-10-02T17:00:00+09:30",
  notes: null,
};
const photo = (id: string): PhotoOut => ({
  id,
  stopId: GPS.id,
  url: `https://r2.example/p/${id}.jpg?X-Amz-Signature=sig${id}`,
  uploadedBy: "Wes",
  takenAt: "2026-10-03T09:00:00Z",
  archived: false,
});
const PHOTOS = [photo("p1"), photo("p2")];

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Routes = Record<string, () => Response | Promise<Response>>;
let routes: Routes;
const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const path = String(input);
  const handler = routes[path];
  if (!handler) throw new Error(`unstubbed fetch ${path}`);
  return handler();
});

function stub(overrides: Routes = {}) {
  routes = {
    "/api/trips/abc": () => json(200, TRIP),
    "/api/trips/abc/stops": () => json(200, [GPS, MANUAL]),
    "/api/trips/abc/map": () =>
      json(200, {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            id: GPS.id,
            geometry: { type: "Point", coordinates: [GPS.lng, GPS.lat] },
            properties: { name: GPS.name, arrivedAt: GPS.arrivedAt },
          },
          {
            type: "Feature",
            id: MANUAL.id,
            geometry: { type: "Point", coordinates: [MANUAL.lng, MANUAL.lat] },
            properties: { name: MANUAL.name, arrivedAt: MANUAL.arrivedAt },
          },
          {
            type: "Feature",
            geometry: { type: "LineString", coordinates: [[133.9, -21.5], [134.1, -19.6]] },
            properties: {},
          },
        ],
      }),
    [`/api/trips/abc/stops/${GPS.id}/photos`]: () => json(200, PHOTOS),
    [`/api/trips/abc/stops/${MANUAL.id}/photos`]: () => json(200, []),
    ...overrides,
  };
}

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

const photoRequests = () => fetchMock.mock.calls.map(([i]) => String(i)).filter((p) => p.endsWith("/photos"));
const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));

beforeEach(() => {
  maps.length = 0;
  localStorage.clear();
  sessionStorage.clear();
  fetchMock.mockClear();
  stub();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AC1/AC2: stop header", () => {
  test("gps stop: name, localised arrivedAt, notes, no approximate label", async () => {
    renderAt(`/t/abc/stops/${GPS.id}`);
    expect(await screen.findByRole("heading", { name: GPS.name })).toBeTruthy();
    expect(screen.getByText(formatInstant(GPS.arrivedAt))).toBeTruthy();
    expect(screen.getByText(GPS.notes!)).toBeTruthy();
    expect(screen.queryByText(/approximate location/)).toBeNull();
  });

  test("manual stop: approximate location shown, null notes render nothing", async () => {
    renderAt(`/t/abc/stops/${MANUAL.id}`);
    expect(await screen.findByRole("heading", { name: MANUAL.name })).toBeTruthy();
    expect(screen.getByText(/approximate location/).textContent).toContain(
      formatInstant(MANUAL.arrivedAt),
    );
    expect(screen.queryByText("null")).toBeNull();
    const main = screen.getByRole("main");
    // Back link, heading, timestamp line, "No photos yet" — no notes paragraph.
    await screen.findByText("No photos yet");
    expect(main.querySelectorAll("p")).toHaveLength(2);
  });

  test.each(["rider", "viewer"] as const)("%s slug renders the same read-only screen", async (access) => {
    localStorage.setItem("btj.displayName", "Wes"); // skip the rider name prompt
    stub({ "/api/trips/abc": () => json(200, { ...TRIP, access }) });
    renderAt(`/t/abc/stops/${GPS.id}`);
    expect(await screen.findByRole("heading", { name: GPS.name })).toBeTruthy();
    await screen.findAllByAltText("Stop photo");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(document.querySelector("input, textarea, form")).toBeNull();
  });
});

describe("AC3: thumbnail grid", () => {
  test("one lazy <img> per photo, src === url, fixed square, object-fit cover", async () => {
    renderAt(`/t/abc/stops/${GPS.id}`);
    const imgs = (await screen.findAllByAltText("Stop photo")) as HTMLImageElement[];
    expect(imgs).toHaveLength(PHOTOS.length);
    imgs.forEach((img, i) => {
      expect(img.getAttribute("src")).toBe(PHOTOS[i].url);
      expect(img.getAttribute("loading")).toBe("lazy");
      expect(img.className).toBe("legacy__thumb-img"); // square + object-fit cover live in t.$slug.css
    });
    expect(photoRequests()).toEqual([`/api/trips/abc/stops/${GPS.id}/photos`]);
  });

  test("empty photo list shows 'No photos yet' and no images", async () => {
    renderAt(`/t/abc/stops/${MANUAL.id}`);
    expect(await screen.findByText("No photos yet")).toBeTruthy();
    expect(screen.queryAllByRole("img")).toHaveLength(0);
  });

  test("photos error shows the envelope message, stop details still render", async () => {
    stub({
      [`/api/trips/abc/stops/${GPS.id}/photos`]: () =>
        json(500, { error: { code: "INTERNAL_ERROR", message: "storage down" } }),
    });
    renderAt(`/t/abc/stops/${GPS.id}`);
    expect(await screen.findByText("storage down")).toBeTruthy();
    expect(screen.getByRole("heading", { name: GPS.name })).toBeTruthy();
  });
});

describe("AC4: enlarge overlay", () => {
  async function openSecond() {
    renderAt(`/t/abc/stops/${GPS.id}`);
    const imgs = await screen.findAllByAltText("Stop photo");
    fireEvent.click(imgs[1]);
    return screen.getByRole("dialog");
  }

  test("tapping a thumbnail opens a role=dialog div with that full image", async () => {
    const dialog = await openSecond();
    expect(dialog.tagName).toBe("DIV"); // plain React state, not <dialog>.showModal
    expect(dialog.querySelector("img")!.getAttribute("src")).toBe(PHOTOS[1].url);
  });

  test("tapping the overlay closes it", async () => {
    const dialog = await openSecond();
    fireEvent.click(dialog.querySelector("img")!); // tap on the full image bubbles to the overlay
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("Escape closes it", async () => {
    await openSecond();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("other keys leave it open", async () => {
    await openSecond();
    fireEvent.keyDown(document, { key: "Enter" });
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});

// Presigned URLs live 1h: a load error refetches the photo list once for fresh
// URLs, and never loops when the fresh URL fails too.
describe("expired photo URLs", () => {
  // Each photos response re-signs every URL with that request's number.
  const fresh = (p: PhotoOut, n: number) => ({ ...p, url: `${p.url}&n=${n}` });
  beforeEach(() => {
    let n = 0;
    stub({ [`/api/trips/abc/stops/${GPS.id}/photos`]: () => (n++, json(200, PHOTOS.map((p) => fresh(p, n)))) });
  });
  const thumbs = () => screen.getAllByAltText("Stop photo") as HTMLImageElement[];

  test("a thumbnail error refetches once and renders the fresh URLs", async () => {
    renderAt(`/t/abc/stops/${GPS.id}`);
    await screen.findAllByAltText("Stop photo");
    expect(thumbs()[0].getAttribute("src")).toBe(fresh(PHOTOS[0], 1).url);
    fireEvent.error(thumbs()[0]);
    await waitFor(() => expect(thumbs()[0].getAttribute("src")).toBe(fresh(PHOTOS[0], 2).url));
    expect(thumbs()[1].getAttribute("src")).toBe(fresh(PHOTOS[1], 2).url);
    expect(photoRequests()).toHaveLength(2);
  });

  test("several thumbnails failing together cause a single refetch", async () => {
    renderAt(`/t/abc/stops/${GPS.id}`);
    await screen.findAllByAltText("Stop photo");
    thumbs().forEach((img) => fireEvent.error(img));
    await waitFor(() => expect(thumbs()[0].getAttribute("src")).toBe(fresh(PHOTOS[0], 2).url));
    await settle();
    expect(photoRequests()).toHaveLength(2);
  });

  test("a fresh URL that fails too is not retried: no loop", async () => {
    renderAt(`/t/abc/stops/${GPS.id}`);
    await screen.findAllByAltText("Stop photo");
    fireEvent.error(thumbs()[0]);
    await waitFor(() => expect(thumbs()[0].getAttribute("src")).toBe(fresh(PHOTOS[0], 2).url));
    fireEvent.error(thumbs()[0]);
    fireEvent.error(thumbs()[1]);
    await settle();
    expect(photoRequests()).toHaveLength(2);
    expect(thumbs()[0].getAttribute("src")).toBe(fresh(PHOTOS[0], 2).url);
  });

  test("an enlarged photo that fails refetches and shows the same photo's fresh URL", async () => {
    renderAt(`/t/abc/stops/${GPS.id}`);
    fireEvent.click((await screen.findAllByAltText("Stop photo"))[1]);
    const big = () => screen.getByRole("dialog").querySelector("img")!;
    expect(big().getAttribute("src")).toBe(fresh(PHOTOS[1], 1).url);
    fireEvent.error(big());
    await waitFor(() => expect(big().getAttribute("src")).toBe(fresh(PHOTOS[1], 2).url));
    expect(photoRequests()).toHaveLength(2);
  });
});

describe("AC5: unknown stopId", () => {
  test("shows 'Stop not found' with a link back to the trip, no throw, no photos request", async () => {
    const router = renderAt("/t/abc/stops/nope");
    expect(await screen.findByText("Stop not found")).toBeTruthy();
    const back = screen.getByRole("link", { name: "Back to trip" });
    expect(back.getAttribute("href")).toBe("/t/abc");
    await settle();
    expect(photoRequests()).toEqual([]);

    fireEvent.click(back);
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
  });

  test("stops request failure shows an error, not 'Stop not found'", async () => {
    stub({ "/api/trips/abc/stops": () => json(500, { error: { code: "INTERNAL_ERROR", message: "db down" } }) });
    renderAt(`/t/abc/stops/${GPS.id}`);
    expect(await screen.findByText("db down")).toBeTruthy();
    expect(screen.queryByText("Stop not found")).toBeNull();
    expect(photoRequests()).toEqual([]);
  });
});

describe("AC6: navigation into the stop", () => {
  test.each([
    ["click", (el: HTMLElement) => fireEvent.click(el)],
    ["Enter", (el: HTMLElement) => fireEvent.keyDown(el, { key: "Enter" })],
  ])("timeline item %s opens /t/$slug/stops/<id>", async (_, activate) => {
    const router = renderAt("/t/abc");
    const item = await screen.findByRole("link", { name: new RegExp(MANUAL.name) });
    activate(item);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/t/abc/stops/${MANUAL.id}`));
    expect(await screen.findByRole("heading", { name: MANUAL.name })).toBeTruthy();
  });

  test("pin click navigates by GeoJSON Feature.id; the trail does not navigate", async () => {
    const router = renderAt("/t/abc");
    await screen.findByRole("heading", { name: TRIP.name });
    const map = () => maps[maps.length - 1];
    let pins: L.Marker[] = [];
    await waitFor(() => {
      pins = [];
      map().eachLayer((l) => l instanceof L.Marker && pins.push(l));
      expect(pins).toHaveLength(2);
    });

    map().eachLayer((l) => {
      if (l instanceof L.Polyline) l.fire("click");
    });
    await settle();
    expect(router.state.location.pathname).toBe("/t/abc");

    const gpsPin = pins.find((p) => p.getLatLng().lat === GPS.lat)!;
    gpsPin.fire("click");
    await waitFor(() => expect(router.state.location.pathname).toBe(`/t/abc/stops/${GPS.id}`));
    expect(await screen.findByRole("heading", { name: GPS.name })).toBeTruthy();
  });
});

test("AC7: presigned photo urls are never written to localStorage, sessionStorage or IndexedDB", async () => {
  const idbOpen = vi.fn();
  vi.stubGlobal("indexedDB", { open: idbOpen, deleteDatabase: vi.fn() });
  renderAt(`/t/abc/stops/${GPS.id}`);
  fireEvent.click((await screen.findAllByAltText("Stop photo"))[0]);
  await settle();
  const stored = [localStorage, sessionStorage].flatMap((s) =>
    Array.from({ length: s.length }, (_, i) => s.getItem(s.key(i)!) ?? ""),
  );
  for (const p of PHOTOS) {
    expect(stored.some((v) => v.includes(p.url) || v.includes("X-Amz-Signature"))).toBe(false);
  }
  expect(idbOpen).not.toHaveBeenCalled();
});
