// The root route mounts the real QueueNotice, which reads the queue's IndexedDB.
import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";

// t-am-fe-signout-cleanup (PR #8 review, CURRENTLY BROKEN). Written from the
// requirement, not the implementation: after sign-out, after a server-side
// session end (401 on `me` while signed in), and after a 401 from password
// change / recovery-code rotation, no private trip data stays readable on the
// device. The offline queue and legacy `btj.trip.<slug>` records survive.
// Real route tree and generated client; only fetch and BroadcastChannel mocked.

const ID = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const LEGACY = "btj.trip.legacy-rider-slug";
const ME = { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" };

// Private markers: none of these may be readable anywhere after sign-out.
const TRIP_NAME = "Secret Desert Ride";
const MEMBER = "Kimberly Quill";
const STOP_NAME = "Hidden Camp";
const REQ_MSG = "please let me in";
const MARKERS = [TRIP_NAME, MEMBER, STOP_NAME, REQ_MSG];

const TRIP = {
  id: ID,
  name: TRIP_NAME,
  startDate: "2026-10-12",
  bikes: [],
  access: "rider",
  visibility: "private",
  publicDelayHours: 0,
  riderCount: 2,
  lastPublicStopAt: null,
  viewer: { role: "leader" },
};
const MEMBERS = [
  { userId: "u1", displayName: "Wes", role: "leader", joinedAt: "2026-10-03T08:00:00Z" },
  { userId: "u2", displayName: MEMBER, role: "rider", joinedAt: "2026-10-03T08:00:00Z" },
];
const STOPS = [{ id: "s1", tripId: ID, name: STOP_NAME, arrivedAt: "2026-10-13T08:00:00Z", lat: -29, lng: 134.7, locationSource: "gps", notes: null }];
const TRIP_REQS = [{ id: "r1", requester: { userId: "u9", displayName: "Applicant" }, state: "pending", via: "direct", message: REQ_MSG, createdAt: "2026-10-05T00:00:00Z" }];
const MY_REQS = [{ id: "r2", tripId: OTHER, tripName: "Other Ride", state: "pending", message: REQ_MSG, createdAt: "2026-10-05T00:00:00Z" }];

const json = (status: number, body: unknown) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const envelope = (status: number, code: string, message: string) => json(status, { error: { code, message } });
const unauth = () => envelope(401, "UNAUTHENTICATED", "Sign in to continue.");
const settle = () => act(() => new Promise((r) => setTimeout(r, 30)));

type Net = "up" | "down" | "hang-trips";
let net: Net;
let session: boolean; // the server's view: is the cookie session alive?
let override: Record<string, () => Response>;
const calls: string[] = [];

function handle(method: string, url: URL): Promise<Response> {
  const p = url.pathname;
  const key = `${method} ${p}`;
  calls.push(key);
  if (net === "down") return Promise.reject(new TypeError("Failed to fetch"));
  if (override[key]) return Promise.resolve(override[key]());
  if (p === "/api/v2/auth/me") return Promise.resolve(session ? json(200, ME) : unauth());
  if (key === "POST /api/v2/auth/signout") {
    session = false;
    return Promise.resolve(json(204, null));
  }
  if (p === "/api/v2/trips") return Promise.resolve(json(200, { items: [], nextCursor: null }));
  if (p === "/api/v2/me/trips") return Promise.resolve(session ? json(200, [{ id: ID, name: TRIP_NAME, startDate: "2026-10-12", role: "leader" }]) : unauth());
  if (p === "/api/v2/me/join-requests") return Promise.resolve(session ? json(200, MY_REQS) : unauth());
  if (p.startsWith(`/api/v2/trips/${ID}`)) return privateTrip(p.slice(`/api/v2/trips/${ID}`.length));
  return Promise.resolve(envelope(404, "NOT_FOUND", "not found"));
}

/** The private trip's reads: members only, so 404 once the session is gone. */
function privateTrip(rest: string): Promise<Response> {
  if (net === "hang-trips") return new Promise<Response>(() => {});
  const body: Record<string, unknown> = {
    "": TRIP,
    "/members": MEMBERS,
    "/stops": STOPS,
    "/map": { type: "FeatureCollection", features: [] },
    "/join-requests": TRIP_REQS,
  };
  if (session && rest in body) return Promise.resolve(json(200, body[rest]));
  if (session && rest.endsWith("/photos")) return Promise.resolve(json(200, []));
  return Promise.resolve(envelope(404, "NOT_FOUND", "not found"));
}

class FakeChannel {
  constructor(readonly name: string) {}
  postMessage() {}
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

type AppRouter = ReturnType<typeof renderAt>;
let qc: QueryClient;
function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

/** Every query key and its data in the TanStack cache, serialised. */
const cacheDump = () => JSON.stringify(qc.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data]));
const expectCacheClean = () => {
  const dump = cacheDump();
  for (const m of MARKERS) expect(dump).not.toContain(m);
};
const tripByIdKeys = () => Object.keys(localStorage).filter((k) => k.startsWith("btj.tripById."));

// --- the offline queue's IndexedDB store (btj-queue / entries) ---------------
function queueDb(): Promise<IDBDatabase> {
  return new Promise((res, rej) => {
    const req = indexedDB.open("btj-queue", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("entries", { autoIncrement: true });
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}
async function queueTx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await queueDb();
  try {
    return await new Promise<T>((res, rej) => {
      const req = fn(db.transaction("entries", mode).objectStore("entries"));
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
  } finally {
    db.close();
  }
}
const QUEUED = { kind: "stop", payload: { tripId: ID, data: { name: "Queued stop" } }, attempts: 3, lastError: "offline", failed: true, userId: "u1" };
const queueCount = () => queueTx("readonly", (s) => s.count());

/** The device as a signed-in leader left it: records, legacy slug cache, a queued entry. */
async function seedDevice({ signedIn }: { signedIn: boolean }) {
  localStorage.setItem(`btj.tripById.${ID}`, JSON.stringify(TRIP));
  localStorage.setItem(`btj.tripById.${OTHER}`, JSON.stringify({ ...TRIP, id: OTHER, name: "Other Ride" }));
  localStorage.setItem(LEGACY, JSON.stringify({ name: "Legacy Ride", access: "rider" }));
  if (signedIn) localStorage.setItem("btj.me", JSON.stringify({ id: "u1", displayName: "Wes" }));
  await queueTx("readwrite", (s) => s.add(QUEUED));
}

/** What must survive every cleanup. */
async function expectQueueAndLegacyKept() {
  expect(localStorage.getItem(LEGACY)).not.toBeNull();
  expect(await queueCount()).toBe(1);
}
async function expectSignedOutClean() {
  await waitFor(() => expect(tripByIdKeys()).toEqual([]));
  expect(localStorage.getItem("btj.me")).toBeNull();
  expectCacheClean();
  await expectQueueAndLegacyKept();
}

/** Navigation is fire-and-forget: awaiting it inside act never settles here. */
const go = (router: AppRouter, opts: object) => {
  void router.navigate(opts);
};

/** Opens the private trip, its members and its join requests so every kind of private query is cached. */
async function warmPrivateCache(router: AppRouter) {
  expect(await screen.findByRole("heading", { level: 1, name: TRIP_NAME })).toBeTruthy();
  go(router, { to: `/trips/${ID}/members`, search: { view: "members" } });
  expect(await screen.findByText(MEMBER)).toBeTruthy();
  go(router, { to: `/trips/${ID}/members`, search: { view: "requests" } });
  expect(await screen.findByText(new RegExp(REQ_MSG))).toBeTruthy();
  const dump = cacheDump();
  for (const m of [TRIP_NAME, MEMBER, REQ_MSG]) expect(dump).toContain(m); // precondition
}

/** No trip name, no leader UI, no members on the private trip pages. */
async function expectNoPrivateTripOnScreen() {
  await settle();
  const text = document.body.textContent ?? "";
  for (const m of MARKERS) expect(text).not.toContain(m);
  expect(screen.queryByText("Leader")).toBeNull();
  expect(screen.queryByRole("link", { name: "Members" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Requests" })).toBeNull();
}

beforeEach(async () => {
  localStorage.clear();
  calls.length = 0;
  net = "up";
  session = true;
  override = {};
  await queueTx("readwrite", (s) => s.clear());
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  vi.stubGlobal("scrollTo", () => {});
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
    handle(init?.method ?? "GET", new URL(String(input), "https://testserver")),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("sign out from /account", () => {
  async function signOutAfterUsingPrivateTrip() {
    await seedDevice({ signedIn: true });
    const router = renderAt(`/trips/${ID}`);
    await warmPrivateCache(router);
    go(router, { to: "/account" });
    await screen.findByText("wes");
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    return router;
  }

  test("removes every btj.tripById record, btj.me and cached private queries; keeps queue + legacy", async () => {
    await signOutAfterUsingPrivateTrip();
    await expectSignedOutClean();
  });

  test("same session, offline: re-opening the private trip shows no name, no leader UI, no members", async () => {
    const router = await signOutAfterUsingPrivateTrip();
    net = "down";
    go(router, { to: `/trips/${ID}` });
    await expectNoPrivateTripOnScreen();
    go(router, { to: `/trips/${ID}/members`, search: { view: "members" } });
    await expectNoPrivateTripOnScreen();
  });

  test("after a reload, offline: re-opening the private trip shows no name, no leader UI", async () => {
    await signOutAfterUsingPrivateTrip();
    cleanup();
    net = "down";
    renderAt(`/trips/${ID}`);
    await expectNoPrivateTripOnScreen();
  });
});

describe("server-side session end: 401 on me", () => {
  test("with btj.me set (app believed it was signed in): same cleanup as sign-out", async () => {
    await seedDevice({ signedIn: true });
    const router = renderAt(`/trips/${ID}`);
    await warmPrivateCache(router);
    // The session ends on the server; the next me fetch (here: Discover mounting
    // it) answers 401. Trip reads hang so only the me 401 can clear anything.
    session = false;
    net = "hang-trips";
    calls.length = 0;
    go(router, { to: "/" });
    await waitFor(() => expect(calls).toContain("GET /api/v2/auth/me"));
    await expectSignedOutClean();

    net = "down";
    go(router, { to: `/trips/${ID}` });
    await expectNoPrivateTripOnScreen();
  });

  test("with btj.me set, cold load: records are wiped by the 401", async () => {
    await seedDevice({ signedIn: true });
    session = false;
    renderAt("/"); // Discover: no trip request that could itself clear a record
    await expectSignedOutClean();
  });

  test("signed-out page load (no btj.me) receiving a 401 wipes nothing", async () => {
    await seedDevice({ signedIn: false });
    session = false;
    renderAt("/");
    await waitFor(() => expect(calls).toContain("GET /api/v2/auth/me"));
    await settle();
    expect(tripByIdKeys().sort()).toEqual([`btj.tripById.${ID}`, `btj.tripById.${OTHER}`]);
    await expectQueueAndLegacyKept();
  });
});

describe("/account: 401 from password change or recovery-code rotation", () => {
  const changePassword = () => {
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "the wrong sentence here" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "the new sentence here" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
  };
  const rotate = async () => {
    fireEvent.click(screen.getByRole("button", { name: "Get a new recovery code" }));
    fireEvent.change(await screen.findByLabelText("Your password"), { target: { value: "the wrong sentence here" } });
    fireEvent.click(screen.getByRole("button", { name: "Get new code" }));
  };
  const cases = [
    ["POST /api/v2/auth/password", changePassword],
    ["POST /api/v2/auth/recovery-code", rotate],
  ] as const;

  async function onAccountWithWarmCache() {
    await seedDevice({ signedIn: true });
    const router = renderAt(`/trips/${ID}`);
    await warmPrivateCache(router);
    go(router, { to: "/account" });
    await screen.findByText("wes");
    return router;
  }

  test.each(cases)("%s 401 (10th wrong password): signed out, cleaned, sent to /signin?next=/account", async (key, act_) => {
    override[key] = () => {
      session = false; // Entry 33: the server deletes the session
      return unauth();
    };
    const router = await onAccountWithWarmCache();
    await act_();
    await waitFor(() => expect(router.state.location.pathname).toBe("/signin"));
    expect(router.state.location.search).toEqual({ next: "/account" });
    await expectSignedOutClean();
  });

  test.each(cases)("%s 403 (attempts 1-9): stays on /account with the error, nothing cleared", async (key, act_) => {
    override[key] = () => envelope(403, "FORBIDDEN", "Your current password is incorrect.");
    const router = await onAccountWithWarmCache();
    await act_();
    expect(await screen.findByText("Your current password is incorrect.")).toBeTruthy();
    await settle();
    expect(router.state.location.pathname).toBe("/account");
    expect(localStorage.getItem("btj.me")).not.toBeNull();
    expect(tripByIdKeys().sort()).toEqual([`btj.tripById.${ID}`, `btj.tripById.${OTHER}`]);
    expect(cacheDump()).toContain(TRIP_NAME);
    await expectQueueAndLegacyKept();
  });
});
