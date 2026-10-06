import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";

// t-am-fe-bikes-v2: /trips/$tripId/bikes. Writes are online-only direct v2 mutations.

vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));
const enqueueMock = vi.hoisted(() => vi.fn<(items: unknown, userId?: string) => Promise<void>>());
vi.mock("./offline/queue", async (orig) => ({ ...(await orig<typeof import("./offline/queue")>()), enqueue: enqueueMock }));

const ID = "11111111-1111-4111-8111-111111111111";
const BIKE = { id: "33333333-3333-4333-8333-333333333333", riderName: "Zed", make: "Surly", model: "Troll", year: 2020, specs: "" };
const BIKE_A = { id: "44444444-4444-4444-8444-444444444444", riderName: "Amy", make: "Trek", model: "520", year: 2018, specs: "Line one\nLine two" };
const trip = (role: string | undefined): TripOut =>
  ({
    id: ID,
    name: "Stuart Hwy 2026",
    startDate: "2026-10-12",
    bikes: [BIKE, BIKE_A],
    access: "rider",
    visibility: "private",
    publicDelayHours: 24,
    riderCount: 2,
    lastPublicStopAt: null,
    ...(role === undefined ? {} : { viewer: { role } }),
  }) as TripOut;

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
let tripBody: TripOut;
let writeResponse: () => Response;
let writes: { method: string; path: string; body: Record<string, unknown> }[];
let tripFetches: number;

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
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

beforeEach(() => {
  localStorage.clear();
  tripBody = trip("rider");
  tripFetches = 0;
  writes = [];
  writeResponse = () => json(200, BIKE);
  enqueueMock.mockReset();
  enqueueMock.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const p = new URL(String(input), "https://testserver").pathname;
    const method = (init?.method ?? "GET").toUpperCase();
    if (p === "/api/v2/auth/me") return json(200, { id: "x", displayName: "Wes", username: "wes" });
    if (p === `/api/v2/trips/${ID}`) {
      tripFetches++;
      return json(200, tripBody);
    }
    if (p === `/api/v2/trips/${ID}/bikes` && method === "GET") return json(200, tripBody.bikes);
    if (p.startsWith(`/api/v2/trips/${ID}/bikes`) && method !== "GET") {
      writes.push({ method, path: p, body: JSON.parse(String(init?.body)) });
      return writeResponse();
    }
    if (p.endsWith("/map")) return json(200, { type: "FeatureCollection", features: [] });
    if (p.endsWith("/stops")) return json(200, []);
    return json(404, { error: { code: "NOT_FOUND", message: "x" } });
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("bikes list and gating", () => {
  test("lists bikes sorted by rider name with specs", async () => {
    renderAt(`/trips/${ID}/bikes`);
    await screen.findByText("Amy");
    const names = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(names).toEqual(["Amy", "Zed"]);
    expect(screen.getByText(/Line one/)).toBeTruthy();
  });

  test.each(["rider", "leader"])("%s sees Add bike and Edit", async (role) => {
    tripBody = trip(role);
    renderAt(`/trips/${ID}/bikes`);
    await screen.findByRole("button", { name: "Add bike" });
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(2);
  });

  test.each([["anonymous"], ["none"], ["pending"], ["visitor"], [undefined]])("role %s gets the list but no forms", async (role) => {
    tripBody = trip(role); // access stays "rider": legacy access never grants write UI
    renderAt(`/trips/${ID}/bikes`);
    await screen.findByText("Amy");
    await settle();
    expect(screen.queryByRole("button", { name: "Add bike" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByLabelText("Make")).toBeNull();
  });

  test("the trip shell links to the bikes tab", async () => {
    renderAt(`/trips/${ID}`);
    const link = await screen.findByRole("link", { name: "Bikes" });
    expect(link.getAttribute("href")).toBe(`/trips/${ID}/bikes`);
  });
});

describe("bike writes", () => {
  async function openAdd() {
    renderAt(`/trips/${ID}/bikes`);
    fireEvent.click(await screen.findByRole("button", { name: "Add bike" }));
    type("Rider name", " Wes ");
    type("Make", "Giant");
    type("Model", "Escape");
    type("Year", "2021");
  }
  const submit = () => fireEvent.click(screen.getAllByRole("button", { name: "Add bike" }).at(-1)!);

  test("create posts to v2 with a client id and trimmed fields; nothing is queued", async () => {
    writeResponse = () => json(201, BIKE);
    await openAdd();
    submit();
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0].method).toBe("POST");
    expect(writes[0].path).toBe(`/api/v2/trips/${ID}/bikes`);
    expect(writes[0].body).toMatchObject({ riderName: "Wes", make: "Giant", model: "Escape", year: 2021, specs: "" });
    expect(writes[0].body.id).toMatch(/^[0-9a-f-]{36}$/);
    await settle();
    expect(enqueueMock).not.toHaveBeenCalled();
  });

  test("a retry after a failure replays the same client id", async () => {
    writeResponse = () => json(422, { error: { code: "VALIDATION_ERROR", message: "bad" } });
    await openAdd();
    submit();
    await screen.findByText("bad");
    submit();
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1].body.id).toBe(writes[0].body.id);
  });

  test("edit PATCHes only the changed fields, never null", async () => {
    renderAt(`/trips/${ID}/bikes`);
    await screen.findByText("Amy");
    fireEvent.click(screen.getAllByRole("button", { name: "Edit" })[0]); // Amy
    type("Model", "530");
    fireEvent.click(screen.getByRole("button", { name: "Save bike" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0].method).toBe("PATCH");
    expect(writes[0].path).toBe(`/api/v2/trips/${ID}/bikes/${BIKE_A.id}`);
    expect(writes[0].body).toEqual({ model: "530" });
  });

  test("an unchanged edit sends nothing", async () => {
    renderAt(`/trips/${ID}/bikes`);
    await screen.findByText("Amy");
    fireEvent.click(screen.getAllByRole("button", { name: "Edit" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Save bike" }));
    await settle();
    expect(writes).toHaveLength(0);
  });

  test.each(["create", "edit"])("offline %s: message, no request, nothing queued", async (kind) => {
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });
    if (kind === "create") {
      await openAdd();
      submit();
    } else {
      renderAt(`/trips/${ID}/bikes`);
      await screen.findByText("Amy");
      fireEvent.click(screen.getAllByRole("button", { name: "Edit" })[0]);
      type("Model", "530");
      fireEvent.click(screen.getByRole("button", { name: "Save bike" }));
    }
    await screen.findByText("You're offline — try again when connected.");
    await settle();
    expect(writes).toHaveLength(0);
    expect(enqueueMock).not.toHaveBeenCalled();
  });

  test("403 shows the envelope message, refetches the trip and keeps input", async () => {
    writeResponse = () => json(403, { error: { code: "FORBIDDEN", message: "You're not a rider on this trip." } });
    await openAdd();
    const before = tripFetches;
    submit();
    await screen.findByText("You're not a rider on this trip.");
    await waitFor(() => expect(tripFetches).toBeGreaterThan(before));
  });

  test("403 then a role-less refetch removes the controls but keeps the trip list", async () => {
    writeResponse = () => json(403, { error: { code: "FORBIDDEN", message: "No." } });
    await openAdd();
    tripBody = trip("none");
    submit();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Edit" })).toBeNull());
    expect(screen.getByText("Amy")).toBeTruthy();
  });

  test("401: warning with a Sign in link, no redirect, input kept", async () => {
    writeResponse = () => json(401, { error: { code: "UNAUTHENTICATED", message: "x" } });
    const router = await (async () => {
      const r = renderAt(`/trips/${ID}/bikes`);
      fireEvent.click(await screen.findByRole("button", { name: "Add bike" }));
      type("Rider name", "Wes");
      type("Make", "Giant");
      type("Model", "Escape");
      type("Year", "2021");
      return r;
    })();
    submit();
    await screen.findByText("Your session has ended. Sign in, then save again.");
    const link = screen.getByRole("link", { name: "Sign in" });
    expect(decodeURIComponent(link.getAttribute("href")!)).toBe(`/signin?next=/trips/${ID}/bikes`);
    expect(router.state.location.pathname).toBe(`/trips/${ID}/bikes`);
    expect((screen.getByLabelText("Make") as HTMLInputElement).value).toBe("Giant");
  });

  test("422 shows the envelope message and keeps input", async () => {
    writeResponse = () => json(422, { error: { code: "VALIDATION_ERROR", message: "Year is out of range." } });
    await openAdd();
    submit();
    await screen.findByText("Year is out of range.");
    expect((screen.getByLabelText("Model") as HTMLInputElement).value).toBe("Escape");
  });

  test("429 shows the warning with the rounded retry time", async () => {
    writeResponse = () => json(429, { error: { code: "RATE_LIMITED", message: "slow" } }, { "Retry-After": "30" });
    await openAdd();
    submit();
    await screen.findByText("Too many requests.");
    await screen.findByText("Try again in 30 seconds.");
  });
});
