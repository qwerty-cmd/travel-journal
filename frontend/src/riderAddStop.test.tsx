import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import L from "leaflet";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";
import type { V2PhotoItem, V2StopItem } from "./offline/queue";

// t-am-fe-rider-add-stop: the rider home's Add stop bar and /trips/$tripId/add.
// enqueue is spied (the form's only write path); processPhoto is stubbed.

vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));
const enqueueMock = vi.hoisted(() => vi.fn<(items: unknown, userId?: string) => Promise<void>>());
vi.mock("./offline/queue", async (orig) => ({ ...(await orig<typeof import("./offline/queue")>()), enqueue: enqueueMock }));
const processPhotoMock = vi.hoisted(() => vi.fn<(file: File) => Promise<{ blob: Blob; takenAt: string }>>());
vi.mock("./photo", async (orig) => ({ ...(await orig<typeof import("./photo")>()), processPhoto: processPhotoMock }));

const maps: L.Map[] = [];
L.Map.addInitHook(function (this: L.Map) {
  maps.push(this);
});

const ID = "11111111-1111-4111-8111-111111111111";
const USER = "22222222-2222-4222-8222-222222222222";
const trip = (role: string | undefined, over: Partial<TripOut> = {}): TripOut => {
  const t = {
    id: ID,
    name: "Stuart Hwy 2026",
    startDate: "2026-10-12",
    bikes: [],
    access: "rider",
    visibility: "private",
    publicDelayHours: 24,
    riderCount: 2,
    lastPublicStopAt: null,
    ...(role === undefined ? {} : { viewer: { role } }),
    ...over,
  };
  return t as TripOut;
};

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let tripBody: TripOut;
let meStatus: number;
const geo = { getCurrentPosition: vi.fn() };

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
const enqueued = () => enqueueMock.mock.calls;

beforeEach(() => {
  maps.length = 0;
  localStorage.clear();
  tripBody = trip("rider");
  meStatus = 200;
  enqueueMock.mockReset();
  enqueueMock.mockResolvedValue(undefined);
  processPhotoMock.mockReset();
  processPhotoMock.mockImplementation(async (f: File) => ({ blob: new Blob([f.name], { type: "image/jpeg" }), takenAt: "2026-10-13T08:15:00+09:30" }));
  geo.getCurrentPosition.mockReset();
  Object.defineProperty(navigator, "geolocation", { value: geo, configurable: true });
  vi.stubGlobal("scrollTo", () => {});
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const p = new URL(String(input), "https://testserver").pathname;
    if (p === "/api/v2/auth/me")
      return meStatus === 200 ? json(200, { id: USER, displayName: "Wes", username: "wes" }) : json(401, { error: { code: "UNAUTHENTICATED", message: "x" } });
    if (p === `/api/v2/trips/${ID}`) return json(200, tripBody);
    if (p.endsWith("/map")) return json(200, { type: "FeatureCollection", features: [] });
    if (p.endsWith("/stops")) return json(200, []);
    return json(404, { error: { code: "NOT_FOUND", message: "x" } });
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("rider home", () => {
  test.each(["rider", "leader"])("%s sees the Add stop bar linking to /add", async (role) => {
    tripBody = trip(role);
    renderAt(`/trips/${ID}`);
    const link = await screen.findByRole("link", { name: "Add stop" });
    expect(link.getAttribute("href")).toBe(`/trips/${ID}/add`);
  });

  test.each([["anonymous"], ["none"], ["pending"], [undefined]])("role %s gets no Add stop", async (role) => {
    tripBody = trip(role, { access: "rider" }); // legacy access never grants write UI
    renderAt(`/trips/${ID}`);
    await screen.findByRole("heading", { name: "Stops" });
    expect(screen.queryByRole("link", { name: "Add stop" })).toBeNull();
  });
});

describe("/trips/$tripId/add gate", () => {
  test.each([["anonymous"], ["none"], ["pending"], [undefined]])("role %s: notice, no form, no geolocation", async (role) => {
    tripBody = trip(role);
    renderAt(`/trips/${ID}/add`);
    await screen.findByText("Only riders on this trip can add stops.");
    await settle();
    expect(screen.queryByLabelText("Name")).toBeNull();
    expect(geo.getCurrentPosition).not.toHaveBeenCalled();
  });

  test("a member signed out (401, nothing cached) is asked to sign in, no geolocation", async () => {
    meStatus = 401;
    renderAt(`/trips/${ID}/add`);
    await screen.findByText("Sign in to add a stop.");
    expect(geo.getCurrentPosition).not.toHaveBeenCalled();
  });
});

describe("/trips/$tripId/add form", () => {
  test("GPS fix + name + photo: one enqueue of v2 payloads with userId, then the trip", async () => {
    geo.getCurrentPosition.mockImplementation((ok: PositionCallback) =>
      ok({ coords: { latitude: -19.6, longitude: 134.1, accuracy: 5 }, timestamp: 0 } as GeolocationPosition),
    );
    const router = renderAt(`/trips/${ID}/add`);
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: " Tennant Creek " } });
    fireEvent.change(screen.getByLabelText("Photos"), { target: { files: [new File(["a"], "a.jpg")] } });
    await screen.findByRole("button", { name: "Remove photo 1" });
    fireEvent.click(screen.getByRole("button", { name: "Save stop" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/trips/${ID}`));
    expect(geo.getCurrentPosition.mock.calls[0][2]).toMatchObject({ enableHighAccuracy: true });
    expect(enqueued()).toHaveLength(1);
    const [items, userId] = enqueued()[0] as [[V2StopItem, V2PhotoItem], string];
    expect(userId).toBe(USER);
    expect(items[0].payload).toEqual({
      tripId: ID,
      data: expect.objectContaining({ name: "Tennant Creek", lat: -19.6, lng: 134.1, locationSource: "gps", notes: null }),
    });
    expect(items[1].payload).toEqual({ tripId: ID, stopId: items[0].payload.data.id, stopName: "Tennant Creek", data: { id: expect.any(String), takenAt: "2026-10-13T08:15:00+09:30" } });
    expect("slug" in items[0].payload).toBe(false);
  });

  test("GPS denied: keyboard 'Use map centre' sets the manual location at the map centre", async () => {
    geo.getCurrentPosition.mockImplementation((_ok: PositionCallback, err: PositionErrorCallback) =>
      err({ code: 1 } as GeolocationPositionError),
    );
    renderAt(`/trips/${ID}/add`);
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Daly Waters" } });
    const save = screen.getByRole("button", { name: "Save stop" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    screen.getByText("Add a name and a location to save.");
    act(() => {
      maps[maps.length - 1].setView([-16.25, 133.37], 10, { animate: false });
    });
    fireEvent.click(screen.getByRole("button", { name: "Use map centre" }));
    await screen.findByText("Location set on the map");
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(enqueued()).toHaveLength(1));
    const [item] = enqueued()[0] as [V2StopItem, string];
    expect(item.payload.data).toMatchObject({ locationSource: "manual" });
    expect(item.payload.data.lat).toBeCloseTo(-16.25, 3);
    expect(item.payload.data.lng).toBeCloseTo(133.37, 3);
  });

  test("a late GPS fix never replaces a centre-set point", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
    let late!: PositionCallback;
    geo.getCurrentPosition.mockImplementation((ok: PositionCallback) => (late = ok));
    renderAt(`/trips/${ID}/add`);
    await screen.findByText("Getting your location…");
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    fireEvent.click(await screen.findByRole("button", { name: "Use map centre" }));
    await screen.findByText("Location set on the map");
    act(() => late({ coords: { latitude: 1, longitude: 2, accuracy: 5 }, timestamp: 0 } as GeolocationPosition));
    expect(screen.queryByText("Location found (GPS)")).toBeNull();
  });

  test("public trip shows the delay note", async () => {
    tripBody = trip("leader", { visibility: "public", publicDelayHours: 6 });
    renderAt(`/trips/${ID}/add`);
    await screen.findByText("This stop is visible to the public 6 hours after it's saved.");
  });
});
