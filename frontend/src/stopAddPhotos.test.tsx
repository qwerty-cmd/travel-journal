import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import type { TripOut } from "./api/gen/types/TripOut";
import type { V2PhotoItem } from "./offline/queue";

// Add photos to an existing stop (docs/design/screens/stop-detail.md): rendering,
// the role gate, the picker and the shape Save hands to enqueue. enqueue is spied;
// processPhoto is stubbed.

vi.mock("./offline/QueueNotice", () => ({ QueueNotice: () => null }));
const enqueueMock = vi.hoisted(() => vi.fn<(items: unknown, userId?: string) => Promise<void>>());
vi.mock("./offline/queue", async (orig) => ({ ...(await orig<typeof import("./offline/queue")>()), enqueue: enqueueMock }));
const processPhotoMock = vi.hoisted(() => vi.fn<(file: File) => Promise<{ blob: Blob; takenAt: string }>>());
vi.mock("./photo", async (orig) => ({ ...(await orig<typeof import("./photo")>()), processPhoto: processPhotoMock }));

const ID = "11111111-1111-4111-8111-111111111111";
const USER = "22222222-2222-4222-8222-222222222222";
const STOP = { id: "s1", tripId: ID, name: "Coober Pedy", arrivedAt: "2026-10-13T08:00:00Z", lat: -29, lng: 134.7, locationSource: "gps", notes: null };
const PHOTO = { id: "p1", stopId: "s1", url: "https://img/p1.jpg", uploadedBy: "Ann", takenAt: STOP.arrivedAt, archived: false };

const trip = (role: string | undefined): TripOut =>
  ({
    id: ID,
    name: "Stuart Hwy 2026",
    startDate: "2026-10-12",
    bikes: [],
    access: "rider",
    visibility: "public",
    publicDelayHours: 0,
    riderCount: 2,
    lastPublicStopAt: null,
    ...(role === undefined ? {} : { viewer: { role } }),
  }) as TripOut;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let tripBody: TripOut;
let photos: unknown[];

function renderStop() {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [`/trips/${ID}/stops/s1`] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}
const pickFiles = (...names: string[]) =>
  fireEvent.change(screen.getByLabelText("Photos"), { target: { files: names.map((n) => new File([n], n)) } });

beforeEach(() => {
  localStorage.clear();
  tripBody = trip("rider");
  photos = [PHOTO];
  enqueueMock.mockReset();
  enqueueMock.mockResolvedValue(undefined);
  processPhotoMock.mockReset();
  processPhotoMock.mockImplementation(async (f: File) => ({ blob: new Blob([f.name], { type: "image/jpeg" }), takenAt: "2026-10-13T08:15:00+09:30" }));
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const p = new URL(String(input), "https://testserver").pathname;
    if (p === "/api/v2/auth/me") return json(200, { id: USER, displayName: "Wes", username: "wes" });
    if (p === `/api/v2/trips/${ID}`) return json(200, tripBody);
    if (p === `/api/v2/trips/${ID}/stops`) return json(200, [STOP]);
    if (p === `/api/v2/trips/${ID}/stops/s1/photos`) return json(200, photos);
    if (p.endsWith("/map")) return json(200, { type: "FeatureCollection", features: [] });
    return json(404, { error: { code: "NOT_FOUND", message: "x" } });
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("role gate", () => {
  test.each(["rider", "leader"])("%s sees Add photos in the header row", async (role) => {
    tripBody = trip(role);
    renderStop();
    expect(await screen.findByRole("heading", { name: "Photos (1)" })).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Add photos" })).toBeTruthy();
  });

  test.each([["visitor"], ["pending"], ["none"], ["anonymous"], [undefined]])("role %s: no Add photos, no picker", async (role) => {
    tripBody = trip(role);
    renderStop();
    await screen.findByRole("img", { name: "Photo by Ann" });
    await screen.findByRole("heading", { name: "Photos (1)" });
    expect(screen.queryByRole("button", { name: "Add photos" })).toBeNull();
    expect(document.querySelector('input[type="file"]')).toBeNull();
  });

  test("no photos: a rider gets one Add photos, in the empty state", async () => {
    photos = [];
    renderStop();
    await screen.findByText("No photos yet");
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Add photos" })).toHaveLength(1));
  });

  test("no photos: a visitor gets 'No photos yet' and no button", async () => {
    photos = [];
    tripBody = trip("visitor");
    renderStop();
    await screen.findByText("No photos yet");
    expect(screen.queryByRole("button", { name: "Add photos" })).toBeNull();
  });
});

describe("picker and save", () => {
  test("pick two, remove one, focus moves; Save 1 photo enqueues one v2 entry and clears the strip", async () => {
    renderStop();
    await screen.findByRole("button", { name: "Add photos" });
    pickFiles("a.jpg", "b.jpg");
    await screen.findByRole("button", { name: "Save 2 photos" });
    fireEvent.click(screen.getByRole("button", { name: "Remove photo 1" }));
    const remaining = await screen.findByRole("button", { name: "Remove photo 1" });
    expect(screen.queryByRole("button", { name: "Remove photo 2" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(remaining));

    fireEvent.click(screen.getByRole("button", { name: "Save 1 photo" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Remove photo 1" })).toBeNull());
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Add photos" }));
    expect(enqueueMock).toHaveBeenCalledTimes(1);
    const [items, userId] = enqueueMock.mock.calls[0] as [V2PhotoItem[], string];
    expect(userId).toBe(USER);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "photo",
      payload: { tripId: ID, stopId: "s1", stopName: "Coober Pedy", data: { takenAt: "2026-10-13T08:15:00+09:30" } },
    });
    expect(Object.keys(items[0].payload.data).sort()).toEqual(["id", "takenAt"]);
    expect(items[0].payload).not.toHaveProperty("slug");
    expect(items[0].file).toBeInstanceOf(Blob);
  });

  test("Save N enqueues N entries with distinct client ids in one call", async () => {
    renderStop();
    await screen.findByRole("button", { name: "Add photos" });
    pickFiles("a.jpg", "b.jpg", "c.jpg");
    fireEvent.click(await screen.findByRole("button", { name: "Save 3 photos" }));
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(1));
    const [items] = enqueueMock.mock.calls[0] as [V2PhotoItem[]];
    expect(items).toHaveLength(3);
    expect(new Set(items.map((i) => i.payload.data.id)).size).toBe(3);
  });

  test("Save is disabled with the reason while a photo processes", async () => {
    let finish!: () => void;
    renderStop();
    await screen.findByRole("button", { name: "Add photos" });
    pickFiles("a.jpg");
    await screen.findByRole("button", { name: "Save 1 photo" });
    processPhotoMock.mockImplementationOnce(
      (f: File) => new Promise((r) => (finish = () => r({ blob: new Blob([f.name]), takenAt: "2026-10-13T08:15:00+09:30" }))),
    );
    pickFiles("b.jpg");
    expect(await screen.findByText("Processing 1 photo…")).toBeTruthy();
    expect(screen.getByText("Wait for photos to finish processing.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save 1 photo" }) as HTMLButtonElement).disabled).toBe(true);
    finish();
    expect(((await screen.findByRole("button", { name: "Save 2 photos" })) as HTMLButtonElement).disabled).toBe(false);
  });

  test("an unreadable file shows the inline line; Cancel discards the picks", async () => {
    processPhotoMock.mockImplementation(async (f: File) => {
      if (f.name.endsWith(".HEIC")) throw new Error("decode");
      return { blob: new Blob([f.name]), takenAt: "2026-10-13T08:15:00+09:30" };
    });
    renderStop();
    await screen.findByRole("button", { name: "Add photos" });
    pickFiles("a.jpg", "IMG_0042.HEIC");
    expect(await screen.findByText("Couldn't read photo IMG_0042.HEIC")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Remove photo 1" })).toBeNull();
    expect(enqueueMock).not.toHaveBeenCalled();
  });

  test("a local save failure keeps the picks and offers Try again", async () => {
    enqueueMock.mockRejectedValueOnce(new Error("idb"));
    renderStop();
    await screen.findByRole("button", { name: "Add photos" });
    pickFiles("a.jpg");
    fireEvent.click(await screen.findByRole("button", { name: "Save 1 photo" }));
    expect(await screen.findByText("Couldn't save the photos on this device")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remove photo 1" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(enqueueMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("Couldn't save the photos on this device")).toBeNull());
  });
});
