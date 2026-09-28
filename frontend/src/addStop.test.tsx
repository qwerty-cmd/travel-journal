import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import L from "leaflet";
import { routeTree } from "./routeTree.gen";
import type { PhotoItem, QueueItem, QueueRecord } from "./offline/queue";
import type { TripOut } from "./api/gen/types/TripOut";
import { PhotoDecodeError } from "./photo"; // the real class, re-exported by the mock below

// t-frontend-add-stop-form: /t/$slug/add. Written from the AC and the
// StopCreate contract, not from the route. enqueue is spied (the UI's only
// write path); one offline test runs the real queue into fake IndexedDB.

vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));
const enqueueMock = vi.hoisted(() =>
  vi.fn<(items: QueueItem | PhotoItem | (QueueItem | PhotoItem)[]) => Promise<void>>(),
);
vi.mock("./offline/queue", async (orig) => ({
  ...(await orig<typeof import("./offline/queue")>()),
  enqueue: enqueueMock,
}));
// t-add-stop-photo-attach: processPhoto is stubbed; PhotoDecodeError stays real.
const processPhotoMock = vi.hoisted(() => vi.fn<(file: File) => Promise<{ blob: Blob; takenAt: string }>>());
vi.mock("./photo", async (orig) => ({
  ...(await orig<typeof import("./photo")>()),
  processPhoto: processPhotoMock,
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
const enqueued = () => enqueueMock.mock.calls.map(([item]) => item as QueueItem);
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
  processPhotoMock.mockReset();
  processPhotoMock.mockImplementation(async (f: File) => ({
    blob: new Blob([`jpeg:${f.name}`], { type: "image/jpeg" }),
    takenAt: "2026-10-03T08:15:00+09:30",
  }));
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

// ---- t-add-stop-photo-attach ----
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const photoInput = () => screen.getByLabelText("Photos") as HTMLInputElement;
const jpg = (name: string) => new File([name], name, { type: "image/jpeg" });
const pick = async (...files: File[]) => {
  await nameInput(); // form rendered
  const input = photoInput();
  Object.defineProperty(input, "files", { value: files, configurable: true });
  fireEvent.change(input);
};
const removeBtns = () => screen.queryAllByRole("button", { name: "Remove" });
const batch = (i: number) => enqueueMock.mock.calls[i][0] as (QueueItem | PhotoItem)[];
const photoPosts = () =>
  fetchMock.mock.calls.filter(([i, init]) => String(i).includes("/photos") && (init?.method ?? "GET") !== "GET");

describe("photo attach", () => {
  test("input is a multi-select image picker with no capture attribute", async () => {
    renderAt("/t/abc/add");
    await nameInput();
    const input = photoInput();
    expect(input.type).toBe("file");
    expect(input.accept).toBe("image/*");
    expect(input.multiple).toBe(true);
    expect(input.hasAttribute("capture")).toBe(false);
  });

  test("AC1: each picked file goes through processPhoto and is listed with its name and a Remove button", async () => {
    renderAt("/t/abc/add");
    await pick(jpg("a.jpg"), jpg("b.jpg"));
    await waitFor(() => expect(removeBtns()).toHaveLength(2));
    expect(processPhotoMock.mock.calls.map(([f]) => f.name)).toEqual(["a.jpg", "b.jpg"]);
    expect(screen.getByText(/a\.jpg/)).toBeTruthy();
    expect(screen.getByText(/b\.jpg/)).toBeTruthy();
  });

  test("AC1: PhotoDecodeError names the file and adds nothing; the other file in the pick still lands", async () => {
    processPhotoMock.mockImplementation(async (f: File) => {
      if (f.name === "bad.heic") throw new PhotoDecodeError("cannot decode");
      return { blob: new Blob(["x"], { type: "image/jpeg" }), takenAt: "2026-10-03T08:15:00+09:30" };
    });
    renderAt("/t/abc/add");
    await pick(jpg("bad.heic"), jpg("good.jpg"));
    expect((await screen.findByRole("alert")).textContent).toContain("bad.heic");
    await waitFor(() => expect(removeBtns()).toHaveLength(1));
    expect(screen.getByText(/good\.jpg/)).toBeTruthy();
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    expect(batch(0)).toHaveLength(2); // stop + good.jpg only
  });

  test("AC2: submit disabled while any file is still processing", async () => {
    let release!: () => void;
    processPhotoMock.mockImplementationOnce(
      (f: File) =>
        new Promise((r) => (release = () => r({ blob: new Blob([f.name]), takenAt: "2026-10-03T08:15:00+09:30" }))),
    );
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    await waitFor(() => expect(saveBtn().disabled).toBe(false));
    await pick(jpg("slow.jpg"));
    await waitFor(() => expect(saveBtn().disabled).toBe(true));
    fireEvent.click(saveBtn());
    await settle();
    expect(enqueueMock).not.toHaveBeenCalled();
    await act(async () => release());
    await waitFor(() => expect(saveBtn().disabled).toBe(false));
  });

  test("AC3: one enqueue call, stop first, photo entries carry the contract shape and the processed blob", async () => {
    const blobs: Record<string, Blob> = {};
    processPhotoMock.mockImplementation(async (f: File) => {
      blobs[f.name] = new Blob([`jpeg:${f.name}`], { type: "image/jpeg" });
      return { blob: blobs[f.name], takenAt: `2026-10-03T08:1${f.name === "a.jpg" ? 1 : 2}:00+09:30` };
    });
    const router = renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "  Tennant Creek  " } });
    await pick(jpg("a.jpg"), jpg("b.jpg"));
    await waitFor(() => expect(removeBtns()).toHaveLength(2));
    fireEvent.click(saveBtn());
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
    expect(enqueueMock).toHaveBeenCalledTimes(1);
    const [stop, ...photos] = batch(0) as [QueueItem, ...PhotoItem[]];
    expect(stop.kind).toBe("stop");
    expect(photos).toHaveLength(2);
    photos.forEach((p, i) => {
      expect(p.kind).toBe("photo");
      expect(Object.keys(p).sort()).toEqual(["file", "kind", "payload"]);
      expect(Object.keys(p.payload).sort()).toEqual(["data", "slug", "stopId", "stopName"]);
      expect(Object.keys(p.payload.data).sort()).toEqual(["id", "takenAt", "uploadedBy"]);
      expect(p.payload).toMatchObject({ slug: "abc", stopId: stop.payload.data.id, stopName: "Tennant Creek" });
      expect(p.payload.data.uploadedBy).toBe("Wes");
      expect(p.payload.data.takenAt).toBe(`2026-10-03T08:1${i + 1}:00+09:30`);
      expect(p.payload.data.id).toMatch(UUID);
      expect(p.file).toBe(blobs[["a.jpg", "b.jpg"][i]]);
    });
    expect(photos[0].payload.data.id).not.toBe(photos[1].payload.data.id);
    expect(photos[0].payload.data.id).not.toBe(stop.payload.data.id);
    expect(photoPosts()).toEqual([]); // the UI never uploads; the queue does
    expect(stopPosts()).toEqual([]);
  });

  test("AC4: a removed photo is not enqueued", async () => {
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    await pick(jpg("keep.jpg"), jpg("drop.jpg"));
    await waitFor(() => expect(removeBtns()).toHaveLength(2));
    fireEvent.click(screen.getByText(/drop\.jpg/).closest("li")!.querySelector("button")!);
    await waitFor(() => expect(removeBtns()).toHaveLength(1));
    expect(screen.queryByText(/drop\.jpg/)).toBeNull();
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    const items = batch(0);
    expect(items).toHaveLength(2);
    expect((items[1] as PhotoItem).file).toBe(await processPhotoMock.mock.results[0].value.then((r: { blob: Blob }) => r.blob));
  });

  test("AC4: removing the only photo leaves a stop-only enqueue", async () => {
    renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    await pick(jpg("only.jpg"));
    await waitFor(() => expect(removeBtns()).toHaveLength(1));
    fireEvent.click(removeBtns()[0]);
    await waitFor(() => expect(removeBtns()).toHaveLength(0));
    fireEvent.click(saveBtn());
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    const items = [enqueueMock.mock.calls[0][0]].flat();
    expect(items.map((x) => x.kind)).toEqual(["stop"]);
  });

  test("AC4/AC5: enqueue rejects with QuotaExceededError -> alert, stays, photos kept; resubmit replays identical ids", async () => {
    enqueueMock.mockRejectedValueOnce(new DOMException("full", "QuotaExceededError"));
    const router = renderAt("/t/abc/add");
    fireEvent.change(await nameInput(), { target: { value: "Tennant Creek" } });
    await pick(jpg("a.jpg"), jpg("b.jpg"));
    await waitFor(() => expect(removeBtns()).toHaveLength(2));
    fireEvent.click(saveBtn());
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(router.state.location.pathname).toBe("/t/abc/add");
    expect(removeBtns()).toHaveLength(2);
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Tennant Creek");
    await waitFor(() => expect(saveBtn().disabled).toBe(false));
    fireEvent.click(saveBtn());
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
    expect(enqueueMock).toHaveBeenCalledTimes(2);
    const ids = (i: number) => batch(i).map((x) => x.payload.data.id);
    expect(ids(0)).toHaveLength(3);
    expect(ids(1)).toEqual(ids(0));
    expect(processPhotoMock).toHaveBeenCalledTimes(2); // not re-processed on resubmit
  });

  describe("AC6: offline", () => {
    beforeEach(() => {
      offline = true;
      localStorage.setItem("btj.trip.abc", JSON.stringify(TRIP));
      setGeo(geoErr(2));
    });

    test("pick + submit still enqueues the stop and its photo in one call", async () => {
      const router = renderAt("/t/abc/add");
      fireEvent.change(await nameInput(), { target: { value: "Barrow Creek" } });
      await waitFor(() => expect(maps.length).toBeGreaterThan(0));
      await tapMap(-21.5, 133.5);
      await pick(jpg("a.jpg"));
      await waitFor(() => expect(removeBtns()).toHaveLength(1));
      fireEvent.click(saveBtn());
      await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
      expect(enqueueMock).toHaveBeenCalledTimes(1);
      const [stop, photo] = batch(0) as [QueueItem, PhotoItem];
      expect(stop.kind).toBe("stop");
      expect(photo).toMatchObject({ kind: "photo", payload: { stopId: stop.payload.data.id, stopName: "Barrow Creek" } });
    });

    test("real queue: the stop and its photo are persisted in IndexedDB while the network is down", async () => {
      const real = await vi.importActual<typeof import("./offline/queue")>("./offline/queue");
      enqueueMock.mockImplementation(real.enqueue);
      renderAt("/t/abc/add");
      fireEvent.change(await nameInput(), { target: { value: "Barrow Creek" } });
      await waitFor(() => expect(maps.length).toBeGreaterThan(0));
      await tapMap(-21.5, 133.5);
      await pick(jpg("a.jpg"));
      await waitFor(() => expect(removeBtns()).toHaveLength(1));
      fireEvent.click(saveBtn());
      await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
      const [stop, photo] = batch(0) as [QueueItem, PhotoItem];
      let stored: QueueRecord[] = [];
      const unsub = real.subscribe((e) => (stored = e));
      await waitFor(() => expect(stored.some((e) => e.payload.data.id === photo.payload.data.id)).toBe(true));
      unsub();
      const s = stored.find((e) => e.payload.data.id === stop.payload.data.id)!;
      const p = stored.find((e) => e.payload.data.id === photo.payload.data.id)!;
      expect(s.key).toBeLessThan(p.key); // the stop drains before its photo
      expect(p.payload).toEqual(photo.payload);
      expect(p.kind === "photo" && new TextDecoder().decode(p.blob)).toBe("jpeg:a.jpg");
    });
  });
});
