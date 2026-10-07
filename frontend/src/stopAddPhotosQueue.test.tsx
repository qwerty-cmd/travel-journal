import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { TripOut } from "./api/gen/types/TripOut";
import { setNavigatorLocks } from "./offline/testLocks";

// test-writer, contract-first: "add photos to an existing stop" end to end with the
// REAL offline queue. Sources: docs/design/screens/stop-detail.md (States table,
// Save behaviour, Handoff checklist), docs/api-contract.md row
// `POST /api/v2/trips/{tripId}/stops/{stopId}/photos` and "Offline-queue
// classification (Entry 29)". Only fetch, BroadcastChannel (real, closed after)
// and processPhoto (jsdom can't decode images) are stubbed.
// Render/picker details are dev's (stopAddPhotos.test.tsx); not repeated here.

const processPhotoMock = vi.hoisted(() => vi.fn<(file: File) => Promise<{ blob: Blob; takenAt: string }>>());
vi.mock("./photo", async (orig) => ({ ...(await orig<typeof import("./photo")>()), processPhoto: processPhotoMock }));

const TRIP = "11111111-1111-4111-8111-111111111111";
const STOP_ID = "33333333-3333-4333-8333-333333333333";
const RIDER = "rider-acct-5c2d";
const OTHER = "other-acct-91ab";
const PHOTOS_PATH = `/api/v2/trips/${TRIP}/stops/${STOP_ID}/photos`;
const STOP = { id: STOP_ID, tripId: TRIP, name: "Coober Pedy", arrivedAt: "2026-10-13T08:00:00Z", lat: -29, lng: 134.7, locationSource: "gps", notes: null };
const photoOut = (id: string) => ({ id, stopId: STOP_ID, url: `https://img/${id}.jpg`, uploadedBy: "Wes", takenAt: STOP.arrivedAt, archived: false });
const EXISTING = photoOut("p-existing");
const JPEG = [0xff, 0xd8, 0xff, 0xe0, 0x42];

const trip = (role: string | undefined, access = "rider"): TripOut =>
  ({
    id: TRIP,
    name: "Stuart Hwy 2026",
    startDate: "2026-10-12",
    bikes: [],
    access,
    visibility: "public",
    publicDelayHours: 0,
    riderCount: 2,
    lastPublicStopAt: null,
    ...(role === undefined ? {} : { viewer: { role } }),
  }) as TripOut;

const setImmediate = (globalThis as unknown as { setImmediate: (f: () => void) => void }).setImmediate;
const flush = async (n = 60) => {
  for (let i = 0; i < n; i++) await new Promise<void>((r) => setImmediate(r));
};
const settle = () => act(() => flush());
async function until(cond: () => boolean | Promise<boolean>, what = "condition") {
  for (let i = 0; i < 2000; i++) {
    if (await cond()) return;
    // React Query's notifyManager batches through setTimeout(0): run due-now timers only.
    await act(async () => {
      if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(0);
      await new Promise<void>((r) => setImmediate(r));
    });
  }
  throw new Error(`timed out waiting for ${what}`);
}
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (status: number, code: string, message: string, headers?: Record<string, string>) =>
  json(status, { error: { code, message } }, headers);
const netDown = (): never => {
  throw new TypeError("Failed to fetch");
};

type Post = { path: string; id: string; fields: Record<string, FormDataEntryValue> };
let posts: Post[]; // POSTs that reached the "server" (i.e. not while offline)
let unexpectedPosts: string[];
let photoGets: number;
let photos: unknown[];
let tripBody: TripOut;
let offline: boolean;
let meId: string | null;
let handler: (p: Post) => Response | Promise<Response>;

const signInAs = (id: string) => {
  meId = id;
  localStorage.setItem("btj.me", JSON.stringify({ id, displayName: "Wes" }));
};

const RealBC = globalThis.BroadcastChannel;
let opened: BroadcastChannel[];
const broadcastSignin = async () => {
  const ch = new RealBC("auth");
  ch.postMessage({ type: "signin" });
  await flush();
  ch.close();
  await settle();
};

type Raw = { key: IDBValidKey; value: Record<string, unknown> };
function rawAll(): Promise<Raw[]> {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open("btj-queue");
    r.onerror = () => reject(r.error);
    r.onsuccess = () => {
      const db = r.result;
      if (!db.objectStoreNames.contains("entries")) {
        db.close();
        return resolve([]);
      }
      const tx = db.transaction("entries", "readonly");
      const out: Raw[] = [];
      const c = tx.objectStore("entries").openCursor();
      c.onsuccess = () => {
        if (c.result) {
          out.push({ key: c.result.key, value: c.result.value });
          c.result.continue();
        }
      };
      tx.oncomplete = () => {
        db.close();
        resolve(out);
      };
      tx.onerror = () => reject(tx.error);
    };
  });
}
const entryId = (r: Raw) => (r.value.payload as { data: { id: string } }).data.id;
async function blobBytes(b: unknown): Promise<number[]> {
  // Stored entries hold an ArrayBuffer from fake-indexeddb's realm, so no instanceof.
  if (b && typeof (b as Blob).arrayBuffer === "function") return Array.from(new Uint8Array(await (b as Blob).arrayBuffer()));
  return Array.from(new Uint8Array(b as ArrayBuffer));
}
const notice = () => document.querySelector('section[aria-label="Unsent stops"]')?.textContent ?? "";

let Q: typeof import("./offline/queue");
async function boot() {
  vi.resetModules();
  Q = await import("./offline/queue");
  const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
  const { RouterProvider, createMemoryHistory, createRouter } = await import("@tanstack/react-router");
  const { routeTree } = await import("./routeTree.gen");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  Q.startQueue(client);
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [`/trips/${TRIP}/stops/${STOP_ID}`] }) });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

// Render as a rider, pick one photo, switch to fake setTimeout (queue backoff /
// Retry-After), Save. Returns the client id the queue recorded.
async function saveOnePhoto(): Promise<string> {
  await boot();
  await screen.findByRole("heading", { name: "Photos (1)" });
  fireEvent.click(await screen.findByRole("button", { name: "Add photos" }));
  fireEvent.change(screen.getByLabelText("Photos"), { target: { files: [new File(["x"], "a.jpg")] } });
  const save = await screen.findByRole("button", { name: "Save 1 photo" });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(save);
  await until(async () => (await rawAll()).length === 1 || posts.length > 0, "photo enqueued");
  const rows = await rawAll();
  return rows.length ? entryId(rows[0]) : posts[0].id;
}

// Every drain trigger the contract names, plus 600 s of fake time.
async function hammer() {
  for (let i = 0; i < 3; i++) await Q.drain();
  window.dispatchEvent(new Event("online"));
  document.dispatchEvent(new Event("visibilitychange"));
  await broadcastSignin();
  await vi.advanceTimersByTimeAsync(600_000);
  await settle();
}

beforeEach(() => {
  vi.stubGlobal("indexedDB", new IDBFactory());
  localStorage.clear();
  opened = [];
  vi.stubGlobal(
    "BroadcastChannel",
    class extends RealBC {
      constructor(name: string) {
        super(name);
        opened.push(this);
      }
    },
  );
  posts = [];
  unexpectedPosts = [];
  photoGets = 0;
  photos = [EXISTING];
  tripBody = trip("rider");
  offline = false;
  signInAs(RIDER);
  handler = (p) => json(201, photoOut(p.id));
  processPhotoMock.mockReset();
  processPhotoMock.mockImplementation(async () => ({ blob: new Blob([new Uint8Array(JPEG)], { type: "image/jpeg" }), takenAt: "2026-10-13T08:15:00+09:30" }));
  Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: () => "blob:x" });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: () => {} });
  Object.defineProperty(navigator, "storage", { configurable: true, value: { persist: vi.fn(async () => true) } });
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  setNavigatorLocks(undefined);
  vi.spyOn(window, "addEventListener");
  vi.spyOn(document, "addEventListener");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (offline) netDown();
      const p = new URL(String(input), "https://testserver").pathname;
      if (init?.method === "POST") {
        if (p !== PHOTOS_PATH || !(init.body instanceof FormData)) {
          unexpectedPosts.push(p);
          return envelope(404, "NOT_FOUND", "x");
        }
        const fields = Object.fromEntries(init.body.entries());
        const post = { path: p, id: String(fields.id), fields };
        posts.push(post);
        return handler(post);
      }
      if (p === "/api/v2/auth/me")
        return meId ? json(200, { id: meId, displayName: "Wes", username: "wes" }) : envelope(401, "UNAUTHENTICATED", "Sign in");
      if (p === `/api/v2/trips/${TRIP}`) return json(200, tripBody);
      if (p === `/api/v2/trips/${TRIP}/stops`) return json(200, [STOP]);
      if (p === PHOTOS_PATH) {
        photoGets++;
        return json(200, photos);
      }
      if (p.endsWith("/map")) return json(200, { type: "FeatureCollection", features: [] });
      return envelope(404, "NOT_FOUND", "x");
    }),
  );
});

afterEach(async () => {
  cleanup();
  await flush();
  for (const ch of opened) ch.close();
  for (const t of [window, document] as EventTarget[]) {
    for (const [type, fn, opts] of vi.mocked(t.addEventListener).mock.calls) {
      if (fn) t.removeEventListener(type, fn, opts);
    }
  }
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
describe("gate: Add photos only for viewer.role rider/leader", () => {
  test.each(["rider", "leader"])("%s sees the control", async (role) => {
    tripBody = trip(role, "viewer");
    await boot();
    await screen.findByRole("heading", { name: "Photos (1)" });
    expect(await screen.findByRole("button", { name: "Add photos" })).toBeTruthy();
  });

  // Legacy `access: "rider"` must never grant the control on its own.
  test.each([["pending"], ["none"], ["visitor"], ["anonymous"], [undefined]])(
    "role %s with legacy access 'rider': no control, no picker",
    async (role) => {
      tripBody = trip(role, "rider");
      await boot();
      await screen.findByRole("heading", { name: "Photos (1)" });
      await settle();
      expect(screen.queryByRole("button", { name: "Add photos" })).toBeNull();
      expect(document.querySelector('input[type="file"]')).toBeNull();
    },
  );
});

// ---------------------------------------------------------------------------
describe("save on an existing stop -> real queue -> v2 POST", () => {
  test("entry carries tripId/stopId/userId; POSTs exactly the v2 path with the client id; 201 refetches photos", async () => {
    let release!: () => void;
    handler = (p) => new Promise<Response>((r) => (release = () => r(json(201, photoOut(p.id)))));
    const id = await saveOnePhoto();

    const [row] = await rawAll();
    expect(row.value).toMatchObject({ kind: "photo", userId: RIDER, payload: { tripId: TRIP, stopId: STOP_ID } });
    expect(row.value.payload).not.toHaveProperty("slug");
    expect(id).toMatch(/^[0-9a-f-]{36}$/i);

    await until(() => posts.length === 1, "POST sent");
    expect(posts[0].path).toBe(PHOTOS_PATH);
    expect(posts[0].id).toBe(id);
    expect(Object.keys(posts[0].fields).sort()).toEqual(["file", "id", "takenAt"]);
    expect(await blobBytes(posts[0].fields.file)).toEqual(JPEG);

    const getsBefore = photoGets;
    photos = [EXISTING, photoOut(id)];
    release();
    await until(async () => (await rawAll()).length === 0, "entry removed on 201");
    await until(() => photoGets > getsBefore, "photos refetched");
    await vi.advanceTimersByTimeAsync(1_000); // a render-batching timer, not a reload
    await until(() => !!screen.queryByRole("heading", { name: "Photos (2)" }), "count updates");
    expect(notice()).not.toMatch(/Waiting to send/);
    expect(unexpectedPosts).toEqual([]);
  });

  test("network error then retry re-sends the SAME client id; a 200 replay counts as success", async () => {
    let calls = 0;
    handler = (p) => (++calls === 1 ? netDown() : json(200, photoOut(p.id)));
    const id = await saveOnePhoto();
    await until(() => posts.length === 1, "first attempt");
    const [row] = await rawAll();
    expect(row.value.failed).toBe(false);

    const getsBefore = photoGets;
    photos = [EXISTING, photoOut(id)];
    await vi.advanceTimersByTimeAsync(5_000); // first backoff step
    await until(() => posts.length === 2, "retry");
    expect(posts.map((p) => p.id)).toEqual([id, id]);
    await until(async () => (await rawAll()).length === 0, "entry removed on 200 replay");
    await until(() => photoGets > getsBefore, "photos refetched after replay");
    await vi.advanceTimersByTimeAsync(1_000);
    await until(() => !!screen.queryByRole("heading", { name: "Photos (2)" }), "count updates");
  });

  test("offline: Save queues, nothing reaches the server until online, then it sends", async () => {
    await boot();
    await screen.findByRole("heading", { name: "Photos (1)" });
    offline = true;
    fireEvent.click(await screen.findByRole("button", { name: "Add photos" }));
    fireEvent.change(screen.getByLabelText("Photos"), { target: { files: [new File(["x"], "a.jpg")] } });
    const save = await screen.findByRole("button", { name: "Save 1 photo" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    fireEvent.click(save);
    await until(async () => (await rawAll()).length === 1, "queued");
    await until(() => /Waiting to send: 1 photo/.test(notice()), "waiting notice");
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();
    expect(posts).toEqual([]);
    const [row] = await rawAll();
    expect(row.value).toMatchObject({ failed: false, userId: RIDER });

    offline = false;
    window.dispatchEvent(new Event("online"));
    await vi.advanceTimersByTimeAsync(300_000);
    await until(() => posts.length === 1, "sent once online");
    expect(posts[0].id).toBe(entryId(row));
    await until(async () => (await rawAll()).length === 0, "entry removed");
  });
});

// ---------------------------------------------------------------------------
describe("Entry 29 classification on the stop-photo POST", () => {
  test("401: pauses, entry kept (not failed, attempts 0), 'Sign in to send 1 item'; sends after sign-in", async () => {
    handler = () => envelope(401, "UNAUTHENTICATED", "Sign in to continue");
    const id = await saveOnePhoto();
    await until(() => posts.length === 1, "first attempt");
    await settle();
    const [row] = await rawAll();
    expect(row.value).toMatchObject({ failed: false, attempts: 0 });
    expect(await blobBytes(row.value.blob ?? row.value.file)).toEqual(JPEG);
    await until(() => /Sign in to send 1 item/.test(notice()), "paused notice");

    await vi.advanceTimersByTimeAsync(600_000);
    await settle();
    expect(posts).toHaveLength(1); // paused: no timer-driven resend

    handler = (p) => json(201, photoOut(p.id));
    await broadcastSignin();
    await until(() => posts.length === 2, "resumed after sign-in");
    expect(posts[1].id).toBe(id);
    await until(async () => (await rawAll()).length === 0, "sent");
  });

  test.each([
    [403, "FORBIDDEN", "You're no longer a rider on this trip"],
    [404, "NOT_FOUND", "Stop not found"],
    [409, "CONFLICT", "That photo id is already in use"],
    [422, "VALIDATION_ERROR", "Photo must be a JPEG"],
  ])("%i %s: never retried, failed with the envelope message, blob kept, 'Save photo to this device' offered", async (status, code, message) => {
    handler = () => envelope(status, code, message);
    const id = await saveOnePhoto();
    await until(() => posts.length === 1, "first attempt");
    await settle();
    await hammer();
    expect(posts.map((p) => p.id)).toEqual([id]);

    const [row] = await rawAll();
    expect(row.value).toMatchObject({ failed: true, lastError: message });
    expect(await blobBytes(row.value.blob ?? row.value.file)).toEqual(JPEG);
    await until(() => notice().includes(message), "failed message shown");
    expect(screen.getByRole("button", { name: /save photo to this device/i })).toBeTruthy();
  });

  test("429 Retry-After: 7 -> no resend before 7 s, resend at 7 s; entry stays Waiting, not failed", async () => {
    let calls = 0;
    handler = (p) => (++calls === 1 ? envelope(429, "RATE_LIMITED", "Too many requests", { "Retry-After": "7" }) : json(201, photoOut(p.id)));
    const id = await saveOnePhoto();
    await until(() => posts.length === 1, "first attempt");
    await settle();
    expect((await rawAll())[0].value.failed).toBe(false);
    await until(() => /Waiting to send: 1 photo/.test(notice()), "stays Waiting");
    expect(notice()).not.toMatch(/Too many requests/);

    await vi.advanceTimersByTimeAsync(6_999);
    await flush();
    expect(posts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await until(() => posts.length === 2, "resend at 7 s");
    expect(posts[1].id).toBe(id);
  });

  test("another account signs in: the first account's queued photo is held, not sent; sends when the owner returns", async () => {
    handler = netDown; // can't send yet
    const id = await saveOnePhoto();
    await until(() => posts.length >= 1, "first attempt");
    await settle();
    const n = posts.length;

    signInAs(OTHER);
    handler = (p) => json(201, photoOut(p.id));
    await hammer();
    expect(posts).toHaveLength(n);
    const [row] = await rawAll();
    expect(row.value).toMatchObject({ userId: RIDER, failed: false });

    signInAs(RIDER);
    await broadcastSignin();
    await vi.advanceTimersByTimeAsync(300_000);
    await until(() => posts.length === n + 1, "owner's session sends it");
    expect(posts[n].id).toBe(id);
  });
});
