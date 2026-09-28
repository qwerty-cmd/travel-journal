import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { BikeOut } from "./api/gen/types/BikeOut";
import type { TripOut } from "./api/gen/types/TripOut";

// The root route's <QueueNotice/> reads IndexedDB; not the bikes path.
vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));

// t-bikes-page-list: /t/$slug/bikes, read-only list off the shell's trip query,
// plus the "Bikes" link on the trip home. Real route tree, fetch stubbed per path.

const bike = (id: string, riderName: string, specs = ""): BikeOut => ({
  id,
  riderName,
  make: "Honda",
  model: `Model-${id}`,
  year: 2019,
  specs,
});
// Deliberately out of riderName order: TripOut.bikes order is not contract.
const BIKES = [bike("b1", "Zoe", "Engine: 1100cc\nTyres: TKC80"), bike("b2", "alex"), bike("b3", "Mia", "Stock")];
const TRIP: TripOut = { id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", bikes: BIKES, access: "viewer" };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Routes = Record<string, () => Response | Promise<Response>>;
let routes: Routes;
const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const handler = routes[String(input)];
  if (!handler) throw new Error(`unstubbed fetch ${String(input)}`);
  return handler();
});

function stub(trip: TripOut = TRIP) {
  routes = {
    "/api/trips/abc": () => json(200, trip),
    "/api/trips/abc/stops": () => json(200, []),
    "/api/trips/abc/map": () => json(200, { type: "FeatureCollection", features: [] }),
  };
}

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
const tripRequests = () => fetchMock.mock.calls.filter(([i]) => String(i) === "/api/trips/abc").length;

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  fetchMock.mockClear();
  stub();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AC1: inside the trip shell", () => {
  test("header renders above the bikes list, one trip request", async () => {
    renderAt("/t/abc/bikes");
    expect(await screen.findByRole("heading", { level: 1, name: TRIP.name })).toBeTruthy();
    expect(await screen.findByRole("heading", { level: 2, name: "Bikes" })).toBeTruthy();
    await settle();
    expect(tripRequests()).toBe(1); // refetchOnMount:false — no second request
  });

  test("rider without a display name gets the prompt, not the list", async () => {
    stub({ ...TRIP, access: "rider" });
    renderAt("/t/abc/bikes");
    expect(await screen.findByRole("heading", { level: 1, name: TRIP.name })).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Save" })).toBeTruthy(); // DisplayNamePrompt
    expect(screen.queryByRole("heading", { level: 2, name: "Bikes" })).toBeNull();
  });
});

describe("AC2/AC3: bike cards", () => {
  test("sorted by riderName (localeCompare), each with make/model/year", async () => {
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 2, name: "Bikes" });
    const names = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(names).toEqual(["alex", "Mia", "Zoe"]); // localeCompare, not code-point (which puts "alex" last)
    for (const b of BIKES) {
      const card = screen.getByRole("heading", { level: 3, name: b.riderName }).closest("li")!;
      expect(within(card).getByText(`${b.make} ${b.model} (${b.year})`)).toBeTruthy();
    }
  });

  test("specs keep line breaks via pre-wrap", async () => {
    renderAt("/t/abc/bikes");
    const card = (await screen.findByRole("heading", { level: 3, name: "Zoe" })).closest("li")!;
    const specs = within(card).getByText((_, el) => el?.textContent === BIKES[0].specs && el.tagName === "P");
    expect(specs.textContent).toContain("\n");
    expect(specs.style.whiteSpace).toBe("pre-wrap");
  });

  test('"" specs render no specs block', async () => {
    renderAt("/t/abc/bikes");
    const card = (await screen.findByRole("heading", { level: 3, name: "alex" })).closest("li")!;
    expect(card.querySelectorAll("p")).toHaveLength(1); // make/model/year only
    const withSpecs = screen.getByRole("heading", { level: 3, name: "Mia" }).closest("li")!;
    expect(withSpecs.querySelectorAll("p")).toHaveLength(2);
  });
});

test("AC4: bikes: [] → No bikes yet", async () => {
  stub({ ...TRIP, bikes: [] });
  renderAt("/t/abc/bikes");
  expect(await screen.findByText("No bikes yet")).toBeTruthy();
  expect(screen.queryByRole("list")).toBeNull();
});

describe("AC5: same list for rider and viewer, no write controls", () => {
  test.each(["rider", "viewer"] as const)("%s", async (access) => {
    localStorage.setItem("btj.displayName", "Wes");
    stub({ ...TRIP, access });
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 2, name: "Bikes" });
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["alex", "Mia", "Zoe"]);
    const main = screen.getByRole("main");
    expect(within(main).queryAllByRole("button")).toHaveLength(0);
    expect(within(main).queryAllByRole("textbox")).toHaveLength(0);
    expect(main.querySelectorAll("form, input, textarea, select")).toHaveLength(0);
    expect(within(main).getAllByRole("link").map((a) => a.textContent)).toEqual(["Back to trip"]);
  });
});

describe("AC6: trip home links", () => {
  test.each([
    ["rider", true],
    ["viewer", false],
  ] as const)("%s: Bikes link present, Add stop %s", async (access, hasAdd) => {
    localStorage.setItem("btj.displayName", "Wes");
    stub({ ...TRIP, access });
    const router = renderAt("/t/abc");
    const link = await screen.findByRole("link", { name: "Bikes" });
    expect(link.getAttribute("href")).toBe("/t/abc/bikes");
    expect(screen.queryByRole("link", { name: "Add stop" }) !== null).toBe(hasAdd);
    fireEvent.click(link);
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc/bikes"));
    expect(await screen.findByRole("heading", { level: 2, name: "Bikes" })).toBeTruthy();
  });
});

test("AC7: back link returns to /t/$slug", async () => {
  const router = renderAt("/t/abc/bikes");
  const back = await screen.findByRole("link", { name: "Back to trip" });
  expect(back.getAttribute("href")).toBe("/t/abc");
  fireEvent.click(back);
  await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
});

test("AC8: cold open offline renders the persisted trip's bikes", async () => {
  localStorage.setItem("btj.trip.abc", JSON.stringify(TRIP));
  const down = () => Promise.reject(new TypeError("Failed to fetch"));
  routes = { "/api/trips/abc": down, "/api/trips/abc/stops": down, "/api/trips/abc/map": down };
  renderAt("/t/abc/bikes");
  expect(await screen.findByRole("heading", { level: 3, name: "Zoe" })).toBeTruthy();
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await settle();
  expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["alex", "Mia", "Zoe"]);
  expect(screen.queryByText("Can't reach the server")).toBeNull();
});
