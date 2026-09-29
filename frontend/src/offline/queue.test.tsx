import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { listStopsApiTripsSlugStopsGetQueryKey } from "../api/gen/hooks/useListStopsApiTripsSlugStopsGet";
import { getMapApiTripsSlugMapGetQueryKey } from "../api/gen/hooks/useGetMapApiTripsSlugMapGet";
import type { StopCreate } from "../api/gen/types/StopCreate";

// t-offline-queue-core. Written from the AC and docs/api-contract.md (Error
// envelope, POST /trips/{slug}/stops) — not from queue.ts. Each test gets a
// fresh module instance and a fresh fake IndexedDB; only AC9 keeps the same
// IndexedDB across a module reset, which is the "app restart".

type QueueMod = typeof import("./queue");
type ClientMod = typeof import("../api/client");
let Q: QueueMod;
let C: ClientMod;
let qc: QueryClient;

async function load() {
  vi.resetModules();
  Q = await import("./queue");
  C = await import("../api/client");
}

async function start() {
  await load();
  qc = new QueryClient();
  Q.startQueue(qc);
  await flush();
}

// fake-indexeddb schedules on setImmediate, which the fake timers below leave real.
// (Node global; tsconfig has no node types.)
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

// ---- fetch stub: every POST is recorded; the response comes from `handler` ----
type Post = { url: string; body: StopCreate };
let posts: Post[];
let handler: (p: Post) => Response | Promise<Response>;
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const envelope = (status: number, code: string, message: string) => json(status, { error: { code, message } });
const netDown = () => Promise.reject(new TypeError("Failed to fetch"));
const created = (p: Post) => json(201, { ...p.body, notes: p.body.notes ?? null });

// ---- raw IndexedDB access, independent of queue.ts ----
type Raw = { key: IDBValidKey; value: Record<string, unknown> };
function withStore<T>(fn: (s: IDBObjectStore, done: (v: T) => void) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open("btj-queue");
    r.onerror = () => reject(r.error);
    r.onsuccess = () => {
      const db = r.result;
      const tx = db.transaction("entries", "readonly");
      let out: T;
      fn(tx.objectStore("entries"), (v) => (out = v));
      tx.oncomplete = () => {
        db.close();
        resolve(out);
      };
      tx.onerror = () => reject(tx.error);
    };
  });
}
const rawAll = () =>
  withStore<Raw[]>((s, done) => {
    const out: Raw[] = [];
    done(out);
    const c = s.openCursor();
    c.onsuccess = () => {
      const cur = c.result;
      if (cur) {
        out.push({ key: cur.key, value: cur.value });
        cur.continue();
      }
    };
  });
const rawById = async (id: string) =>
  (await rawAll()).find((r) => (r.value.payload as { data: StopCreate }).data.id === id);

let n = 0;
const stop = (name: string): StopCreate => ({
  id: `00000000-0000-4000-8000-${String(++n).padStart(12, "0")}`,
  name,
  lat: -16.4,
  lng: 133.4,
  locationSource: "gps",
  arrivedAt: "2026-10-03T08:30:00Z",
  notes: null,
});
const item = (data: StopCreate, slug = "abc") => ({ kind: "stop" as const, payload: { slug, data } });
const postedIds = () => posts.map((p) => p.body.id);

// ---- environment ----
let visibility: DocumentVisibilityState;
let persist: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("indexedDB", new IDBFactory());
  // Single-tab tests: Node's real BroadcastChannel would link every module
  // instance started in this file. Cross-tab behaviour is queueTabs.test.tsx.
  vi.stubGlobal("BroadcastChannel", undefined);
  posts = [];
  handler = created;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "POST") throw new Error(`unstubbed ${init?.method} ${String(input)}`);
      const p = { url: String(input), body: JSON.parse(String(init.body)) as StopCreate };
      posts.push(p);
      return handler(p);
    }),
  );
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  persist = vi.fn(async () => true);
  Object.defineProperty(navigator, "storage", { configurable: true, value: { persist } });
  // Recorded so a previous test's module instance stops reacting to online/visibilitychange.
  vi.spyOn(window, "addEventListener");
  vi.spyOn(document, "addEventListener");
});

afterEach(async () => {
  cleanup();
  await flush();
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

// ---------------------------------------------------------------------------
describe("AC4: isNeverRetry classifies by envelope code only", () => {
  beforeEach(load);
  const env = (code: string) => ({ error: { code, message: `m ${code}` } }) as never;

  test.each([
    ["VALIDATION_ERROR", 422],
    ["METHOD_NOT_ALLOWED", 405],
    ["CONFLICT", 409],
    ["FORBIDDEN", 403],
    ["NOT_FOUND", 404],
  ])("%s is never-retry", (code, status) => {
    expect(Q.isNeverRetry(new C.ApiError("x", status, env(code)))).toBe(true);
  });

  test.each<[string, () => unknown]>([
    ["INTERNAL_ERROR envelope", () => new C.ApiError("x", 500, env("INTERNAL_ERROR"))],
    ["ApiError with status but no envelope (502 HTML)", () => new C.ApiError("x", 502)],
    ["ApiError 409 status but no envelope (status alone never classifies)", () => new C.ApiError("x", 409)],
    ["ApiError with no status (network)", () => new C.ApiError("Failed to fetch")],
    ["raw SyntaxError", () => new SyntaxError("Unexpected token <")],
    ["raw TypeError", () => new TypeError("network error")],
    ["unknown code string", () => new C.ApiError("x", 429, env("RATE_LIMITED"))],
    ["envelope-shaped non-ApiError", () => Object.assign(new Error("x"), { envelope: env("CONFLICT") })],
    ["undefined", () => undefined],
  ])("%s retries", (_, make) => {
    expect(Q.isNeverRetry(make())).toBe(false);
  });
});

// ---------------------------------------------------------------------------
describe("AC1: storage shape", () => {
  test("enqueue writes {kind, payload, attempts:0, lastError:null, failed:false} under an auto-increment key, then drains", async () => {
    await start();
    let release!: (r: Response) => void;
    handler = () => new Promise((r) => (release = r));
    const a = stop("Daly Waters Pub");
    const b = stop("Larrimah");
    await Q.enqueue(item(a));
    // Resolved => committed: an independent connection sees it.
    const [first] = await rawAll();
    expect(first.value).toEqual({ kind: "stop", payload: { slug: "abc", data: a }, attempts: 0, lastError: null, failed: false });
    await until(() => posts.length === 1, "drain after enqueue");
    expect(postedIds()).toEqual([a.id]);
    await Q.enqueue(item(b));
    const rows = await rawAll();
    expect(typeof rows[0].key).toBe("number");
    expect(rows[1].key as number).toBeGreaterThan(rows[0].key as number);
    const meta = await withStore<{ keyPath: unknown; autoIncrement: boolean }>((s, done) =>
      done({ keyPath: s.keyPath, autoIncrement: s.autoIncrement }),
    );
    expect(meta).toEqual({ keyPath: null, autoIncrement: true });
    handler = created;
    release(created(posts[0]));
    await Q.drain();
  });
});

// ---------------------------------------------------------------------------
describe("AC3: FIFO drain, delete + invalidate on 2xx", () => {
  test("sends in ascending key order, one at a time, and empties the store", async () => {
    await start();
    handler = netDown;
    const [a, b, c] = [stop("A"), stop("B"), stop("C")];
    for (const s of [a, b, c]) await Q.enqueue(item(s));
    await Q.drain();
    expect(new Set(postedIds())).toEqual(new Set([a.id])); // stopped at A every time
    posts = [];
    let inFlight = 0;
    let maxInFlight = 0;
    handler = async (p) => {
      maxInFlight = Math.max(maxInFlight, ++inFlight);
      await flush(5);
      inFlight--;
      return created(p);
    };
    await Q.drain();
    expect(postedIds()).toEqual([a.id, b.id, c.id]);
    expect(maxInFlight).toBe(1);
    expect(await rawAll()).toEqual([]);
    expect(posts.map((p) => p.url)).toEqual(Array(3).fill("/api/trips/abc/stops"));
  });

  test("201 and 200 replay both delete and invalidate that trip's stops + map queries", async () => {
    await start();
    handler = netDown;
    const a = stop("A");
    const b = stop("B");
    await Q.enqueue(item(a, "abc"));
    await Q.enqueue(item(b, "xyz"));
    await flush();
    const keys = ["abc", "xyz"].flatMap((slug) => [
      listStopsApiTripsSlugStopsGetQueryKey({ slug }),
      getMapApiTripsSlugMapGetQueryKey({ slug }),
    ]);
    for (const k of keys) qc.setQueryData(k, []);
    handler = (p) => (p.body.id === a.id ? created(p) : json(200, { ...p.body })); // b is a replay
    await Q.drain();
    expect(await rawAll()).toEqual([]);
    expect(posts.at(-1)!.url).toBe("/api/trips/xyz/stops");
    for (const k of keys) expect(qc.getQueryState(k)?.isInvalidated, JSON.stringify(k)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
describe("AC5: never-retry marks failed, keeps it, and continues", () => {
  test.each([
    [422, "VALIDATION_ERROR"],
    [405, "METHOD_NOT_ALLOWED"],
    [409, "CONFLICT"],
    [403, "FORBIDDEN"],
    [404, "NOT_FOUND"],
  ])("%i %s", async (status, code) => {
    await start();
    const a = stop("Bad");
    const b = stop("Good");
    const msg = `server says ${code}`;
    handler = (p) => (p.body.id === a.id ? envelope(status, code, msg) : created(p));
    await Q.enqueue(item(a));
    await Q.enqueue(item(b));
    await Q.drain();
    expect(postedIds()).toEqual([a.id, b.id]); // continued past A; A not re-sent
    const rows = await rawAll();
    expect(rows).toHaveLength(1);
    expect(rows[0].value).toMatchObject({ failed: true, lastError: msg, payload: { data: a } });
  });

  test("a failed entry is never auto-deleted or re-sent, only dismiss() removes it", async () => {
    await start();
    const a = stop("Bad");
    handler = () => envelope(422, "VALIDATION_ERROR", "bad");
    await Q.enqueue(item(a));
    await Q.drain();
    handler = created;
    window.dispatchEvent(new Event("online"));
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(600_000);
    await Q.drain();
    await Q.enqueue(item(stop("Other")));
    await Q.drain();
    expect(postedIds().filter((id) => id === a.id)).toHaveLength(1);
    const row = await rawById(a.id);
    expect(row?.value.failed).toBe(true);
    await Q.dismiss(row!.key as number);
    expect(await rawAll()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
describe("AC6: retryable failure counts the attempt and stops the drain", () => {
  test.each<[string, () => Response | Promise<Response>]>([
    ["500 INTERNAL_ERROR", () => envelope(500, "INTERNAL_ERROR", "boom")],
    ["network error", netDown],
    ["502 HTML from an intermediary", () => new Response("<html>Bad Gateway</html>", { status: 502 })],
    ["409 without an envelope", () => new Response("<html>conflict</html>", { status: 409 })],
    ["unknown envelope code", () => envelope(429, "RATE_LIMITED", "slow down")],
    ["2xx with a non-JSON body", () => new Response("not json", { status: 201 })],
  ])("%s", async (_, respond) => {
    await start();
    const a = stop("A");
    const b = stop("B");
    handler = (p) => (p.body.id === a.id ? respond() : created(p));
    await Q.enqueue(item(a));
    await flush();
    await Q.enqueue(item(b));
    await flush();
    expect(new Set(postedIds())).toEqual(new Set([a.id])); // B never sent behind a retrying A
    const row = await rawById(a.id);
    expect(row?.value.failed).toBe(false);
    expect(row?.value.attempts as number).toBeGreaterThanOrEqual(1);
    expect(row?.value.attempts).toBe(posts.length);
    expect(typeof row?.value.lastError).toBe("string");
    expect((row?.value.lastError as string).length).toBeGreaterThan(0);
    expect(await rawById(b.id)).toBeDefined();
  });

  // Advance to one ms before `ms`, confirm silence, then the last ms fires a send.
  async function expectRetryAfter(ms: number) {
    const before = posts.length;
    await vi.advanceTimersByTimeAsync(ms - 1);
    await flush();
    expect(posts.length, `no send before ${ms}ms`).toBe(before);
    await vi.advanceTimersByTimeAsync(1);
    await until(() => posts.length === before + 1, `send at ${ms}ms`);
    await flush();
  }

  test("backoff 5s doubling to a 300s cap", async () => {
    await start();
    handler = netDown;
    await Q.enqueue(item(stop("A")));
    await until(() => posts.length === 1);
    await flush();
    for (const ms of [5_000, 10_000, 20_000, 40_000, 80_000, 160_000, 300_000, 300_000]) await expectRetryAfter(ms);
    expect((await rawAll())[0].value.attempts).toBe(9);
  });

  test("a 2xx resets the backoff", async () => {
    await start();
    handler = netDown;
    await Q.enqueue(item(stop("A")));
    await until(() => posts.length === 1);
    await flush();
    await expectRetryAfter(5_000);
    handler = created;
    await expectRetryAfter(10_000); // A succeeds here
    expect(await rawAll()).toEqual([]);
    handler = netDown;
    await Q.enqueue(item(stop("B")));
    await until(() => posts.length === 4);
    await flush();
    await expectRetryAfter(5_000);
  });

  test("`online` resets the backoff and drains immediately", async () => {
    await start();
    handler = netDown;
    await Q.enqueue(item(stop("A")));
    await until(() => posts.length === 1);
    await flush();
    await expectRetryAfter(5_000);
    await expectRetryAfter(10_000); // next would be 20s
    window.dispatchEvent(new Event("online"));
    await until(() => posts.length === 4, "immediate send on online");
    await flush();
    await expectRetryAfter(5_000);
  });
});

// ---------------------------------------------------------------------------
describe("AC2: drain triggers and coalescing", () => {
  test("visibilitychange drains only when visible, ignoring backoff", async () => {
    await start();
    handler = netDown;
    await Q.enqueue(item(stop("A")));
    await until(() => posts.length === 1);
    await flush();
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(posts).toHaveLength(1);
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await until(() => posts.length === 2, "drain on visible");
  });

  test("triggers during a drain coalesce: no concurrent sends, one follow-up picks up new work", async () => {
    await start();
    const a = stop("A");
    const b = stop("B");
    let releaseA!: () => void;
    handler = (p) =>
      p.body.id === a.id ? new Promise<Response>((r) => (releaseA = () => r(created(p)))) : created(p);
    await Q.enqueue(item(a));
    await until(() => posts.length === 1);
    window.dispatchEvent(new Event("online"));
    document.dispatchEvent(new Event("visibilitychange"));
    void Q.drain();
    void Q.drain();
    await Q.enqueue(item(b));
    await flush();
    expect(postedIds()).toEqual([a.id]); // A in flight, nothing else sent concurrently
    releaseA();
    await Q.drain();
    expect(postedIds()).toEqual([a.id, b.id]);
    expect(await rawAll()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
describe("AC8: navigator.storage.persist()", () => {
  test("called exactly once, from startQueue", async () => {
    await load();
    expect(persist).not.toHaveBeenCalled();
    Q.startQueue(new QueryClient());
    await flush();
    await Q.enqueue(item(stop("A")));
    await Q.drain();
    window.dispatchEvent(new Event("online"));
    await flush();
    expect(persist).toHaveBeenCalledTimes(1);
  });

  test.each([
    ["navigator.storage missing", undefined],
    ["persist missing", {}],
  ])("%s: startQueue still works", async (_, value) => {
    Object.defineProperty(navigator, "storage", { configurable: true, value });
    await load();
    expect(() => Q.startQueue(new QueryClient())).not.toThrow();
    await Q.enqueue(item(stop("A")));
    await Q.drain();
    expect(await rawAll()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
describe("AC7: QueueNotice + subscribe", () => {
  const loadNotice = async () => (await import("./QueueNotice")).QueueNotice;
  const settle = () => act(() => flush());

  test("subscribe delivers current records and every change", async () => {
    await start();
    const seen: unknown[][] = [];
    const unsub = Q.subscribe((rs) => seen.push(rs));
    await flush();
    expect(seen.at(-1)).toEqual([]);
    handler = netDown;
    const a = stop("A");
    await Q.enqueue(item(a));
    await flush();
    const last = seen.at(-1) as Array<Record<string, unknown>>;
    expect(last).toHaveLength(1);
    expect(last[0]).toMatchObject({ kind: "stop", payload: { slug: "abc", data: a }, failed: false, attempts: 1 });
    expect(typeof last[0].key).toBe("number");
    unsub();
  });

  test("renders nothing with an empty queue", async () => {
    await start();
    const QueueNotice = await loadNotice();
    const { container } = render(<QueueNotice />);
    await settle();
    expect(container.innerHTML).toBe("");
  });

  // t-offline-indicator-pending: deliberately inverted from "renders nothing with
  // only young retrying entries" — a young entry is now counted, but still not listed.
  test("a young retrying entry is counted, not listed individually", async () => {
    await start();
    const QueueNotice = await loadNotice();
    const { container } = render(<QueueNotice />);
    await settle();
    handler = netDown;
    await Q.enqueue(item(stop("Young")));
    await settle();
    expect(container.textContent).toContain("Waiting to send: 1 stop");
    expect(container.textContent).not.toContain("Young");
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /dismiss/i })).toBeNull();
  });

  test("lists failed (with Dismiss) and stuck >=10 attempts (still trying), and updates live", async () => {
    await start();
    const QueueNotice = await loadNotice();
    const bad = stop("Daly Waters Pub");
    const stuck = stop("Larrimah Hotel");
    handler = (p) => (p.body.id === bad.id ? envelope(422, "VALIDATION_ERROR", "name is too long") : netDown());
    await Q.enqueue(item(bad));
    await Q.enqueue(item(stuck));
    await flush();
    const attempts = async () => (await rawById(stuck.id))!.value.attempts as number;
    for (let i = 0; i < 20 && (await attempts()) < 9; i++) await Q.drain();
    expect(await attempts()).toBe(9);

    const { container } = render(<QueueNotice />);
    await settle();
    expect(screen.getByText(/Daly Waters Pub/)).toBeTruthy();
    expect(screen.getByText(/name is too long/)).toBeTruthy();
    expect(screen.queryByText(/Larrimah Hotel/)).toBeNull(); // 9 attempts: not yet shown
    // Failed excluded; the 9-attempt entry is counted.
    expect(container.textContent).toContain("Waiting to send: 1 stop");
    expect(screen.getAllByRole("button", { name: /dismiss/i })).toHaveLength(1);

    await act(() => Q.drain());
    await settle();
    expect(await attempts()).toBe(10);
    const lastError = (await rawById(stuck.id))!.value.lastError as string;
    expect(screen.getByText(/Larrimah Hotel/)).toBeTruthy();
    expect(container.textContent).toMatch(/still trying/i);
    expect(container.textContent).toContain(lastError);
    expect(container.textContent).toContain("Waiting to send: 1 stop"); // counted regardless of attempts
    // A retrying entry is not dismissable: only failed ones are.
    expect(screen.getAllByRole("button", { name: /dismiss/i })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    await settle();
    expect(await rawById(bad.id)).toBeUndefined();
    expect(screen.queryByText(/Daly Waters Pub/)).toBeNull();

    handler = created;
    await act(() => Q.drain());
    await settle();
    expect(await rawAll()).toEqual([]);
    expect(container.innerHTML).toBe("");
  });
});

// ---------------------------------------------------------------------------
test("AC9 (spec §12): network loss, restart, resume: the original id is delivered", async () => {
  await start();
  handler = netDown;
  const a = stop("Tennant Creek");
  await Q.enqueue(item(a));
  await until(() => posts.length === 1);
  await flush();
  const before = await rawAll();
  expect(before).toHaveLength(1);
  expect(before[0].value).toMatchObject({ attempts: 1, failed: false });

  // Restart: brand new module instance, same IndexedDB global.
  posts = [];
  handler = created;
  await load();
  Q.startQueue(new QueryClient());
  await Q.drain();
  await until(async () => (await rawAll()).length === 0, "store drained after restart");
  expect(posts).toHaveLength(1);
  expect(posts[0].url).toBe("/api/trips/abc/stops");
  expect(posts[0].body).toEqual(a);
});

test("AC2/AC5/AC9: startQueue alone drains after restart; failed entries and attempt counts survive it", async () => {
  await start();
  const bad = stop("Bad");
  const retrying = stop("Retrying");
  handler = (p) => (p.body.id === bad.id ? envelope(422, "VALIDATION_ERROR", "bad") : netDown());
  await Q.enqueue(item(bad));
  await Q.enqueue(item(retrying));
  await flush();
  const attemptsBefore = (await rawById(retrying.id))!.value.attempts as number;
  expect(attemptsBefore).toBeGreaterThanOrEqual(1);

  posts = [];
  await load();
  Q.startQueue(new QueryClient()); // no drain() call: startQueue must trigger it
  await until(() => posts.length === 1, "drain from startQueue");
  await flush();
  expect(postedIds()).toEqual([retrying.id]);
  expect((await rawById(bad.id))?.value).toMatchObject({ failed: true, lastError: "bad" });
  expect((await rawById(retrying.id))?.value.attempts).toBe(attemptsBefore + 1);
});

test("AC1: enqueue resolves only after its readwrite transaction has committed", async () => {
  await start();
  handler = () => new Promise<Response>(() => {}); // keep the drain from touching the store
  let committed = 0;
  const orig = IDBDatabase.prototype.transaction;
  vi.spyOn(IDBDatabase.prototype, "transaction").mockImplementation(function (this: IDBDatabase, ...args) {
    const tx = orig.apply(this, args as Parameters<typeof orig>);
    if (tx.mode === "readwrite") tx.addEventListener("complete", () => committed++);
    return tx;
  });
  await Q.enqueue(item(stop("A")));
  expect(committed).toBe(1);
});
