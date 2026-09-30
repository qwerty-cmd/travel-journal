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
const TRIP: TripOut = { id: "t1", name: "Stuart Hwy 2026", startDate: "2026-10-01", bikes: BIKES, access: "viewer", visibility: "private", publicDelayHours: 24, riderCount: 1, lastPublicStopAt: null, viewer: { role: "none" } };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Routes = Record<string, (init?: RequestInit) => Response | Promise<Response>>;
let routes: Routes;
const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const handler = routes[String(input)];
  if (!handler) throw new Error(`unstubbed fetch ${String(input)}`);
  return handler(init);
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

describe("AC5: same list for rider and viewer", () => {
  test.each(["rider", "viewer"] as const)("%s", async (access) => {
    localStorage.setItem("btj.displayName", "Wes");
    stub({ ...TRIP, access });
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 2, name: "Bikes" });
    const cards = screen.getAllByRole("listitem").map((li) => within(li).getByRole("heading", { level: 3 }).textContent);
    expect(cards).toEqual(["alex", "Mia", "Zoe"]);
    expect(within(screen.getByRole("main")).getAllByRole("link").map((a) => a.textContent)).toEqual(["Back to trip"]);
  });

  // t-bikes-page-edit AC1: write UI gated on trip.access alone.
  test("viewer: no write UI at all", async () => {
    localStorage.setItem("btj.displayName", "Wes");
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 2, name: "Bikes" });
    const main = screen.getByRole("main");
    expect(within(main).queryAllByRole("button")).toHaveLength(0);
    expect(within(main).queryAllByRole("textbox")).toHaveLength(0);
    expect(main.querySelectorAll("form, input, textarea, select")).toHaveLength(0);
    expect(within(main).queryByRole("heading", { name: "Add bike" })).toBeNull();
  });

  test("rider: Add bike form plus one Edit per bike", async () => {
    localStorage.setItem("btj.displayName", "Wes");
    stub({ ...TRIP, access: "rider" });
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 2, name: "Bikes" });
    expect(screen.getByRole("heading", { level: 3, name: "Add bike" })).toBeTruthy();
    for (const li of screen.getAllByRole("listitem")) expect(within(li).getByRole("button", { name: "Edit" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Add bike" })).toBeTruthy();
  });
});

// ---- t-bikes-page-edit: rider writes (online-only direct mutations) ----

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const OFFLINE = "You're offline — try again when connected.";
const GENERIC = "Couldn't save the bike — try again.";
const envelope = (status: number, code: string, message: string) => json(status, { error: { code, message } });
const writes = (url: string) => fetchMock.mock.calls.filter(([i]) => String(i) === url);
const bodyOf = (call: unknown[]) => JSON.parse((call[1] as RequestInit).body as string);
const bikeWrites = () => fetchMock.mock.calls.filter(([i]) => String(i).startsWith("/api/trips/abc/bikes"));

let trip: TripOut;
function riderSetup() {
  localStorage.setItem("btj.displayName", "Wes");
  trip = { ...TRIP, access: "rider", bikes: [...BIKES] };
  stub();
  routes["/api/trips/abc"] = () => json(200, trip); // mutable: a refetch sees server-side changes
}
const addForm = () => screen.getByRole("button", { name: "Add bike" }).closest("form")!;
const set = (form: HTMLElement, label: string, value: string) =>
  fireEvent.change(within(form).getByLabelText(label), { target: { value } });
const input = (form: HTMLElement, label: string) => (within(form).getByLabelText(label) as HTMLInputElement).value;
function fillAdd(form: HTMLElement) {
  set(form, "Rider name", " Kai ");
  set(form, "Make", "KTM");
  set(form, "Model", "890 Adventure");
  set(form, "Year", "2022");
  set(form, "Specs", "Rally seat");
}
async function openEdit(riderName: string) {
  const li = (await screen.findByRole("heading", { level: 3, name: riderName })).closest("li")!;
  fireEvent.click(within(li).getByRole("button", { name: "Edit" }));
  return within(li).getByRole("button", { name: "Save bike" }).closest("form")!;
}

describe("t-bikes-page-edit AC2: validation and pending state", () => {
  beforeEach(riderSetup);

  test("required text fields and an integer year gate submit; specs optional", async () => {
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 3, name: "Add bike" });
    const form = addForm();
    const submit = within(form).getByRole("button", { name: "Add bike" }) as HTMLButtonElement;
    expect((within(form).getByLabelText("Year") as HTMLInputElement).type).toBe("number");
    expect(within(form).getByLabelText("Specs").tagName).toBe("TEXTAREA");
    expect(submit.disabled).toBe(true);
    fillAdd(form);
    set(form, "Specs", "");
    expect(submit.disabled).toBe(false); // specs optional
    set(form, "Year", "2019.5");
    expect(submit.disabled).toBe(true);
    set(form, "Year", "2022");
    for (const label of ["Rider name", "Make", "Model"]) {
      set(form, label, "   ");
      expect(submit.disabled).toBe(true);
      set(form, label, "x");
    }
    expect(submit.disabled).toBe(false);
  });

  test("submit disabled while the request is pending; a second submit sends nothing", async () => {
    let release!: (r: Response) => void;
    routes["/api/trips/abc/bikes"] = () => new Promise<Response>((r) => (release = r));
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 3, name: "Add bike" });
    const form = addForm();
    fillAdd(form);
    const submit = within(form).getByRole("button", { name: "Add bike" }) as HTMLButtonElement;
    fireEvent.click(submit);
    await waitFor(() => expect(submit.disabled).toBe(true));
    fireEvent.submit(form);
    await settle();
    expect(writes("/api/trips/abc/bikes")).toHaveLength(1);
    const sent = bodyOf(writes("/api/trips/abc/bikes")[0]);
    await act(async () => release(json(201, { ...sent })));
  });
});

describe("t-bikes-page-edit AC3/AC5/AC6: create", () => {
  beforeEach(riderSetup);

  test("failed create keeps input; retry replays the same id; success refetches and resets with a fresh id", async () => {
    let fail = true;
    routes["/api/trips/abc/bikes"] = (init) => {
      if (fail) {
        fail = false;
        return new Response("<html>bad gateway</html>", { status: 502 });
      }
      const b = JSON.parse(init!.body as string) as BikeOut;
      trip = { ...trip, bikes: [...trip.bikes, b] };
      return json(201, b);
    };
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 3, name: "Add bike" });
    let form = addForm();
    fillAdd(form);
    fireEvent.click(within(form).getByRole("button", { name: "Add bike" }));

    expect((await screen.findByRole("alert")).textContent).toBe(GENERIC);
    const call0 = writes("/api/trips/abc/bikes")[0];
    expect((call0[1] as RequestInit).method).toBe("POST");
    const first = bodyOf(call0);
    expect(first.id).toMatch(UUID4);
    expect(first).toEqual({ id: first.id, riderName: "Kai", make: "KTM", model: "890 Adventure", year: 2022, specs: "Rally seat" });
    expect(input(form, "Rider name")).toBe(" Kai ");
    expect(input(form, "Specs")).toBe("Rally seat");

    const before = tripRequests();
    fireEvent.click(within(form).getByRole("button", { name: "Add bike" }));
    expect(await screen.findByRole("heading", { level: 3, name: "Kai" })).toBeTruthy(); // from the refetched trip
    expect(bodyOf(writes("/api/trips/abc/bikes")[1]).id).toBe(first.id);
    expect(tripRequests()).toBeGreaterThan(before);

    form = addForm();
    await waitFor(() => expect(input(form, "Rider name")).toBe(""));
    for (const label of ["Make", "Model", "Year", "Specs"]) expect(input(form, label)).toBe("");
    expect(within(form).queryByRole("alert")).toBeNull();

    fillAdd(form);
    set(form, "Rider name", "Lee");
    fireEvent.click(within(form).getByRole("button", { name: "Add bike" }));
    await screen.findByRole("heading", { level: 3, name: "Lee" });
    const third = bodyOf(writes("/api/trips/abc/bikes")[2]);
    expect(third.id).toMatch(UUID4);
    expect(third.id).not.toBe(first.id);
  });

  test("rejected fetch shows exactly the offline message, input kept", async () => {
    routes["/api/trips/abc/bikes"] = () => Promise.reject(new TypeError("Failed to fetch"));
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 3, name: "Add bike" });
    const form = addForm();
    fillAdd(form);
    fireEvent.click(within(form).getByRole("button", { name: "Add bike" }));
    expect((await screen.findByRole("alert")).textContent).toBe(OFFLINE);
    expect(input(form, "Model")).toBe("890 Adventure");
    expect(input(form, "Year")).toBe("2022");
  });

  test("envelope error shows envelope.error.message, input kept", async () => {
    routes["/api/trips/abc/bikes"] = () => envelope(409, "ID_CONFLICT", "That id belongs to another trip.");
    renderAt("/t/abc/bikes");
    await screen.findByRole("heading", { level: 3, name: "Add bike" });
    const form = addForm();
    fillAdd(form);
    fireEvent.click(within(form).getByRole("button", { name: "Add bike" }));
    expect((await screen.findByRole("alert")).textContent).toBe("That id belongs to another trip.");
    expect(input(form, "Make")).toBe("KTM");
  });
});

describe("t-bikes-page-edit AC4/AC5/AC6: edit", () => {
  beforeEach(riderSetup);

  test('PATCH sends only changed fields, cleared specs as ""; success refetches and closes the form', async () => {
    routes["/api/trips/abc/bikes/b1"] = (init) => {
      const updated = { ...trip.bikes.find((b) => b.id === "b1")!, ...JSON.parse(init!.body as string) };
      trip = { ...trip, bikes: trip.bikes.map((b) => (b.id === "b1" ? updated : b)) };
      return json(200, updated);
    };
    renderAt("/t/abc/bikes");
    const form = await openEdit("Zoe");
    expect(input(form, "Rider name")).toBe("Zoe");
    expect(input(form, "Year")).toBe("2019");
    set(form, "Model", "Africa Twin");
    set(form, "Specs", "");
    const before = tripRequests();
    fireEvent.click(within(form).getByRole("button", { name: "Save bike" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Save bike" })).toBeNull());
    const calls = writes("/api/trips/abc/bikes/b1");
    expect(calls).toHaveLength(1);
    expect((calls[0][1] as RequestInit).method).toBe("PATCH");
    expect((calls[0][1] as RequestInit).body).not.toContain("null");
    expect(bodyOf(calls[0])).toEqual({ model: "Africa Twin", specs: "" });
    const li = (await screen.findByText("Honda Africa Twin (2019)")).closest("li")!;
    expect(tripRequests()).toBeGreaterThan(before);
    expect(within(li).getByRole("heading", { level: 3, name: "Zoe" })).toBeTruthy();
    expect(li.querySelectorAll("p")).toHaveLength(1); // specs block gone
  });

  test("changed year is sent alone, as an integer", async () => {
    routes["/api/trips/abc/bikes/b2"] = () => json(200, { ...BIKES[1], year: 2021 });
    renderAt("/t/abc/bikes");
    const form = await openEdit("alex");
    set(form, "Year", "2021");
    fireEvent.click(within(form).getByRole("button", { name: "Save bike" }));
    await waitFor(() => expect(writes("/api/trips/abc/bikes/b2")).toHaveLength(1));
    expect(bodyOf(writes("/api/trips/abc/bikes/b2")[0])).toEqual({ year: 2021 });
  });

  test("no change: no request, form closes", async () => {
    renderAt("/t/abc/bikes");
    const form = await openEdit("Mia");
    set(form, "Model", "Model-b3"); // retyped identical value
    fireEvent.click(within(form).getByRole("button", { name: "Save bike" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save bike" })).toBeNull());
    await settle();
    expect(bikeWrites()).toHaveLength(0);
  });

  test("envelope error keeps the edit form open with input intact", async () => {
    routes["/api/trips/abc/bikes/b2"] = () => envelope(403, "FORBIDDEN", "This link can only view the trip.");
    renderAt("/t/abc/bikes");
    const form = await openEdit("alex");
    set(form, "Make", "Yamaha");
    fireEvent.click(within(form).getByRole("button", { name: "Save bike" }));
    expect((await within(form).findByRole("alert")).textContent).toBe("This link can only view the trip.");
    expect(input(form, "Make")).toBe("Yamaha");
    expect(within(form).getByRole("button", { name: "Save bike" })).toBeTruthy();
  });

  test("Cancel closes the edit form without a request", async () => {
    renderAt("/t/abc/bikes");
    const form = await openEdit("alex");
    set(form, "Make", "Yamaha");
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Save bike" })).toBeNull();
    expect(screen.getByText("Honda Model-b2 (2019)")).toBeTruthy();
    expect(bikeWrites()).toHaveLength(0);
  });

  // F1 regression: invalidation must survive the form unmounting mid-request
  // (TanStack v5 skips a per-mutate() onSuccess once its observer is gone).
  test("Cancel during a pending PATCH still refetches; list shows the saved values", async () => {
    let release!: () => void;
    routes["/api/trips/abc/bikes/b2"] = (init) =>
      new Promise<Response>((r) => {
        release = () => {
          const updated = { ...trip.bikes.find((b) => b.id === "b2")!, ...JSON.parse(init!.body as string) };
          trip = { ...trip, bikes: trip.bikes.map((b) => (b.id === "b2" ? updated : b)) };
          r(json(200, updated));
        };
      });
    renderAt("/t/abc/bikes");
    const form = await openEdit("alex");
    set(form, "Make", "Yamaha");
    fireEvent.click(within(form).getByRole("button", { name: "Save bike" }));
    await waitFor(() => expect(writes("/api/trips/abc/bikes/b2")).toHaveLength(1));
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Save bike" })).toBeNull();
    expect(screen.getByText("Honda Model-b2 (2019)")).toBeTruthy(); // stale until the PATCH lands
    const before = tripRequests();
    await act(async () => release());
    expect(await screen.findByText("Yamaha Model-b2 (2019)")).toBeTruthy();
    expect(tripRequests()).toBeGreaterThan(before);
  });
});

test("F1: create still pending when the page unmounts still refetches the trip", async () => {
  riderSetup();
  let release!: () => void;
  routes["/api/trips/abc/bikes"] = (init) =>
    new Promise<Response>((r) => {
      release = () => {
        const b = JSON.parse(init!.body as string) as BikeOut;
        trip = { ...trip, bikes: [...trip.bikes, b] };
        r(json(201, b));
      };
    });
  const router = renderAt("/t/abc/bikes");
  await screen.findByRole("heading", { level: 3, name: "Add bike" });
  fillAdd(addForm());
  fireEvent.click(within(addForm()).getByRole("button", { name: "Add bike" }));
  await waitFor(() => expect(writes("/api/trips/abc/bikes")).toHaveLength(1));
  fireEvent.click(screen.getByRole("link", { name: "Back to trip" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/t/abc"));
  await waitFor(() => expect(screen.queryByRole("heading", { level: 2, name: "Bikes" })).toBeNull());
  const before = tripRequests();
  await act(async () => release());
  await waitFor(() => expect(tripRequests()).toBeGreaterThan(before));
  // Back on the bikes page (refetchOnMount:false) the cached, refetched trip has the bike.
  fireEvent.click(await screen.findByRole("link", { name: "Bikes" }));
  expect(await screen.findByRole("heading", { level: 3, name: "Kai" })).toBeTruthy();
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
