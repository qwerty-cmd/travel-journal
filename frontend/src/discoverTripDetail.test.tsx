// The root route mounts the real QueueNotice, which reads the queue's IndexedDB.
import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";
import type { TripSummaryOut } from "./api/gen/types/TripSummaryOut";

// t-am-fe-discover-trip-detail: Discover at `/` and the v2 trip pages at
// /trips/$tripId (decision-log Entries 19 and 29). Real route tree and
// generated client; only globalThis.fetch is mocked, routed by URL.

const ID = "11111111-1111-4111-8111-111111111111";
const TRIP: TripOut = {
  id: ID,
  name: "Stuart Hwy 2026",
  startDate: "2026-10-12",
  bikes: [{ id: "b1", riderName: "Ann", make: "Surly", model: "LHT", year: 2019, specs: "" }],
  access: "viewer",
  visibility: "public",
  publicDelayHours: 24,
  riderCount: 3,
  lastPublicStopAt: null,
  viewer: { role: "anonymous" },
};
const STOP = {
  id: "s1",
  tripId: ID,
  name: "Coober Pedy",
  arrivedAt: "2026-10-13T08:00:00Z",
  lat: -29,
  lng: 134.7,
  locationSource: "gps",
  notes: null,
};
const summary = (n: number): TripSummaryOut => ({
  id: `t${n}`,
  name: `Trip ${n}`,
  startDate: "2026-10-12",
  riderCount: 1,
  lastPublicStopAt: null,
});

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const envelope = (code: string, message = code.toLowerCase()) => ({ error: { code, message } });
const networkDown = () => Promise.reject(new TypeError("Failed to fetch"));
const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));

type Handler = (url: URL) => Promise<Response> | undefined;
let handler: Handler;
const calls: string[] = [];

/** Default answers: signed out, no public trips, empty map/stops; `over` wins. */
function serve(over: Handler = () => undefined) {
  handler = (url) => {
    const hit = over(url);
    if (hit) return hit;
    const p = url.pathname;
    if (p === "/api/v2/auth/me") return Promise.resolve(json(401, envelope("UNAUTHENTICATED")));
    if (p === "/api/v2/trips") return Promise.resolve(json(200, { items: [], nextCursor: null }));
    if (p.endsWith("/map")) return Promise.resolve(json(200, { type: "FeatureCollection", features: [] }));
    if (p.endsWith("/stops")) return Promise.resolve(json(200, []));
    if (p.endsWith("/photos")) return Promise.resolve(json(200, []));
    return Promise.resolve(json(404, envelope("NOT_FOUND")));
  };
}

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

beforeEach(() => {
  localStorage.clear();
  calls.length = 0;
  serve();
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = new URL(String(input), "https://testserver");
    calls.push(url.pathname + url.search);
    return handler(url)!;
  });
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Discover", () => {
  test("lists public trips; Show more appends the next page, focuses its first card, then disappears", async () => {
    serve((url) => {
      if (url.pathname !== "/api/v2/trips") return;
      const cursor = url.searchParams.get("cursor");
      if (cursor === null) return Promise.resolve(json(200, { items: [summary(1), summary(2)], nextCursor: "c2" }));
      if (cursor === "c2") return Promise.resolve(json(200, { items: [summary(3)], nextCursor: null }));
    });
    renderAt("/");
    const first = await screen.findByRole("link", { name: "Trip 1" });
    expect(first.getAttribute("href")).toBe("/trips/t1");
    expect(screen.getAllByText("Public")).toHaveLength(2);
    expect(screen.getAllByText("Started 12 Oct 2026 · 1 rider")).toHaveLength(2);
    expect(screen.getAllByText("No public stops yet")).toHaveLength(2);
    expect(calls).toContain("/api/v2/trips?limit=20");

    fireEvent.click(screen.getByRole("button", { name: "Show more trips" }));
    const third = await screen.findByRole("link", { name: "Trip 3" });
    await waitFor(() => expect(document.activeElement).toBe(third));
    expect(calls).toContain("/api/v2/trips?cursor=c2&limit=20");
    expect(screen.getByText("1 more trip loaded")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Trip 1" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Show more trips" })).toBeNull();
  });

  test("a 422 on a later page shows the load-more error; Try again restarts from page one", async () => {
    serve((url) => {
      if (url.pathname !== "/api/v2/trips") return;
      if (url.searchParams.get("cursor") === "bad") return Promise.resolve(json(422, envelope("VALIDATION_ERROR")));
      return Promise.resolve(json(200, { items: [summary(1)], nextCursor: "bad" }));
    });
    renderAt("/");
    fireEvent.click(await screen.findByRole("button", { name: "Show more trips" }));
    expect(await screen.findByText("Couldn't load more trips")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Trip 1" })).toBeTruthy(); // loaded cards stay
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Show more trips" })).toBeTruthy();
    expect(screen.queryByText("Couldn't load more trips")).toBeNull();
  });

  test("no public trips: empty state; signed out: no /me/trips request", async () => {
    renderAt("/");
    expect(await screen.findByText("No public trips yet")).toBeTruthy();
    await settle();
    expect(calls.some((c) => c.startsWith("/api/v2/me/trips"))).toBe(false);
    expect(screen.queryByRole("heading", { name: "Your trips" })).toBeNull();
  });

  test("signed in: Your trips lists /me/trips with role badges", async () => {
    serve((url) => {
      if (url.pathname === "/api/v2/auth/me") return Promise.resolve(json(200, { id: "u1", username: "ann", displayName: "Ann", createdAt: "2026-10-01T00:00:00Z" }));
      if (url.pathname === "/api/v2/me/trips")
        return Promise.resolve(json(200, [{ id: ID, name: "My ride", startDate: "2026-10-12", role: "leader" }]));
    });
    renderAt("/");
    const link = await screen.findByRole("link", { name: "My ride" });
    expect(link.getAttribute("href")).toBe(`/trips/${ID}`);
    expect(screen.getByText("Leader")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Account" })).toBeTruthy();
  });
});

describe("/trips/$tripId", () => {
  const tripAs = (over: Partial<TripOut>) => {
    const trip = { ...TRIP, ...over };
    serve((url) => (url.pathname === `/api/v2/trips/${ID}` ? Promise.resolve(json(200, trip)) : undefined));
    return trip;
  };

  test("anonymous on a public trip: v2 reads, delay note, bikes, no write UI, persisted per trip id", async () => {
    const trip = tripAs({});
    renderAt(`/trips/${ID}`);
    expect(await screen.findByRole("heading", { level: 1, name: TRIP.name })).toBeTruthy();
    expect(screen.getByText("Starts 12 Oct 2026 · 3 riders")).toBeTruthy();
    expect(screen.getByText("Public")).toBeTruthy();
    expect(screen.getByText("Stops appear here 24 hours after they're added.")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Ann" })).toBeTruthy();
    expect(screen.getByText("2019 Surly LHT")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /add|edit/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /add stop/i })).toBeNull();
    await waitFor(() => expect(JSON.parse(localStorage.getItem(`btj.tripById.${ID}`)!)).toEqual(trip));
    expect(calls).toEqual(expect.arrayContaining([`/api/v2/trips/${ID}`, `/api/v2/trips/${ID}/map`, `/api/v2/trips/${ID}/stops`]));
    expect(calls.some((c) => c.startsWith("/api/trips/"))).toBe(false);
  });

  test.each([
    ["rider", "Rider"],
    ["leader", "Leader"],
  ] as const)("%s: role badge, no delay note", async (role, badge) => {
    tripAs({ viewer: { role }, access: "rider" });
    renderAt(`/trips/${ID}`);
    expect(await screen.findByText(badge)).toBeTruthy();
    expect(screen.queryByText(/Stops appear here/)).toBeNull();
  });

  test.each([
    ["private trip", { visibility: "private" as const }],
    ["delay 0", { publicDelayHours: 0 }],
  ])("non-member, %s: no delay note", async (_, over) => {
    tripAs(over);
    renderAt(`/trips/${ID}`);
    expect(await screen.findByRole("heading", { level: 1, name: TRIP.name })).toBeTruthy();
    expect(screen.queryByText(/Stops appear here/)).toBeNull();
  });

  test("404 shows Trip not found (signed-out copy) and clears the persisted record", async () => {
    localStorage.setItem(`btj.tripById.${ID}`, JSON.stringify({ ...TRIP, name: "Cached" }));
    renderAt(`/trips/${ID}`); // default handler: NOT_FOUND for the trip
    expect(await screen.findByRole("heading", { name: "Trip not found" })).toBeTruthy();
    expect(screen.getByText("Check the link. If this is a private trip, sign in with an account that belongs to it.")).toBeTruthy();
    expect(screen.queryByText("Cached")).toBeNull();
    await waitFor(() => expect(localStorage.getItem(`btj.tripById.${ID}`)).toBeNull());
  });

  test("404 signed in: the member copy, identical for any not-found trip", async () => {
    serve((url) =>
      url.pathname === "/api/v2/auth/me"
        ? Promise.resolve(json(200, { id: "u1", username: "ann", displayName: "Ann", createdAt: "2026-10-01T00:00:00Z" }))
        : undefined,
    );
    renderAt(`/trips/${ID}`);
    expect(await screen.findByText("Check the link. If this is a private trip, you need to be a member to see it.")).toBeTruthy();
  });

  test("offline: the persisted TripOut is initialData and renders", async () => {
    localStorage.setItem(`btj.tripById.${ID}`, JSON.stringify({ ...TRIP, name: "Cached" }));
    serve((url) => (url.pathname.startsWith("/api/v2/trips/") ? networkDown() : undefined));
    renderAt(`/trips/${ID}`);
    expect(await screen.findByRole("heading", { level: 1, name: "Cached" })).toBeTruthy();
    await settle();
    expect(screen.getByRole("heading", { level: 1, name: "Cached" })).toBeTruthy();
    expect(screen.queryByText(/Can't reach the server/)).toBeNull();
  });

  test("no cache and unreachable: error with Try again", async () => {
    serve((url) => (url.pathname.startsWith("/api/v2/trips/") ? networkDown() : undefined));
    renderAt(`/trips/${ID}`);
    expect(await screen.findByText("Can't reach the server. Check your signal and try again.")).toBeTruthy();
    tripAs({});
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: TRIP.name })).toBeTruthy();
  });

  describe("pre-extension cached TripOut (no viewer, no visibility)", () => {
    const { viewer: _v, visibility: _vis, publicDelayHours: _d, riderCount: _r, lastPublicStopAt: _l, ...legacy } = TRIP;
    const OLD = { ...legacy, name: "Old cache", access: "rider" };

    test("offline: renders without crashing, no role or visibility badge, no write UI", async () => {
      localStorage.setItem(`btj.tripById.${ID}`, JSON.stringify(OLD));
      serve((url) => (url.pathname.startsWith("/api/v2/trips/") ? networkDown() : undefined));
      renderAt(`/trips/${ID}`);
      expect(await screen.findByRole("heading", { level: 1, name: "Old cache" })).toBeTruthy();
      expect(screen.getByText("Starts 12 Oct 2026")).toBeTruthy();
      expect(screen.queryByText(/^(Public|Private|Rider|Leader|Pending)$/)).toBeNull();
      expect(screen.queryByText(/Stops appear here/)).toBeNull();
      expect(screen.queryByRole("button", { name: /add|edit/i })).toBeNull();
    });

    test("online: it is refetched and replaced by the server's TripOut", async () => {
      localStorage.setItem(`btj.tripById.${ID}`, JSON.stringify(OLD));
      const trip = tripAs({});
      renderAt(`/trips/${ID}`);
      expect(await screen.findByRole("heading", { level: 1, name: TRIP.name })).toBeTruthy();
      expect(screen.getByText("Stops appear here 24 hours after they're added.")).toBeTruthy();
      await waitFor(() => expect(JSON.parse(localStorage.getItem(`btj.tripById.${ID}`)!)).toEqual(trip));
    });
  });

  test("a timeline row opens the v2 stop detail with its gallery", async () => {
    tripAs({});
    serve((url) => {
      if (url.pathname === `/api/v2/trips/${ID}`) return Promise.resolve(json(200, TRIP));
      if (url.pathname === `/api/v2/trips/${ID}/stops`) return Promise.resolve(json(200, [STOP]));
      if (url.pathname === `/api/v2/trips/${ID}/stops/s1/photos`)
        return Promise.resolve(
          json(200, [{ id: "p1", stopId: "s1", url: "https://img/p1.jpg", uploadedBy: "Ann", takenAt: STOP.arrivedAt, archived: false }]),
        );
    });
    const router = renderAt(`/trips/${ID}`);
    fireEvent.click(await screen.findByText("Coober Pedy"));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/trips/${ID}/stops/s1`));
    expect(await screen.findByRole("img", { name: "Photo by Ann" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to trip" }).getAttribute("href")).toBe(`/trips/${ID}`);
  });
});
