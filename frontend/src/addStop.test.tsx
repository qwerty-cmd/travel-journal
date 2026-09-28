import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import L from "leaflet";
import { routeTree } from "./routeTree.gen";
import type { QueueItem, QueueRecord } from "./offline/queue";
import type { TripOut } from "./api/gen/types/TripOut";

// t-frontend-add-stop-form: /t/$slug/add. Written from the AC and the
// StopCreate contract, not from the route. enqueue is spied (the UI's only
// write path); one offline test runs the real queue into fake IndexedDB.

vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));
const enqueueMock = vi.hoisted(() => vi.fn<(item: QueueItem) => Promise<void>>());
vi.mock("./offline/queue", async (orig) => ({
  ...(await orig<typeof import("./offline/queue")>()),
  enqueue: enqueueMock,
}));

const maps: L.Map[] = [];
L.Map.addInitHook(function (this: L.Map) {
  maps.push(this);
});

const TRIP: TripOut = { id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", bikes: [], access: "rider" };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

let tripBody: TripOut;
let offline: boolean;
const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
  if (offline) throw new TypeError("Failed to fetch");
  const path = String(input);
  if (path === "/api/trips/abc") return json(200, tripBody);
  if (path === "/api/trips/abc/stops") return json(200, []);
  if (path === "/api/trips/abc/map") return json(200, { type: "FeatureCollection", features: [] });
  throw new Error(`unstubbed fetch ${path}`);
});
const stopPosts = () =>
  fetchMock.mock.calls.filter(([i, init]) => String(i).endsWith("/stops") && (init?.method ?? "GET") !== "GET");

// ---- geolocation ----
type Geo = { getCurrentPosition: ReturnType<typeof vi.fn> };
function setGeo(geo: Geo | undefined) {
  Object.defineProperty(navigator, "geolocation", { value: geo, configurable: true });
}
const geoOk = (lat: number, lng: number): Geo => ({
  getCurrentPosition: vi.fn((ok: PositionCallback) =>
    ok({ coords: { latitude: lat, longitude: lng, accuracy: 5 }, timestamp: Date.now() } as GeolocationPosition),
  ),
});
const geoErr = (code: number): Geo => ({
  getCurrentPosition: vi.fn((_ok: PositionCallback, err?: PositionErrorCallback | null) =>
    err?.({ code, message: "nope", PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3 } as GeolocationPositionError),
  ),
});
const geoPending = (): Geo => ({ getCurrentPosition: vi.fn() });

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));
const nameInput = () => screen.findByLabelText("Name");
const saveBtn = () => screen.getByRole("button", { name: "Save stop" }) as HTMLButtonElement;
const tapMap = (lat: number, lng: number) =>
  act(() => {
    maps[maps.length - 1].fire("click", { latlng: L.latLng(lat, lng) });
  });
const enqueued = () => enqueueMock.mock.calls.map(([item]) => item);
const TZ_ISO = /(Z|[+-]\d\d:\d\d)$/;

beforeEach(() => {
  maps.length = 0;
  localStorage.clear();
  localStorage.setItem("btj.displayName", "Wes"); // skip the rider name prompt
  tripBody = TRIP;
  offline = false;
  fetchMock.mockClear();
  enqueueMock.mockReset();
  enqueueMock.mockResolvedValue(undefined);
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("scrollTo", () => {});
  setGeo(geoOk(-19.6, 134.1));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setGeo(undefined);
});

describe("AC1: id and arrivedAt fixed at mount", () => {
  test("arrivedAt is the mount time, timezone-aware; id is a UUID", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-03T08:30:00Z"));
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    vi.setSystemTime(new Date("2026-10-03T09:45:00Z")); // time passes before submit
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    const { data } = enqueued()[0].payload;
    expect(data.arrivedAt).toMatch(TZ_ISO);
    expect(new Date(data.arrivedAt).getTime()).toBe(Date.parse("2026-10-03T08:30:00Z"));
    expect(data.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  test("resubmit after a failed save reuses the same id and arrivedAt", async () => {
    enqueueMock.mockRejectedValueOnce(new Error("quota"));
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    fireEvent.click(saveBtn());
    expect(await screen.findByRole("alert")).toBeTruthy();
    await waitFor(() => expect(saveBtn().disabled).toBe(false));
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(2));
    const [a, b] = enqueued().map((i) => i.payload.data);
    expect(b.id).toBe(a.id);
    expect(b.arrivedAt).toBe(a.arrivedAt);
  });

  test("a fresh mount gets a fresh id", async () => {
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "A" } });
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    cleanup();
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "B" } });
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(2));
    const [a, b] = enqueued().map((i) => i.payload.data.id);
    expect(b).not.toBe(a);
  });
});

test("AC2: GPS success fills lat/lng with locationSource gps; high accuracy + timeout requested", async () => {
  const geo = geoOk(-19.6, 134.1);
  setGeo(geo);
  renderAt("/t/abc/add");
  fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
  fireEvent.click(saveBtn());
  await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
  const opts = geo.getCurrentPosition.mock.calls[0][2] as PositionOptions;
  expect(opts.enableHighAccuracy).toBe(true);
  expect(opts.timeout).toBeGreaterThan(0);
  expect(enqueued()[0].payload.data).toMatchObject({ lat: -19.6, lng: 134.1, locationSource: "gps" });
});

describe("AC3: geolocation unavailable -> manual map tap", () => {
  test.each([
    ["permission denied", () => setGeo(geoErr(1))],
    ["position unavailable", () => setGeo(geoErr(2))],
    ["timeout", () => setGeo(geoErr(3))],
    ["no navigator.geolocation", () => setGeo(undefined)],
  ])("%s: map shown, tap sets lat/lng and manual", async (_, arrange) => {
    arrange();
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Barrow Creek" } });
    await waitFor(() => expect(maps.length).toBeGreaterThan(0));
    expect(saveBtn().disabled).toBe(true); // no position yet
    await tapMap(-21.5, 133.5);
    await waitFor(() => expect(saveBtn().disabled).toBe(false));
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    expect(enqueued()[0].payload.data).toMatchObject({ lat: -21.5, lng: 133.5, locationSource: "manual" });
  });

  test("tap on a wrapped world copy stores lng inside -180..180", async () => {
    setGeo(geoErr(1));
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "X" } });
    await waitFor(() => expect(maps.length).toBeGreaterThan(0));
    await tapMap(-21.5, 133.5 + 360);
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    expect(enqueued()[0].payload.data.lng).toBeCloseTo(133.5);
  });
});

describe("AC4: submit gating and the enqueued entry", () => {
  test("disabled with a blank or whitespace name even with a GPS fix", async () => {
    renderAt("/t/abc/add");
    await nameInput();
    expect(saveBtn().disabled).toBe(true);
    fireEvent.change(await nameInput(), { target: { value: "   " } });
    expect(saveBtn().disabled).toBe(true);
    fireEvent.click(saveBtn());
    await settle();
    expect(enqueueMock).not.toHaveBeenCalled();
  });

  test("disabled with a name while no position is set (GPS pending)", async () => {
    setGeo(geoPending());
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    expect(saveBtn().disabled).toBe(true);
    fireEvent.click(saveBtn());
    await settle();
    expect(enqueueMock).not.toHaveBeenCalled();
  });

  test("exactly one enqueue with the six StopCreate fields, blank notes -> null, then /t/$slug", async () => {
    const router = renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "   " } });
    fireEvent.click(saveBtn());
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
    expect(enqueueMock).toHaveBeenCalledTimes(1);
    const item = enqueued()[0];
    expect(item.kind).toBe("stop");
    expect(item.payload.slug).toBe("abc");
    expect(Object.keys(item.payload).sort()).toEqual(["data", "slug"]);
    const { data } = item.payload;
    expect(Object.keys(data).sort()).toEqual(["arrivedAt", "id", "lat", "lng", "locationSource", "name", "notes"].sort());
    expect(data).toMatchObject({ name: "Tennant Creek", lat: -19.6, lng: 134.1, locationSource: "gps", notes: null });
    expect(stopPosts()).toEqual([]); // the UI never POSTs; the queue does
  });

  test("non-blank notes pass through", async () => {
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Headwind all day" } });
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    expect(enqueued()[0].payload.data.notes).toBe("Headwind all day");
  });

  test("double tap while saving enqueues once", async () => {
    let release!: () => void;
    enqueueMock.mockImplementationOnce(() => new Promise<void>((r) => (release = r)));
    const router = renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    fireEvent.click(saveBtn());
    fireEvent.click(saveBtn());
    await settle();
    expect(enqueueMock).toHaveBeenCalledTimes(1);
    expect(router.state.location.pathname).toBe("/t/abc/add"); // not before the write commits
    await act(async () => release());
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
    expect(enqueueMock).toHaveBeenCalledTimes(1);
  });

  test("enqueue failure: alert shown, stays on the form, nothing lost silently", async () => {
    enqueueMock.mockRejectedValueOnce(new Error("quota"));
    const router = renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    fireEvent.click(saveBtn());
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(router.state.location.pathname).toBe("/t/abc/add");
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Tennant Creek");
  });
});

describe("AC5: access control", () => {
  test("rider trip home has an Add stop link to /t/$slug/add", async () => {
    renderAt("/t/abc");
    const link = await screen.findByRole("link", { name: "Add stop" });
    expect(link.getAttribute("href")).toBe("/t/abc/add");
  });

  test("viewer trip home has no Add stop link", async () => {
    tripBody = { ...TRIP, access: "viewer" };
    renderAt("/t/abc");
    await screen.findByRole("main");
    await settle();
    expect(screen.queryByRole("link", { name: "Add stop" })).toBeNull();
  });

  test("viewer opening /t/$slug/add directly is redirected to /t/$slug, no form, no enqueue", async () => {
    tripBody = { ...TRIP, access: "viewer" };
    const router = renderAt("/t/abc/add");
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
    await settle();
    expect(screen.queryByLabelText("Name")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save stop" })).toBeNull();
    expect(enqueueMock).not.toHaveBeenCalled();
  });

  test("server access wins over a cached rider trip", async () => {
    localStorage.setItem("btj.trip.abc", JSON.stringify(TRIP));
    tripBody = { ...TRIP, access: "viewer" };
    const router = renderAt("/t/abc/add");
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
    await settle();
    expect(screen.queryByRole("button", { name: "Save stop" })).toBeNull();
  });
});

describe("AC6: offline", () => {
  beforeEach(() => {
    offline = true;
    localStorage.setItem("btj.trip.abc", JSON.stringify(TRIP));
    setGeo(geoErr(2));
  });

  test("rider form renders from the persisted trip; tapped submit enqueues", async () => {
    const router = renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Barrow Creek" } });
    await waitFor(() => expect(maps.length).toBeGreaterThan(0));
    await tapMap(-21.5, 133.5);
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    expect(enqueued()[0]).toMatchObject({
      kind: "stop",
      payload: { slug: "abc", data: { name: "Barrow Creek", lat: -21.5, lng: 133.5, locationSource: "manual" } },
    });
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
  });

  test("real queue: the stop is persisted in IndexedDB while the network is down", async () => {
    const real = await vi.importActual<typeof import("./offline/queue")>("./offline/queue");
    enqueueMock.mockImplementation(real.enqueue);
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Barrow Creek" } });
    await waitFor(() => expect(maps.length).toBeGreaterThan(0));
    await tapMap(-21.5, 133.5);
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    const sent = enqueued()[0].payload.data;
    let stored: QueueRecord[] = [];
    const unsub = real.subscribe((e) => (stored = e));
    await waitFor(() => expect(stored.some((e) => e.payload.data.id === sent.id)).toBe(true));
    unsub();
    expect(stored.find((e) => e.payload.data.id === sent.id)!.payload.data).toEqual(sent);
  });
});
