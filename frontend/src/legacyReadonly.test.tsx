import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";

// t-am-fe-legacy-readonly: /t/$slug is read-only (legacy-link.md). Real route tree, fetch stubbed.
// Queue drain of already-stored legacy items to legacy paths: offline/queueUserId.test.tsx
// ("slug entries without userId to the legacy ones") and offline/queue.test.tsx (POST /api/trips/abc/stops).
vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));

const TRIP = (access: "rider" | "viewer", role: string): TripOut =>
  ({
    id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", access, visibility: "private", publicDelayHours: 0,
    riderCount: 1, lastPublicStopAt: null, viewer: { role },
    bikes: [{ id: "b1", riderName: "Zoe", make: "Honda", model: "X", year: 2019, specs: "" }],
  }) as TripOut;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

let trip: TripOut;
const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const url = String(input);
  if (url === "/api/trips/abc") return json(200, trip);
  if (url === "/api/trips/abc/stops") return json(200, []);
  if (url === "/api/trips/abc/map") return json(200, { type: "FeatureCollection", features: [] });
  if (url === "/api/v2/auth/me") return json(401, { error: { code: "UNAUTHENTICATED", message: "no" } });
  throw new Error(`unstubbed fetch ${url}`);
});

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

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ROLES = ["anonymous", "none", "pending", "rider", "leader"];

describe("no write entry points, any access x role", () => {
  for (const access of ["rider", "viewer"] as const) {
    for (const role of ROLES) {
      test(`access=${access} role=${role}: no Add stop, no Edit bike / Add bike / name prompt`, async () => {
        trip = TRIP(access, role);
        renderAt("/t/abc");
        await screen.findByRole("link", { name: "Bikes" });
        await settle();
        expect(screen.queryByRole("link", { name: "Add stop" })).toBeNull();
        expect(screen.queryByLabelText(/your name/i)).toBeNull();
        expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
        fireEvent.click(screen.getByRole("link", { name: "Bikes" }));
        await screen.findByRole("heading", { level: 2, name: "Bikes" });
        expect(screen.queryByRole("button", { name: /edit|add bike/i })).toBeNull();
        expect(document.querySelectorAll("main form, main input, main textarea")).toHaveLength(0);
      });
    }
  }
});

describe("/t/$slug/add no longer offers a form", () => {
  test("member is redirected to /trips/$tripId/add", async () => {
    trip = TRIP("rider", "rider");
    const router = renderAt("/t/abc/add");
    await waitFor(() => expect(router.state.location.pathname).toBe("/trips/t1/add"));
  });
  test("signed-out viewer lands on /t/abc with the notice, no form", async () => {
    trip = TRIP("viewer", "anonymous");
    const router = renderAt("/t/abc/add");
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
    expect(await screen.findByText("This is an old trip link")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save stop" })).toBeNull();
  });
});

describe("notice variants", () => {
  test("signed out: copy, Sign in with next, Create an account", async () => {
    trip = TRIP("viewer", "anonymous");
    renderAt("/t/abc");
    expect(await screen.findByText("This is an old trip link")).toBeTruthy();
    expect(screen.getByText("Viewing still works. To add stops and photos, you now need an account and a leader's approval.")).toBeTruthy();
    const signIn = screen.getByRole("link", { name: "Sign in" });
    expect(decodeURIComponent(signIn.getAttribute("href")!)).toContain("next=/t/abc");
    expect(screen.getByRole("link", { name: "Create an account" }).getAttribute("href")).toBe("/signup");
    expect(screen.queryByRole("link", { name: "Open trip" })).toBeNull();
  });

  test.each(["rider", "leader"])("member (%s): Open trip -> /trips/t1", async (role) => {
    trip = TRIP("rider", role);
    renderAt("/t/abc");
    expect(await screen.findByText("You're a member of this trip. Open it in the new view to add stops.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open trip" }).getAttribute("href")).toBe("/trips/t1");
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull();
  });
});
