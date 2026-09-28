import { afterEach, expect, test, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "../routeTree.gen";
import { Timeline } from "./Timeline";
import type { StopOut } from "../api/gen/types/StopOut";
import type { TripOut } from "../api/gen/types/TripOut";

// t-frontend-timeline-feed: chronological stop feed on the trip home.

const s = (id: string, arrivedAt: string, over: Partial<StopOut> = {}): StopOut => ({
  id,
  name: `Stop ${id}`,
  lat: -19.6,
  lng: 134.1,
  locationSource: "gps",
  arrivedAt,
  notes: null,
  ...over,
});
const names = () => screen.getAllByRole("listitem").map((li) => li.querySelector("strong")!.textContent);

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("AC1: sorts by instant, not string — +09:30 10:00 (00:30Z) precedes 01:00Z", () => {
  // String order would put "…T01:00:00Z" first.
  render(<Timeline stops={[s("utc", "2026-06-14T01:00:00Z"), s("acst", "2026-06-14T10:00:00+09:30")]} />);
  expect(names()).toEqual(["Stop acst", "Stop utc"]);
});

test("AC1: equal instants (different offsets) tie-break by id; input order is ignored", () => {
  render(
    <Timeline
      stops={[
        s("c", "2026-06-15T00:00:00Z"),
        s("b", "2026-06-14T11:30:00+09:30"), // == 02:00Z
        s("a", "2026-06-14T02:00:00Z"),
        s("z", "2026-06-13T23:00:00Z"),
      ]}
    />,
  );
  expect(names()).toEqual(["Stop z", "Stop a", "Stop b", "Stop c"]);
});

test("AC1: does not mutate the caller's array", () => {
  const stops = [s("b", "2026-06-15T00:00:00Z"), s("a", "2026-06-14T00:00:00Z")];
  render(<Timeline stops={stops} />);
  expect(stops.map((x) => x.id)).toEqual(["b", "a"]);
});

test("AC2: item shows name, toLocaleString() arrival, and notes when non-null", () => {
  const at = "2026-06-14T10:00:00+09:30";
  render(<Timeline stops={[s("a", at, { name: "Daly Waters Pub", notes: "Cold beer" })]} />);
  const li = screen.getByRole("listitem");
  expect(li.querySelector("strong")!.textContent).toBe("Daly Waters Pub");
  expect(li.textContent).toContain(new Date(at).toLocaleString());
  expect(li.querySelector("p")!.textContent).toBe("Cold beer");
});

test("AC2: null notes render nothing (no empty paragraph, no 'null')", () => {
  render(<Timeline stops={[s("a", "2026-06-14T01:00:00Z")]} />);
  const li = screen.getByRole("listitem");
  expect(li.querySelector("p")).toBeNull();
  expect(li.textContent).not.toContain("null");
});

test("AC2: empty-string notes are still non-null and rendered", () => {
  render(<Timeline stops={[s("a", "2026-06-14T01:00:00Z", { notes: "" })]} />);
  expect(screen.getByRole("listitem").querySelector("p")).not.toBeNull();
});

test("AC3: manual shows 'approximate location'; gps shows nothing extra", () => {
  render(
    <Timeline
      stops={[s("g", "2026-06-14T01:00:00Z"), s("m", "2026-06-14T02:00:00Z", { locationSource: "manual" })]}
    />,
  );
  const [gps, manual] = screen.getAllByRole("listitem");
  expect(manual.textContent).toContain("approximate location");
  expect(gps.textContent).not.toContain("approximate");
});

test("AC4: empty list shows 'No stops yet.'", () => {
  render(<Timeline stops={[]} />);
  expect(screen.getByText("No stops yet.")).toBeTruthy();
  expect(screen.queryByRole("list")).toBeNull();
});

test("AC6: keyed by stop.id — an earlier stop inserted above keeps the existing <li> node", () => {
  const later = s("later", "2026-06-15T00:00:00Z");
  const { rerender } = render(<Timeline stops={[later]} />);
  const before = screen.getByRole("listitem");
  rerender(<Timeline stops={[later, s("earlier", "2026-06-14T00:00:00Z")]} />);
  const [first, second] = screen.getAllByRole("listitem");
  // Index keys would reuse `before` for position 0 ("earlier").
  expect(second).toBe(before);
  expect(first).not.toBe(before);
});

// AC5: through the real route tree, fetch stubbed per path.
const TRIP: TripOut = { id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", bikes: [], access: "viewer" };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function renderHome(stops: () => Promise<Response>) {
  vi.stubGlobal("scrollTo", () => {});
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/stops")) return stops();
    if (url.endsWith("/map")) return json(200, { type: "FeatureCollection", features: [] });
    return json(200, TRIP);
  });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/t/abc"] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

test("AC5: pending /stops shows 'Loading stops…'", async () => {
  renderHome(() => new Promise<Response>(() => {}));
  expect(await screen.findByText("Loading stops…")).toBeTruthy();
});

test("AC5: envelope error shows envelope.error.message", async () => {
  renderHome(async () => json(404, { error: { code: "TRIP_NOT_FOUND", message: "No such trip, mate" } }));
  expect(await screen.findByText("No such trip, mate")).toBeTruthy();
  expect(screen.queryByText("Couldn't load stops")).toBeNull();
});

test.each([
  ["network failure", () => Promise.reject(new TypeError("Failed to fetch"))],
  ["non-JSON 500", async () => new Response("<html>bad gateway</html>", { status: 502 })],
])("AC5: %s (no envelope) shows \"Couldn't load stops\"", async (_, stops) => {
  renderHome(stops);
  expect(await screen.findByText("Couldn't load stops")).toBeTruthy();
});

test("success: /stops data renders as the sorted timeline on the trip home", async () => {
  renderHome(async () => json(200, [s("utc", "2026-06-14T01:00:00Z"), s("acst", "2026-06-14T10:00:00+09:30")]));
  await screen.findByText("Stop acst");
  expect(names()).toEqual(["Stop acst", "Stop utc"]);
  expect(screen.queryByText("Loading stops…")).toBeNull();
});
