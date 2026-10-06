import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import type { StopCreate } from "../api/gen/types/StopCreate";
import { setNavigatorLocks } from "./testLocks";

// t-am-queue-classification (obligation 14). Written from the AC and
// docs/api-contract.md "Offline-queue classification (Entry 29)" — not from
// queue.ts. Harness follows queuePhotos.test.tsx: fresh module + fresh fake
// IndexedDB per test; a module reset over the same IndexedDB is the "reload".

type QueueMod = typeof import("./queue");
let Q: QueueMod;

async function start() {
  vi.resetModules();
  Q = await import("./queue");
  Q.startQueue(new QueryClient());
  await flush();
}

const setImmediate = (globalThis as unknown as { setImmediate: (f: () => void) => void }).setImmediate;
const flush = async (n = 60) => {
  for (let i = 0; i < n; i++) await new Promise<void>((r) => setImmediate(r));
};
async function until(cond: () => boolean | Promise<boolean>, what = "condition") {
  for (let i = 0; i < 500; i++) {
    if (await cond()) return;
    await new Promise<void>((r) => setImmediate(r));
  }
  throw new Error(`timed out waiting for ${what}`);
}

// ---- fetch stub ----
type Post = { url: string; id: string };
let posts: Post[];
let handler: (p: Post) => Response | Promise<Response>;
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (status: number, code: string, message: string, headers?: Record<string, string>) =>
  json(status, { error: { code, message } }, headers);
const unauth = () => envelope(401, "UNAUTHENTICATED", "Sign in to continue");
const forbidden = () => envelope(403, "FORBIDDEN", "You're no longer a rider on this trip");
const limited = (retryAfter?: string) =>
  envelope(429, "RATE_LIMITED", "Too many requests", retryAfter === undefined ? {} : { "Retry-After": retryAfter });
const boom = () => envelope(500, "INTERNAL_ERROR", "boom");
const netDown = () => Promise.reject(new TypeError("Failed to fetch"));
const ok = () => json(201, {});
const postedIds = () => posts.map((p) => p.id);

// ---- raw IndexedDB access, independent of queue.ts ----
type Raw = { key: IDBValidKey; value: Record<string, unknown> };
function rawAll(): Promise<Raw[]> {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open("btj-queue");
    r.onerror = () => reject(r.error);
    r.onsuccess = () => {
      const db = r.result;
      const tx = db.transaction("entries", "readonly");
      const out: Raw[] = [];
      const c = tx.objectStore("entries").openCursor();
      c.onsuccess = () => {
        if (c.result) {
          out.push({ key: c.result.key, value: c.result.value });
          c.result.continue();
        }
      };
      tx.oncomplete = () => {
        db.close();
        resolve(out);
      };
      tx.onerror = () => reject(tx.error);
    };
  });
}
const rawById = async (id: string) =>
  (await rawAll()).find((r) => (r.value.payload as { data: { id: string } }).data.id === id);
const bytesOf = (v: unknown) => Array.from(new Uint8Array(v as ArrayBuffer));
const blobBytes = (b: Blob) =>
  new Promise<number[]>((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(bytesOf(fr.result));
    fr.onerror = () => reject(fr.error);
    fr.readAsArrayBuffer(b);
  });

// ---- fixtures ----
let n = 0;
const uuid = () => `00000000-0000-4000-9000-${String(++n).padStart(12, "0")}`;
const stop = (name: string): StopCreate => ({
  id: uuid(),
  name,
  lat: -16.4,
  lng: 133.4,
  locationSource: "gps",
  arrivedAt: "2026-10-03T08:30:00Z",
  notes: null,
});
const stopItem = (data: StopCreate) => ({ kind: "stop" as const, payload: { slug: "abc", data } });
const jpegBytes = (seed: number) => {
  const b = new Uint8Array(1024);
  for (let i = 0; i < b.length; i++) b[i] = (i * 31 + seed * 7) & 0xff;
  b.set([0xff, 0xd8, 0xff, 0xe0, 0x00, 0xc3, 0x28], 0);
  return b;
};
type Photo = { id: string; bytes: Uint8Array<ArrayBuffer>; stopId: string; stopName: string };
const photo = (s: StopCreate, seed = 1): Photo => ({ id: uuid(), bytes: jpegBytes(seed), stopId: s.id, stopName: s.name });
const photoItem = (p: Photo) => ({
  kind: "photo" as const,
  payload: {
    slug: "abc",
    stopId: p.stopId,
    stopName: p.stopName,
    data: { id: p.id, uploadedBy: "Wes", takenAt: "2026-10-03T08:41:07+09:30" },
  },
  file: new Blob([p.bytes], { type: "image/jpeg" }),
});

// ---- BroadcastChannel: undefined by default; real (and tracked, so closed) on request ----
const RealBC = globalThis.BroadcastChannel;
let opened: BroadcastChannel[];
function useRealBroadcastChannel() {
  vi.stubGlobal(
    "BroadcastChannel",
    class extends RealBC {
      constructor(name: string) {
        super(name);
        opened.push(this);
      }
    },
  );
}
async function broadcastSignin() {
  const ch = new BroadcastChannel("auth");
  ch.postMessage({ type: "signin" });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("indexedDB", new IDBFactory());
  vi.stubGlobal("BroadcastChannel", undefined);
  localStorage.clear();
  opened = [];
  posts = [];
  handler = ok;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "POST") throw new Error(`unstubbed ${init?.method} ${String(input)}`);
      const id = init.body instanceof FormData ? String(init.body.get("id")) : (JSON.parse(String(init.body)) as StopCreate).id;
      const p = { url: String(input), id };
      posts.push(p);
      return handler(p);
    }),
  );
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  Object.defineProperty(navigator, "storage", { configurable: true, value: { persist: vi.fn(async () => true) } });
  setNavigatorLocks(undefined);
  vi.spyOn(window, "addEventListener");
  vi.spyOn(document, "addEventListener");
});

afterEach(async () => {
  cleanup();
  await flush();
  for (const ch of opened) ch.close();
  for (const t of [window, document] as EventTarget[]) {
    for (const [type, fn, opts] of vi.mocked(t.addEventListener).mock.calls) {
      if (fn) t.removeEventListener(type, fn, opts);
    }
  }
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// Advance to one ms before `ms`, confirm silence, then the last ms fires a send.
async function expectRetryAt(ms: number) {
  const before = posts.length;
  await vi.advanceTimersByTimeAsync(ms - 1);
  await flush();
  expect(posts.length, `no send before ${ms}ms`).toBe(before);
  await vi.advanceTimersByTimeAsync(1);
  await until(() => posts.length === before + 1, `send at ${ms}ms`);
  await flush();
}

const settle = () => act(() => flush());

// ---------------------------------------------------------------------------
describe("401 UNAUTHENTICATED: pause", () => {
  test("not failed, attempts unchanged, drain stops, notice counts only non-failed entries", async () => {
    await start();
    const { QueueNotice } = await import("./QueueNotice");
    const s = stop("Mataranka");
    const gone = photo(s, 1);
    const a = stop("A");
    const b = stop("B");
    // A failed (403) entry first: it must not be counted in "Sign in to send N items".
    handler = (p) => (p.id === gone.id ? forbidden() : unauth());
    await Q.enqueue(photoItem(gone));
    await flush();
    expect((await rawById(gone.id))?.value.failed).toBe(true);

    await Q.enqueue([stopItem(a), stopItem(b)]);
    await flush();
    expect(postedIds()).toEqual([gone.id, a.id]); // drain stopped at A; B never sent

    const rowA = await rawById(a.id);
    expect(rowA?.value.failed).toBe(false);
    expect(rowA?.value.attempts).toBe(0);
    expect((await rawById(b.id))?.value).toMatchObject({ failed: false, attempts: 0 });
    expect(Q.isPaused()).toBe(true);

    // Drain stays stopped: no backoff retry is scheduled for a 401.
    await vi.advanceTimersByTimeAsync(600_000);
    await flush();
    expect(postedIds()).toEqual([gone.id, a.id]);

    const { container } = render(<QueueNotice />);
    await settle();
    expect(container.textContent).toContain("Sign in to send 2 items");
  });

  test("the pause survives a reload (module re-import over the same IndexedDB)", async () => {
    await start();
    const { QueueNotice } = await import("./QueueNotice");
    const a = stop("A");
    const b = stop("B");
    handler = unauth;
    await Q.enqueue([stopItem(a), stopItem(b)]);
    await flush();
    expect(Q.isPaused()).toBe(true);
    cleanup();

    await start(); // reload
    const { QueueNotice: QueueNotice2 } = await import("./QueueNotice");
    expect(QueueNotice2).not.toBe(QueueNotice);
    expect(Q.isPaused()).toBe(true);
    const { container } = render(<QueueNotice2 />);
    await settle();
    expect(container.textContent).toContain("Sign in to send 2 items");
    expect((await rawById(a.id))?.value).toMatchObject({ failed: false, attempts: 0 });
  });

  test("a successful send clears the pause", async () => {
    await start();
    const a = stop("A");
    handler = unauth;
    await Q.enqueue(stopItem(a));
    await flush();
    expect(Q.isPaused()).toBe(true);

    handler = ok;
    window.dispatchEvent(new Event("online")); // a usual trigger
    await until(async () => (await rawById(a.id)) === undefined, "A sent and deleted");
    expect(Q.isPaused()).toBe(false);
    expect(localStorage.getItem("btj.queue.paused")).toBeNull();
  });

  test("an `auth` signin broadcast restarts the drain and sends the entry", async () => {
    useRealBroadcastChannel();
    await start();
    const a = stop("A");
    const b = stop("B");
    handler = unauth;
    await Q.enqueue([stopItem(a), stopItem(b)]);
    await flush();
    expect(postedIds()).toEqual([a.id]);

    handler = ok;
    await broadcastSignin();
    await until(async () => (await rawAll()).length === 0, "both entries sent after signin");
    expect(postedIds()).toEqual([a.id, a.id, b.id]);
    expect(Q.isPaused()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
describe("403 FORBIDDEN: failed, blob kept, rescue download", () => {
  test("'Save photo to this device' downloads the stored JPEG via an object URL and <a download>", async () => {
    const createObjectURL = vi.fn((_: Blob) => "blob:btj/rescued");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: revokeObjectURL });
    const clicked: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this);
    });

    await start();
    const { QueueNotice, REVOKE_DELAY_MS } = await import("./QueueNotice");
    const badStop = stop("Bad Stop");
    const revoked = photo(stop("Daly Waters Pub"), 1);
    const stuck = photo(stop("Larrimah Hotel"), 2);
    handler = (p) =>
      p.id === badStop.id ? envelope(422, "VALIDATION_ERROR", "bad") : p.id === revoked.id ? forbidden() : netDown();
    await Q.enqueue([stopItem(badStop), photoItem(revoked), photoItem(stuck)]);
    await flush();

    const row = await rawById(revoked.id);
    expect(row?.value).toMatchObject({ kind: "photo", failed: true, lastError: "You're no longer a rider on this trip" });
    expect(bytesOf(row?.value.blob)).toEqual(Array.from(revoked.bytes));
    expect((await rawById(stuck.id))?.value.failed).toBe(false);

    render(<QueueNotice />);
    await settle();
    // Only the failed photo row offers the rescue — not the failed stop, not the still-trying photo.
    const buttons = screen.getAllByRole("button", { name: /save photo to this device/i });
    expect(buttons).toHaveLength(1);
    const items = screen.getAllByRole("listitem");
    const revokedRow = items.find((li) => li.textContent?.includes("Photo for Daly Waters Pub"))!;
    expect(revokedRow).toBeDefined();
    expect(within(revokedRow).getByRole("button", { name: /save photo to this device/i })).toBe(buttons[0]);

    fireEvent.click(buttons[0]);
    await settle();
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0][0];
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe("image/jpeg");
    expect(await blobBytes(blob)).toEqual(Array.from(revoked.bytes));
    expect(clicked).toHaveLength(1);
    expect(clicked[0].hasAttribute("download")).toBe(true);
    expect(clicked[0].getAttribute("href")).toBe("blob:btj/rescued");

    await vi.advanceTimersByTimeAsync(REVOKE_DELAY_MS - 1);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:btj/rescued");
    // The rescue doesn't delete the entry; only Dismiss does.
    expect(await rawById(revoked.id)).toBeDefined();
  });

  test("Entry 29 no grace: a revoked rider's 403 photo is never resent, signin broadcasts included", async () => {
    useRealBroadcastChannel();
    await start();
    const revoked = photo(stop("S"), 3);
    handler = forbidden;
    await Q.enqueue(photoItem(revoked));
    await flush();
    expect(postedIds()).toEqual([revoked.id]);

    handler = ok;
    for (let i = 0; i < 3; i++) {
      await broadcastSignin();
      await flush();
      window.dispatchEvent(new Event("online"));
      await vi.advanceTimersByTimeAsync(600_000);
      await Q.drain();
      await flush();
    }
    expect(postedIds()).toEqual([revoked.id]);
    const row = await rawById(revoked.id);
    expect(row?.value.failed).toBe(true);
    expect(bytesOf(row?.value.blob)).toEqual(Array.from(revoked.bytes));
  });
});

// ---------------------------------------------------------------------------
describe("429 RATE_LIMITED", () => {
  test("Retry-After: 7 schedules the next attempt at 7 s, counts it, and does not advance the doubling backoff", async () => {
    await start();
    const a = stop("A");
    const replies = [boom, () => limited("7"), boom, ok];
    handler = () => replies.shift()!();
    await Q.enqueue(stopItem(a));
    await flush();
    expect(posts).toHaveLength(1); // 500 → backoff 5 s
    await expectRetryAt(5_000); // 429 Retry-After: 7
    expect((await rawById(a.id))?.value).toMatchObject({ failed: false, attempts: 2 });
    await expectRetryAt(7_000); // 500: next step is 10 s (pre-429 step), not 20 s
    expect((await rawById(a.id))?.value).toMatchObject({ failed: false, attempts: 3 });
    await expectRetryAt(10_000);
    await until(async () => (await rawById(a.id)) === undefined, "A sent");
  });

  test("a first-attempt 429 with Retry-After leaves the backoff at its first step", async () => {
    await start();
    const a = stop("A");
    const replies = [() => limited("7"), boom, ok];
    handler = () => replies.shift()!();
    await Q.enqueue(stopItem(a));
    await flush();
    expect((await rawById(a.id))?.value).toMatchObject({ failed: false, attempts: 1 });
    await expectRetryAt(7_000); // 500
    await expectRetryAt(5_000);
  });

  test("429 without Retry-After uses normal backoff and counts attempts", async () => {
    await start();
    const a = stop("A");
    handler = () => limited();
    await Q.enqueue(stopItem(a));
    await flush();
    await expectRetryAt(5_000);
    await expectRetryAt(10_000);
    expect((await rawById(a.id))?.value).toMatchObject({ failed: false, attempts: 3 });
  });
});

// ---------------------------------------------------------------------------
describe("frozen pre-upgrade classification", () => {
  // Pinned copy of the five-code NEVER_RETRY set shipped before Entry 29. Do not edit:
  // it stands in for clients already installed on riders' phones.
  const OLD_NEVER_RETRY: ReadonlySet<string> = new Set([
    "VALIDATION_ERROR",
    "METHOD_NOT_ALLOWED",
    "CONFLICT",
    "FORBIDDEN",
    "NOT_FOUND",
  ]);
  const oldIsNeverRetry = (err: unknown) =>
    err instanceof ApiError && OLD_NEVER_RETRY.has(err.envelope?.error.code as string);
  const err = (status: number, code: string) =>
    new ApiError("x", status, { error: { code, message: "x" } } as ConstructorParameters<typeof ApiError>[2]);

  test("old clients treat UNAUTHENTICATED and RATE_LIMITED as retryable", () => {
    expect(OLD_NEVER_RETRY.size).toBe(5);
    expect(OLD_NEVER_RETRY.has("UNAUTHENTICATED")).toBe(false);
    expect(OLD_NEVER_RETRY.has("RATE_LIMITED")).toBe(false);
    expect(oldIsNeverRetry(err(401, "UNAUTHENTICATED"))).toBe(false);
    expect(oldIsNeverRetry(err(429, "RATE_LIMITED"))).toBe(false);
    for (const code of OLD_NEVER_RETRY) expect(oldIsNeverRetry(err(400, code))).toBe(true);
  });

  test.each([
    ["401 UNAUTHENTICATED", unauth],
    ["429 RATE_LIMITED", () => limited("7")],
  ])("the current queue never fails an entry on %s (pauses or retries; blob kept)", async (_, respond) => {
    await start();
    expect(Q.isNeverRetry(err(401, "UNAUTHENTICATED"))).toBe(false);
    expect(Q.isNeverRetry(err(429, "RATE_LIMITED"))).toBe(false);
    const ph = photo(stop("S"), 4);
    handler = respond;
    await Q.enqueue(photoItem(ph));
    await flush();
    const row = await rawById(ph.id);
    expect(row?.value.failed).toBe(false);
    expect(bytesOf(row?.value.blob)).toEqual(Array.from(ph.bytes));
  });
});
