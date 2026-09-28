import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";

// t-frontend-display-name-prompt: riders without a stored name are asked for
// one before the trip home renders; viewers never are (decision-log Entry 18).
// Driven through the real route tree, with only globalThis.fetch mocked.

const fetchMock = vi.fn<typeof fetch>();
const TRIP: TripOut = { id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", bikes: [], access: "rider" };
const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

function renderTrip(access: TripOut["access"]) {
  fetchMock.mockImplementation(async () => json({ ...TRIP, access }));
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/t/abc"] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

const heading = () => screen.findByRole("heading", { name: TRIP.name });
const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));
const nameInput = () => screen.queryByLabelText("Your name");
const tripHome = () => screen.queryByRole("main"); // t.$slug.index.tsx renders <main>

function submit(value: string) {
  fireEvent.change(nameInput()!, { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("scrollTo", () => {});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("AC1: rider with no name sees the prompt, not the trip home", async () => {
  renderTrip("rider");
  await heading();
  expect(nameInput()).toBeTruthy();
  expect(tripHome()).toBeNull();
});

test("AC2: a padded name is stored trimmed and the trip home renders without reload", async () => {
  renderTrip("rider");
  await heading();
  submit("  Wes  ");
  expect(localStorage.getItem("btj.displayName")).toBe("Wes");
  expect(await screen.findByRole("main")).toBeTruthy();
  expect(nameInput()).toBeNull();
  expect(fetchMock).toHaveBeenCalledTimes(1); // no refetch/reload needed
});

describe("AC3: empty or whitespace is rejected inline", () => {
  test.each(["", "   "])("%j: alert shown, nothing stored, prompt stays", async (value) => {
    renderTrip("rider");
    await heading();
    submit(value);
    expect((await screen.findByRole("alert")).textContent).toBe("Please enter your name.");
    expect(localStorage.getItem("btj.displayName")).toBeNull();
    expect(nameInput()).toBeTruthy();
    expect(tripHome()).toBeNull();
  });
});

test("AC4: viewer is never prompted", async () => {
  renderTrip("viewer");
  await heading();
  await settle();
  expect(nameInput()).toBeNull();
  expect(tripHome()).toBeTruthy();
});

test("AC5: rider with a stored name is not prompted", async () => {
  localStorage.setItem("btj.displayName", "Wes");
  renderTrip("rider");
  await heading();
  await settle();
  expect(nameInput()).toBeNull();
  expect(tripHome()).toBeTruthy();
});

test("access comes from the server: cached rider trip, fresh viewer response -> no prompt", async () => {
  localStorage.setItem("btj.trip.abc", JSON.stringify(TRIP)); // stale cache says rider
  renderTrip("viewer");
  await heading();
  await settle();
  expect(nameInput()).toBeNull();
  expect(tripHome()).toBeTruthy();
});
