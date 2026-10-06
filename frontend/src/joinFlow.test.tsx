import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";

// t-am-fe-join-flow: request / cancel on /trips/$tripId, "My requests" on /account,
// and the legacy rider-link claim on /t/$slug. Real route tree and generated client;
// only globalThis.fetch is mocked, routed by URL.

const ID = "11111111-1111-4111-8111-111111111111";
const SLUG = "rider-slug-secret";
const ME = { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" };
const trip = (role?: string): TripOut =>
  ({
    id: ID,
    name: "Stuart Hwy 2026",
    startDate: "2026-10-12",
    bikes: [],
    access: "viewer",
    visibility: "public",
    publicDelayHours: 0,
    riderCount: 3,
    lastPublicStopAt: null,
    ...(role ? { viewer: { role } } : {}),
  }) as TripOut;
const REQ = { id: "r1", tripId: ID, tripName: "Stuart Hwy 2026", state: "pending", message: "hi", createdAt: "2026-10-05T00:00:00Z" };

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (code: string, message = code.toLowerCase()) => ({ error: { code, message } });

type Handler = (url: URL, init?: RequestInit) => Promise<Response> | undefined;
let handler: Handler;
const posts: { path: string; body: string }[] = [];
let tripRole: string | undefined;
let signedIn: boolean;
let requests: unknown[];

function serve(over: Handler = () => undefined) {
  handler = (url, init) => {
    const hit = over(url, init);
    if (hit) return hit;
    const p = url.pathname;
    if (p === "/api/v2/auth/me") return Promise.resolve(signedIn ? json(200, ME) : json(401, envelope("UNAUTHENTICATED")));
    if (p === "/api/v2/me/join-requests") return Promise.resolve(json(200, requests));
    if (p === `/api/v2/trips/${ID}` || p === `/api/trips/${SLUG}`) return Promise.resolve(json(200, trip(tripRole)));
    if (p.endsWith("/map")) return Promise.resolve(json(200, { type: "FeatureCollection", features: [] }));
    if (p.endsWith("/stops")) return Promise.resolve(json(200, []));
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
  posts.length = 0;
  tripRole = "none";
  signedIn = true;
  requests = [];
  serve();
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "https://testserver");
    if (init?.method === "POST") posts.push({ path: url.pathname, body: String(init.body ?? "") });
    return handler(url, init)!;
  });
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const sendButton = () => screen.findByRole("button", { name: "Send request" });
const typeMessage = (v: string) => fireEvent.change(screen.getByLabelText("Message to the leaders (optional)"), { target: { value: v } });

describe("request on /trips/$tripId", () => {
  test("anonymous sees a sign-in link carrying next", async () => {
    tripRole = "anonymous";
    signedIn = false;
    renderAt(`/trips/${ID}`);
    const link = await screen.findByRole("link", { name: "Sign in to ask to join" });
    expect(link.getAttribute("href")).toBe(`/signin?next=${encodeURIComponent(`/trips/${ID}`)}`);
  });

  test.each(["rider", "leader", "pending"])("role %s gets no Send request", async (role) => {
    tripRole = role;
    renderAt(`/trips/${ID}`);
    await screen.findByText("Stuart Hwy 2026", { selector: "h1" });
    await settle();
    expect(screen.queryByRole("button", { name: "Send request" })).toBeNull();
  });

  test("a pre-extension cached trip with no role gets no request UI", async () => {
    tripRole = undefined;
    renderAt(`/trips/${ID}`);
    await screen.findByText("Stuart Hwy 2026", { selector: "h1" });
    await settle();
    expect(screen.queryByRole("button", { name: "Send request" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Sign in to ask to join" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel request" })).toBeNull();
  });

  test("counter is live and stops at 280; the message is trimmed on send", async () => {
    renderAt(`/trips/${ID}`);
    await sendButton();
    expect(screen.getByText("0 / 280")).toBeTruthy();
    typeMessage("a".repeat(300));
    expect(screen.getByText("280 / 280")).toBeTruthy();
    typeMessage("  hello  ");
    expect(screen.getByText("9 / 280")).toBeTruthy();
    serve((url) => (url.pathname === `/api/v2/trips/${ID}/join-requests` ? Promise.resolve(json(201, REQ)) : undefined));
    fireEvent.click(screen.getByRole("button", { name: "Send request" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(JSON.parse(posts[0].body)).toEqual({ message: "hello" });
  });

  test("after a request the trip refetches to pending and the button becomes Cancel", async () => {
    renderAt(`/trips/${ID}`);
    await sendButton();
    serve((url) => {
      if (url.pathname === `/api/v2/trips/${ID}/join-requests`) {
        tripRole = "pending";
        requests = [REQ];
        return Promise.resolve(json(201, REQ));
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "Send request" }));
    const cancel = await screen.findByRole("button", { name: "Cancel request" });
    expect(screen.queryByRole("button", { name: "Send request" })).toBeNull();
    serve((url) => {
      if (url.pathname === "/api/v2/join-requests/r1/cancel") {
        tripRole = "none";
        requests = [];
        return Promise.resolve(json(200, { ...REQ, state: "cancelled" }));
      }
    });
    fireEvent.click(cancel);
    expect(await screen.findByText("Request cancelled")).toBeTruthy();
    expect(await sendButton()).toBeTruthy();
    expect(posts.at(-1)?.path).toBe("/api/v2/join-requests/r1/cancel");
  });

  test("a double tap sends one request", async () => {
    renderAt(`/trips/${ID}`);
    await sendButton();
    serve((url) => (url.pathname === `/api/v2/trips/${ID}/join-requests` ? new Promise(() => {}) : undefined));
    const form = screen.getByRole("button", { name: "Send request" }).closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    await settle();
    expect(posts).toHaveLength(1);
  });

  test.each(["cooldown message one", "You can't ask again yet.", "Too many pending requests."])("409 shows %s verbatim and hides the form", async (message) => {
    renderAt(`/trips/${ID}`);
    await sendButton();
    typeMessage("hello");
    serve((url) => (url.pathname === `/api/v2/trips/${ID}/join-requests` ? Promise.resolve(json(409, envelope("CONFLICT", message))) : undefined));
    fireEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send request" })).toBeNull();
  });

  test("422 shows the envelope message under the field", async () => {
    renderAt(`/trips/${ID}`);
    await sendButton();
    serve((url) => (url.pathname === `/api/v2/trips/${ID}/join-requests` ? Promise.resolve(json(422, envelope("VALIDATION_ERROR", "message is too long"))) : undefined));
    fireEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByText("message is too long")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Send request" })).toBeTruthy();
  });

  test("429 shows the countdown and disables Send", async () => {
    renderAt(`/trips/${ID}`);
    await sendButton();
    serve((url) => (url.pathname === `/api/v2/trips/${ID}/join-requests` ? Promise.resolve(json(429, envelope("RATE_LIMITED"), { "Retry-After": "120" })) : undefined));
    fireEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByText("Try again in 2 minutes.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Send request" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("My requests on /account", () => {
  test("lists requests, Cancel only on pending; rejected reads Not approved", async () => {
    requests = [
      REQ,
      { ...REQ, id: "r2", tripName: "Old trip", state: "rejected" },
      { ...REQ, id: "r3", tripName: "Blocked trip", state: "blocked" },
    ];
    renderAt("/account");
    expect(await screen.findByText("Old trip")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Cancel request" })).toHaveLength(1);
    expect(screen.getAllByText("Not approved")).toHaveLength(2);
    expect(screen.queryByText(/blocked$/i)).toBeNull();
  });

  test("Cancel posts once, refetches; a 409 shows the envelope message on the row", async () => {
    requests = [REQ];
    renderAt("/account");
    const cancel = await screen.findByRole("button", { name: "Cancel request" });
    serve((url) => (url.pathname === "/api/v2/join-requests/r1/cancel" ? Promise.resolve(json(409, envelope("CONFLICT", "A leader already decided."))) : undefined));
    fireEvent.click(cancel);
    fireEvent.click(cancel);
    expect(await screen.findByText("A leader already decided.")).toBeTruthy();
    expect(posts.filter((p) => p.path.endsWith("/cancel"))).toHaveLength(1);
  });
});

describe("claim on /t/$slug", () => {
  const claim = (res: () => Response) =>
    serve((url) => (url.pathname === "/api/v2/trips/claim" ? Promise.resolve(res()) : undefined));

  test("signed-out and member see no claim button", async () => {
    signedIn = false;
    renderAt(`/t/${SLUG}`);
    await screen.findByText("Stuart Hwy 2026");
    await settle();
    expect(screen.queryByRole("button", { name: "Ask to join as a rider" })).toBeNull();
    cleanup();
    signedIn = true;
    tripRole = "rider";
    renderAt(`/t/${SLUG}`);
    await screen.findByText("Stuart Hwy 2026");
    await settle();
    expect(screen.queryByRole("button", { name: "Ask to join as a rider" })).toBeNull();
  });

  test.each([201, 200])("claim %i posts the slug in the body and shows pending", async (code) => {
    renderAt(`/t/${SLUG}`);
    const btn = await screen.findByRole("button", { name: "Ask to join as a rider" });
    claim(() => {
      tripRole = "pending";
      requests = [REQ];
      return json(code, REQ);
    });
    fireEvent.click(btn);
    expect(await screen.findByText("Request sent. A leader of this trip will review it.")).toBeTruthy();
    expect(JSON.parse(posts[0].body)).toEqual({ riderSlug: SLUG });
    expect(posts[0].path).toBe("/api/v2/trips/claim");
    expect(screen.getByRole("button", { name: "Cancel request" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Ask to join as a rider" })).toBeNull();
  });

  test("claim 404 shows 'This link can't be used to join'", async () => {
    renderAt(`/t/${SLUG}`);
    const btn = await screen.findByRole("button", { name: "Ask to join as a rider" });
    claim(() => json(404, envelope("NOT_FOUND")));
    fireEvent.click(btn);
    expect(await screen.findByText("This link can't be used to join")).toBeTruthy();
    expect(document.body.textContent).not.toContain("viewer link");
  });

  test("claim 409 shows the envelope message verbatim", async () => {
    renderAt(`/t/${SLUG}`);
    const btn = await screen.findByRole("button", { name: "Ask to join as a rider" });
    claim(() => json(409, envelope("CONFLICT", "Wait a few days.")));
    fireEvent.click(btn);
    expect(await screen.findByText("Wait a few days.")).toBeTruthy();
  });
});

const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));
