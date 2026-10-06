import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";

// t-am-fe-create-trip: `/trips/new` through the real route tree and generated
// client; only globalThis.fetch is mocked, routed by URL.

const ME = { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" };
const ID = "22222222-2222-4222-8222-222222222222";
const TRIP = {
  id: ID,
  name: "Spring loop",
  startDate: "2026-10-12",
  bikes: [],
  access: "member",
  visibility: "public",
  publicDelayHours: 24,
  riderCount: 1,
  lastPublicStopAt: null,
  viewer: { role: "leader" },
};

type Handler = (init?: RequestInit) => Response | Promise<Response>;
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (status: number, code: string, message: string, headers?: Record<string, string>) =>
  json(status, { error: { code, message } }, headers);

let routes: Record<string, Handler>;
let posts: { id: string; name: string; startDate: string; visibility: string }[];

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

const type = (label: string | RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));

async function fill(visibility: "public" | "private" = "private") {
  await screen.findByRole("heading", { name: "Create a trip" });
  type("Trip name", "  Spring loop ");
  type("Start date", "2026-10-12");
  if (visibility === "private") fireEvent.click(screen.getByLabelText(/Private/));
}

beforeEach(() => {
  localStorage.clear();
  posts = [];
  routes = {
    "GET /api/v2/auth/me": () => json(200, ME),
    [`GET /api/v2/trips/${ID}`]: () => json(200, TRIP),
  };
  vi.stubGlobal("BroadcastChannel", class { postMessage() {} close() {} addEventListener() {} removeEventListener() {} });
  vi.stubGlobal("scrollTo", () => {});
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    if (key === "POST /api/v2/trips") posts.push(JSON.parse(String(init?.body)));
    const h = routes[key];
    if (!h) throw new TypeError(`unexpected ${key}`);
    return h(init);
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("/trips/new", () => {
  test("signed out: redirected to /signin?next=/trips/new", async () => {
    routes["GET /api/v2/auth/me"] = () => envelope(401, "UNAUTHENTICATED", "Sign in to continue.");
    const router = renderAt("/trips/new");
    await waitFor(() => expect(router.state.location.pathname).toBe("/signin"));
    expect(router.state.location.search).toMatchObject({ next: "/trips/new" });
  });

  test("201: private sends no warning dialog, a fresh canonical UUID, trimmed name; navigates", async () => {
    routes["POST /api/v2/trips"] = () => json(201, TRIP);
    const router = renderAt("/trips/new");
    await fill("private");
    expect(screen.getByText("Riders can't join a private trip.")).toBeTruthy();
    click("Create trip");
    await waitFor(() => expect(router.state.location.pathname).toBe(`/trips/${ID}`));
    expect(posts).toHaveLength(1);
    expect(posts[0].id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    expect(posts[0]).toMatchObject({ name: "Spring loop", startDate: "2026-10-12", visibility: "private" });
  });

  test("public is the default: the warning is visible and one request is sent", async () => {
    routes["POST /api/v2/trips"] = () => json(200, TRIP);
    const router = renderAt("/trips/new");
    await fill("public");
    expect(screen.getByText("Check your first stop.")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Publish this trip?" })).toBeNull();
    click("Create trip");
    await waitFor(() => expect(router.state.location.pathname).toBe(`/trips/${ID}`)); // 200 replay navigates too
    expect(posts).toHaveLength(1);
    expect(posts[0].visibility).toBe("public");
  });

  test("a double tap sends one request", async () => {
    routes["POST /api/v2/trips"] = () => new Promise((r) => setTimeout(() => r(json(201, TRIP)), 50));
    renderAt("/trips/new");
    await fill("private");
    const btn = screen.getByRole("button", { name: "Create trip" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await settle();
    expect(posts).toHaveLength(1);
  });

  test("a network failure retries under the same id", async () => {
    let n = 0;
    routes["POST /api/v2/trips"] = () => {
      if (++n === 1) throw new TypeError("Failed to fetch");
      return json(201, TRIP);
    };
    renderAt("/trips/new");
    await fill("private");
    click("Create trip");
    await screen.findByText("You need a connection to create a trip.");
    click("Create trip");
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].id).toBe(posts[0].id);
  });

  test("retry after a 422 reuses the id", async () => {
    let n = 0;
    routes["POST /api/v2/trips"] = () => (++n === 1 ? envelope(422, "VALIDATION_ERROR", "Bad name.") : json(201, TRIP));
    renderAt("/trips/new");
    await fill("private");
    click("Create trip");
    await screen.findByText("Bad name.");
    click("Create trip");
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].id).toBe(posts[0].id);
  });

  test("409 shows the server message and the next tap uses a fresh id", async () => {
    let n = 0;
    routes["POST /api/v2/trips"] = () => (++n === 1 ? envelope(409, "CONFLICT", "You have reached the limit of 20 trips.") : json(201, TRIP));
    renderAt("/trips/new");
    await fill("private");
    click("Create trip");
    await screen.findByText("You have reached the limit of 20 trips.");
    click("Create trip");
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].id).not.toBe(posts[0].id);
  });

  test("429 shows the message, the countdown and disables Create", async () => {
    routes["POST /api/v2/trips"] = () => envelope(429, "RATE_LIMITED", "You can create 3 trips a day.", { "Retry-After": "7200" });
    renderAt("/trips/new");
    await fill("private");
    click("Create trip");
    await screen.findByText("You can create 3 trips a day.");
    expect(screen.getByText("Try again in 2 hours.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Create trip" }) as HTMLButtonElement).disabled).toBe(true);
  });

  test("offline: notice shown, input kept, submit disabled, nothing sent", async () => {
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    renderAt("/trips/new");
    await fill("private");
    expect(screen.getByText("You need a connection to create a trip.")).toBeTruthy();
    expect((screen.getByLabelText("Trip name") as HTMLInputElement).value).toBe("  Spring loop ");
    const btn = screen.getByRole("button", { name: "Create trip" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.submit(btn.closest("form")!);
    await settle();
    expect(posts).toHaveLength(0);
  });

  test("field errors block the request", async () => {
    renderAt("/trips/new");
    await screen.findByRole("heading", { name: "Create a trip" });
    click("Create trip");
    expect(await screen.findByText("Enter a trip name.")).toBeTruthy();
    expect(screen.getByText("Choose a start date.")).toBeTruthy();
    expect(posts).toHaveLength(0);
  });

  test("Discover shows Create a trip to signed-in users only", async () => {
    routes["GET /api/v2/trips?limit=20"] = () => json(200, { items: [], nextCursor: null });
    routes["GET /api/v2/me/trips"] = () => json(200, []);
    renderAt("/");
    expect(await screen.findByRole("link", { name: "Create a trip" })).toBeTruthy();
  });
});
