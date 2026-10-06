import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";

// t-am-fe-members: /trips/$tripId/members?view=members. Real route
// tree and generated client; only globalThis.fetch is mocked, routed by URL.

const ID = "11111111-1111-4111-8111-111111111111";
const trip = (role?: string): TripOut =>
  ({ id: ID, name: "Stuart Hwy 2026", startDate: "2026-10-12", bikes: [], access: "rider", visibility: "public", publicDelayHours: 0, riderCount: 3, lastPublicStopAt: null, ...(role ? { viewer: { role } } : {}) }) as TripOut;
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (code: string, message = code.toLowerCase()) => ({ error: { code, message } });
const mem = (id: string, name: string, role: string) => ({ userId: id, displayName: name, role, joinedAt: "2026-10-03T08:00:00Z" });

let role: string | undefined;
let members: ReturnType<typeof mem>[];
let override: Record<string, () => Response | Promise<Response>>;
let calls: { method: string; path: string }[];

beforeEach(() => {
  localStorage.clear();
  role = "leader";
  members = [mem("u1", "Wes", "leader"), mem("u2", "Kim", "leader"), mem("u3", "Sam", "rider")];
  override = {};
  calls = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const p = new URL(String(input), "https://testserver").pathname;
    const method = init?.method ?? "GET";
    calls.push({ method, path: p });
    const key = `${method} ${p.replace(`/api/v2/trips/${ID}`, "")}`;
    if (override[key]) return Promise.resolve(override[key]());
    if (p === "/api/v2/auth/me") return Promise.resolve(json(200, { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" }));
    if (p === `/api/v2/trips/${ID}`) return Promise.resolve(json(200, trip(role)));
    if (key === "GET /members") return Promise.resolve(json(200, members));
    if (key === "POST /members/u3/promote") {
      members = members.map((m) => (m.userId === "u3" ? { ...m, role: "leader" } : m));
      return Promise.resolve(json(200, members[2]));
    }
    if (key === "DELETE /members/u3") {
      members = members.filter((m) => m.userId !== "u3");
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (key === "POST /step-down") return Promise.resolve(json(200, mem("u1", "Wes", "rider")));
    if (key === "POST /leave") return Promise.resolve(new Response(null, { status: 204 }));
    return Promise.resolve(json(404, envelope("NOT_FOUND")));
  });
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

let router: ReturnType<typeof createRouter>;
function open() {
  router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [`/trips/${ID}/members?view=members`] }) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}
const writes = () => calls.filter((c) => c.method !== "GET");
const count = (method: string, path: string) => calls.filter((c) => c.method === method && c.path === `/api/v2/trips/${ID}${path}`).length;
const btn = (name: string | RegExp) => screen.findByRole("button", { name });
const dialog = () => screen.getByRole("dialog");

describe("role gating", () => {
  test.each(["pending", "none", "anonymous", undefined])("role %s gets no members list", async (r) => {
    role = r;
    open();
    await screen.findByText("Leaders only");
    expect(count("GET", "/members")).toBe(0);
  });

  test("a pre-extension cached trip (no viewer) loads no members", async () => {
    localStorage.setItem(`btj.trip.${ID}`, JSON.stringify(trip()));
    role = undefined;
    open();
    await screen.findByText("Leaders only");
    expect(count("GET", "/members")).toBe(0);
  });

  test("a rider sees the list read-only, with Leave on their own row only", async () => {
    role = "rider";
    members = [mem("u1", "Wes", "rider"), mem("u2", "Kim", "leader"), mem("u3", "Sam", "rider")];
    open();
    await screen.findByText("Kim");
    expect(screen.queryByRole("button", { name: /Make|Revoke|Step down/ })).toBeNull();
    expect(screen.getAllByRole("button", { name: "Leave trip" })).toHaveLength(1);
    expect(screen.queryByRole("link", { name: "Requests" })).toBeNull();
  });

  test("the shell links a rider to Members", async () => {
    role = "rider";
    router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [`/trips/${ID}`] }) });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("link", { name: "Members" })).toBeTruthy();
  });
});

describe("leader view", () => {
  test("Revoke is absent on leaders and on self; Make leader and Revoke on riders", async () => {
    open();
    await screen.findByText("Sam");
    expect(screen.getAllByRole("button", { name: /^Revoke/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Revoke Sam" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Step down" })).toHaveLength(1);
    expect(screen.getByText("Leaders can't remove each other.")).toBeTruthy();
  });

  test("promote confirms first, then sends once and refetches members and trip", async () => {
    open();
    fireEvent.click(await btn("Make Sam a leader"));
    expect(writes()).toHaveLength(0);
    const tripGets = count("GET", "");
    fireEvent.click(within(dialog()).getByRole("button", { name: "Make leader" }));
    await screen.findByText("Sam is now a leader");
    expect(count("POST", "/members/u3/promote")).toBe(1);
    await waitFor(() => expect(count("GET", "")).toBeGreaterThan(tripGets));
    expect(await screen.findByText("Leaders (3)")).toBeTruthy();
  });

  test("revoke confirms first (Cancel sends nothing), then removes", async () => {
    open();
    fireEvent.click(await btn("Revoke Sam"));
    fireEvent.click(within(dialog()).getByRole("button", { name: "Cancel" }));
    expect(writes()).toHaveLength(0);
    fireEvent.click(await btn("Revoke Sam"));
    fireEvent.click(within(dialog()).getByRole("button", { name: "Remove Sam" }));
    await screen.findByText("Removed Sam");
    expect(count("DELETE", "/members/u3")).toBe(1);
  });

  test("a double tap on the confirm sends one request", async () => {
    let release: (r: Response) => void = () => {};
    override["POST /members/u3/promote"] = () => new Promise<Response>((r) => (release = r));
    open();
    fireEvent.click(await btn("Make Sam a leader"));
    const go = within(dialog()).getByRole("button", { name: "Make leader" });
    fireEvent.click(go);
    fireEvent.click(go);
    await waitFor(() => expect(count("POST", "/members/u3/promote")).toBe(1));
    release(json(200, mem("u3", "Sam", "leader")));
    await screen.findByText("Sam is now a leader");
    expect(count("POST", "/members/u3/promote")).toBe(1);
  });

  test("step down confirms, then shows the rider notice", async () => {
    open();
    fireEvent.click(await btn("Step down"));
    expect(writes()).toHaveLength(0);
    fireEvent.click(within(dialog()).getByRole("button", { name: "Step down" }));
    await screen.findByText("You're now a rider on this trip");
    expect(count("POST", "/step-down")).toBe(1);
  });

  test("leave confirms, then goes home and refetches the trip and my trips", async () => {
    open();
    fireEvent.click(await btn("Leave trip"));
    expect(writes()).toHaveLength(0);
    const tripGets = count("GET", "");
    fireEvent.click(within(dialog()).getByRole("button", { name: "Leave trip" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(count("POST", "/leave")).toBe(1);
    await waitFor(() => expect(count("GET", "")).toBeGreaterThan(tripGets));
    await waitFor(() => expect(calls.some((c) => c.method === "GET" && c.path === "/api/v2/me/trips")).toBe(true));
  });

  test("the sole leader has Step down and Leave disabled with the reason shown", async () => {
    members = [mem("u1", "Wes", "leader"), mem("u3", "Sam", "rider")];
    open();
    await screen.findByText("Sam");
    expect((screen.getByRole("button", { name: "Step down" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Leave trip" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("You're the only leader. Promote another rider to leader first.")).toBeTruthy();
  });

  test("a last-leader 409 (race) shows the server message verbatim in the dialog", async () => {
    override["POST /step-down"] = () => json(409, envelope("CONFLICT", "Promote another rider to leader first"));
    open();
    fireEvent.click(await btn("Step down"));
    fireEvent.click(within(dialog()).getByRole("button", { name: "Step down" }));
    expect(await within(dialog()).findByText("Promote another rider to leader first")).toBeTruthy();
    expect(screen.queryByText("You're now a rider on this trip")).toBeNull();
  });

  test("a 403 on revoke shows the envelope message and refetches the list", async () => {
    override["DELETE /members/u3"] = () => json(403, envelope("FORBIDDEN", "Leaders can't remove another leader"));
    open();
    fireEvent.click(await btn("Revoke Sam"));
    const gets = count("GET", "/members");
    fireEvent.click(within(dialog()).getByRole("button", { name: "Remove Sam" }));
    expect(await screen.findByText("Leaders can't remove another leader")).toBeTruthy();
    await waitFor(() => expect(count("GET", "/members")).toBeGreaterThan(gets));
  });
});
