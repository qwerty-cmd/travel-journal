import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";

// t-am-fe-leader-review: /trips/$tripId/members (requests + blocked views). Real route
// tree and generated client; only globalThis.fetch is mocked, routed by URL.

const ID = "11111111-1111-4111-8111-111111111111";
const trip = (role?: string): TripOut =>
  ({ id: ID, name: "Stuart Hwy 2026", startDate: "2026-10-12", bikes: [], access: "rider", visibility: "public", publicDelayHours: 0, riderCount: 3, lastPublicStopAt: null, ...(role ? { viewer: { role } } : {}) }) as TripOut;
const req = (id: string, name: string, extra: object = {}) => ({
  id,
  requester: { userId: `user-${id}`, displayName: name },
  state: "pending",
  via: "direct",
  message: null,
  createdAt: new Date(Date.now() - 2 * 3600_000).toISOString(),
  ...extra,
});
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (code: string, message = code.toLowerCase()) => ({ error: { code, message } });

let role: string | undefined;
let pending: ReturnType<typeof req>[];
let blockedList: ReturnType<typeof req>[];
let decisions: Record<string, (id: string, action: string) => Response | Promise<Response>>;
let calls: { method: string; path: string; body: string }[];

beforeEach(() => {
  localStorage.clear();
  role = "leader";
  pending = [req("a", "Sam", { message: "Riding the Hilux", via: "legacy_rider_link" }), req("b", "Alex"), req("c", "Jo")];
  blockedList = [req("z", "Blocky", { state: "blocked" })];
  decisions = {};
  calls = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "https://testserver");
    const p = url.pathname;
    const method = init?.method ?? "GET";
    calls.push({ method, path: p, body: String(init?.body ?? "") });
    if (p === "/api/v2/auth/me") return Promise.resolve(json(200, { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" }));
    if (p === `/api/v2/trips/${ID}`) return Promise.resolve(json(200, trip(role)));
    if (p === `/api/v2/trips/${ID}/join-requests`) return Promise.resolve(json(200, url.searchParams.get("state") === "blocked" ? blockedList : pending));
    const m = p.match(/join-requests\/([^/]+)\/(decision|unblock)$/);
    if (m && method === "POST") {
      if (m[2] === "unblock") return Promise.resolve(decisions[m[1]]?.(m[1], "unblock") ?? json(200, {}));
      const action = JSON.parse(String(init?.body)).action as string;
      const out = decisions[m[1]]?.(m[1], action) ?? json(200, {});
      if (out instanceof Response && out.ok) pending = pending.filter((r) => r.id !== m[1]);
      return Promise.resolve(out);
    }
    return Promise.resolve(json(404, envelope("NOT_FOUND")));
  });
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}
const open = (q = "") => renderAt(`/trips/${ID}/members${q}`);
const posts = () => calls.filter((c) => c.method === "POST");
const listCalls = () => calls.filter((c) => c.path === `/api/v2/trips/${ID}/join-requests`);

describe("role gate", () => {
  test.each(["rider", "pending", "none", "anonymous", undefined])("role %s sees Leaders only and loads nothing", async (r) => {
    role = r;
    open();
    await screen.findByText("Leaders only");
    expect(listCalls()).toHaveLength(0);
    expect(screen.queryByRole("link", { name: "Requests" })).toBeNull();
  });

  test("a pre-extension cached trip (no viewer) gets the gate", async () => {
    localStorage.setItem(`btj.trip.${ID}`, JSON.stringify(trip()));
    role = undefined;
    open();
    await screen.findByText("Leaders only");
    expect(listCalls()).toHaveLength(0);
  });

  test("the trip shell shows a Requests link to leaders only", async () => {
    renderAt(`/trips/${ID}`);
    const link = await screen.findByRole("link", { name: "Requests" });
    expect(link.getAttribute("href")).toBe(`/trips/${ID}/members?view=requests`);
    cleanup();
    role = "rider";
    renderAt(`/trips/${ID}`);
    await screen.findByText("Stuart Hwy 2026", { selector: "h1" });
    await waitFor(() => expect(screen.queryByRole("link", { name: "Requests" })).toBeNull());
  });
});

describe("pending list", () => {
  test("shows display names, message, legacy badge; no ids or usernames in the DOM", async () => {
    open();
    await screen.findByText("Sam");
    expect(screen.getByText("Riding the Hilux")).toBeTruthy();
    expect(screen.getByText("Via old rider link")).toBeTruthy();
    expect(screen.getAllByText(/Requested .*ago/)).toHaveLength(3);
    expect(document.body.innerHTML).not.toMatch(/user-a|wes/);
  });

  test("Approve sends one decision, echoes it, and refetches list and trip", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Approve Sam" }));
    await screen.findByText("Approved Sam");
    expect(posts()).toEqual([{ method: "POST", path: `/api/v2/trips/${ID}/join-requests/a/decision`, body: JSON.stringify({ action: "approve" }) }]);
    expect(screen.queryByText("Riding the Hilux")).toBeNull();
    await waitFor(() => expect(listCalls().length).toBeGreaterThan(1));
    expect(calls.filter((c) => c.path === `/api/v2/trips/${ID}`).length).toBeGreaterThan(1);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Approve Alex" })));
  });

  test("Reject sends reject with no confirm", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Reject Alex" }));
    await screen.findByText("Rejected Alex");
    expect(JSON.parse(posts()[0].body)).toEqual({ action: "reject" });
  });

  test("Reject and block asks first; cancel sends nothing, confirm sends reject_and_block", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "More options for Jo" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject and block" }));
    const dialog = await screen.findByText("Reject and block Jo?");
    expect(posts()).toHaveLength(0);
    fireEvent.click(within(dialog.closest(".dialog__card") as HTMLElement).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Reject and block Jo?")).toBeNull());
    expect(posts()).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "More options for Jo" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject and block" }));
    const card = (await screen.findByText("Reject and block Jo?")).closest(".dialog__card") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Reject and block" }));
    await screen.findByText("Blocked Jo");
    expect(posts()).toHaveLength(1);
    expect(JSON.parse(posts()[0].body)).toEqual({ action: "reject_and_block" });
  });

  test("a double tap on Approve sends one request", async () => {
    let release: (r: Response) => void = () => {};
    decisions.a = () => new Promise<Response>((res) => (release = res));
    open();
    const btn = await screen.findByRole("button", { name: "Approve Sam" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(posts()).toHaveLength(1));
    release(json(200, {}));
    await screen.findByText("Approved Sam");
    expect(posts()).toHaveLength(1);
  });

  test("a 409 on a single row shows Already handled with the envelope message", async () => {
    decisions.b = () => json(409, envelope("CONFLICT", "Already rejected by another leader"));
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Approve Alex" }));
    await screen.findByText(/Already handled\. Already rejected by another leader/);
  });
});

describe("multi-select", () => {
  test("bulk approve with mixed outcomes: one request each, failures reported and kept selected", async () => {
    decisions.b = () => json(409, envelope("CONFLICT", "Already rejected"));
    open();
    await screen.findByText("Sam");
    fireEvent.click(screen.getByLabelText("Select all"));
    expect(screen.getByText("3 selected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve 3" }));
    await screen.findByText("Approved 2 of 3. Couldn't approve Alex: Already rejected");
    expect(posts().map((p) => p.path.split("/").slice(-2, -1)[0])).toEqual(["a", "b", "c"]);
    expect(screen.getByText("1 selected")).toBeTruthy();
    expect(screen.queryByLabelText("Select Alex")).toBeNull();
  });

  test("a 429 stops the loop and leaves the rest selected", async () => {
    decisions.a = () => json(429, envelope("RATE_LIMITED", "slow down"), { "Retry-After": "30" });
    open();
    await screen.findByText("Sam");
    fireEvent.click(screen.getByLabelText("Select all"));
    fireEvent.click(screen.getByRole("button", { name: "Reject 3" }));
    await screen.findByText("Too many requests.");
    expect(posts()).toHaveLength(1);
    expect(screen.getByText("3 selected")).toBeTruthy();
  });
});

describe("blocked view", () => {
  test("lists blocked requests and Unblock sends one request with the 7-day toast", async () => {
    open("?view=blocked");
    await screen.findByText("Blocky");
    expect(listCalls()[0].path).toBe(`/api/v2/trips/${ID}/join-requests`);
    fireEvent.click(screen.getByRole("button", { name: "Unblock Blocky" }));
    await screen.findByText(/Unblocked Blocky\. They can ask to join again once 7 days have passed since the original decision\./);
    expect(posts()).toEqual([{ method: "POST", path: `/api/v2/trips/${ID}/join-requests/z/unblock`, body: "" }]);
  });

  test("Unblock 409 shows Already handled", async () => {
    decisions.z = () => json(409, envelope("CONFLICT", "Not blocked"));
    open("?view=blocked");
    fireEvent.click(await screen.findByRole("button", { name: "Unblock Blocky" }));
    await screen.findByText(/Already handled\. Not blocked/);
  });

  test("empty blocked list", async () => {
    blockedList = [];
    open("?view=blocked");
    await screen.findByText("Nobody is blocked.");
  });
});
