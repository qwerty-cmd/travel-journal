import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";

// t-am-fe-trip-settings: `/trips/$tripId/settings` through the real route tree;
// only globalThis.fetch is mocked.

const ID = "22222222-2222-4222-8222-222222222222";
const ME = { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" };
const trip = (over: Record<string, unknown> = {}) => ({
  id: ID, name: "Spring loop", startDate: "2026-10-12", bikes: [], access: "member",
  visibility: "private", publicDelayHours: 24, riderCount: 1, lastPublicStopAt: null,
  viewer: { role: "leader" }, ...over,
});
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

let current: Record<string, unknown>;
let patches: Record<string, unknown>[];
let patchResponse: () => Response | Promise<Response>;
let getCount: number;

function open() {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [`/trips/${ID}/settings`] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}
const type = (label: string | RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const save = () => fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
const ready = () => screen.findByRole("heading", { name: "Trip settings" });
const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));

beforeEach(() => {
  localStorage.clear();
  current = trip();
  patches = [];
  getCount = 0;
  patchResponse = () => json(200, { ...current, ...patches[patches.length - 1] });
  vi.stubGlobal("BroadcastChannel", class { postMessage() {} close() {} addEventListener() {} removeEventListener() {} });
  vi.stubGlobal("scrollTo", () => {});
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    if (key === "GET /api/v2/auth/me") return json(200, ME);
    if (key === `GET /api/v2/trips/${ID}`) {
      getCount++;
      return json(200, current);
    }
    if (key === `PATCH /api/v2/trips/${ID}`) {
      patches.push(JSON.parse(String(init?.body)));
      return patchResponse();
    }
    throw new TypeError(`unexpected ${key}`);
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("role gate", () => {
  test.each(["rider", "pending", "none"])("%s sees Leaders only and no form", async (role) => {
    current = trip({ viewer: { role } });
    open();
    await screen.findByText("Leaders only");
    expect(screen.queryByLabelText("Trip name")).toBeNull();
  });

  test("a cached trip with no viewer gets no form", async () => {
    const { viewer: _v, ...old } = trip();
    current = old;
    open();
    await screen.findByText("Leaders only");
    expect(screen.queryByRole("button", { name: "Save settings" })).toBeNull();
  });

  test("the shell shows Settings to a leader only", async () => {
    open();
    expect(await screen.findByRole("link", { name: "Settings" })).toBeTruthy();
    cleanup();
    current = trip({ viewer: { role: "rider" } });
    open();
    await screen.findByText("Leaders only");
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
  });
});

describe("saving", () => {
  test("sends only the changed field, then updates and refetches the trip", async () => {
    open();
    await ready();
    type("Trip name", "  New name ");
    const before = getCount;
    save();
    await screen.findByText("Saved.");
    expect(patches).toEqual([{ name: "New name" }]);
    await waitFor(() => expect(getCount).toBeGreaterThan(before));
  });

  test("unchanged form sends nothing", async () => {
    open();
    await ready();
    save();
    await settle();
    expect(patches).toHaveLength(0);
  });

  test("delay chips and visibility are sent alone, never null", async () => {
    open();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "48" }));
    fireEvent.click(screen.getByLabelText(/Public:/));
    save();
    await screen.findByText("Saved.");
    expect(patches).toEqual([{ visibility: "public", publicDelayHours: 48 }]);
  });

  test.each(["-1", "169", "", "1.5", "abc"])("delay %j is rejected on the client", async (v) => {
    open();
    await ready();
    type("Hours before stops are public", v);
    save();
    await screen.findByText("Choose 0 to 168 hours.");
    expect(patches).toHaveLength(0);
  });

  test.each(["0", "168"])("delay %s is accepted", async (v) => {
    open();
    await ready();
    type("Hours before stops are public", v);
    save();
    await screen.findByText("Saved.");
    expect(patches).toEqual([{ publicDelayHours: Number(v) }]);
  });

  test("empty name is rejected on the client", async () => {
    open();
    await ready();
    type("Trip name", "   ");
    save();
    await screen.findByText("Enter a trip name.");
    expect(patches).toHaveLength(0);
  });

  test("private to public shows the Publish warning before Save; back to private hides it", async () => {
    open();
    await ready();
    expect(screen.queryByText("Check your first stop.")).toBeNull();
    fireEvent.click(screen.getByLabelText(/Public:/));
    expect(screen.getByText("Check your first stop.")).toBeTruthy();
    expect(patches).toHaveLength(0);
    fireEvent.click(screen.getByLabelText(/Private:/));
    expect(screen.queryByText("Check your first stop.")).toBeNull();
  });

  test("a public trip shows no publish warning", async () => {
    current = trip({ visibility: "public" });
    open();
    await ready();
    expect(screen.queryByText("Check your first stop.")).toBeNull();
  });

  test("0 hours shows the live-location warning", async () => {
    open();
    await ready();
    type("Hours before stops are public", "0");
    expect(screen.getByText(/With no delay/)).toBeTruthy();
  });

  test("a double tap sends one request", async () => {
    let release!: () => void;
    patchResponse = () => new Promise((r) => (release = () => r(json(200, { ...current, name: "X" }))));
    open();
    await ready();
    type("Trip name", "X");
    const btn = screen.getByRole("button", { name: "Save settings" });
    fireEvent.submit(btn.closest("form")!);
    fireEvent.submit(btn.closest("form")!);
    await settle();
    expect(patches).toHaveLength(1);
    release();
    await screen.findByText("Saved.");
    expect(patches).toHaveLength(1);
  });
});

describe("errors", () => {
  const envelope = (status: number, code: string, message: string, h?: Record<string, string>) => json(status, { error: { code, message } }, h);

  test("403 shows the no-longer-a-leader state", async () => {
    patchResponse = () => envelope(403, "FORBIDDEN", "Leaders only.");
    open();
    await ready();
    type("Trip name", "X");
    save();
    await screen.findByText("You're no longer a leader of this trip.");
    expect(screen.queryByLabelText("Trip name")).toBeNull();
  });

  test("422 shows the envelope message and keeps the form", async () => {
    patchResponse = () => envelope(422, "VALIDATION_ERROR", "Name is too long.");
    open();
    await ready();
    type("Trip name", "X");
    save();
    await screen.findByText("Name is too long.");
    expect(screen.getByLabelText("Trip name")).toBeTruthy();
  });

  test("429 shows the message and countdown and disables Save", async () => {
    patchResponse = () => envelope(429, "RATE_LIMITED", "Slow down.", { "Retry-After": "30" });
    open();
    await ready();
    type("Trip name", "X");
    save();
    await screen.findByText("Slow down.");
    expect(screen.getByText("Try again in 30 seconds.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save settings" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
