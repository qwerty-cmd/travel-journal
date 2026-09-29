import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import type { StopCreate } from "../api/gen/types/StopCreate";

// Cross-tab queue: two tabs are two module instances over one fake IndexedDB,
// joined by an in-memory Web Locks and BroadcastChannel (jsdom has neither;
// Node's real BroadcastChannel is replaced so nothing leaks between tests).

type QueueMod = typeof import("./queue");
type Tab = { Q: QueueMod; QueueNotice: (typeof import("./QueueNotice"))["QueueNotice"] };

async function openTab(): Promise<Tab> {
  vi.resetModules();
  const Q = await import("./queue");
  const { QueueNotice } = await import("./QueueNotice");
  Q.startQueue(new QueryClient());
  await flush();
  return { Q, QueueNotice };
}

const setImmediate = (globalThis as unknown as { setImmediate: (f: () => void) => void }).setImmediate;
const flush = async (n = 60) => {
  for (let i = 0; i < n; i++) await new Promise<void>((r) => setImmediate(r));
};

// ---- in-memory Web Locks: exclusive, ifAvailable only ----
class FakeLocks {
  held = new Set<string>();
  async request(name: string, opts: LockOptions, cb: (lock: Lock | null) => Promise<unknown>) {
    if (!opts.ifAvailable) throw new Error("fake supports ifAvailable only");
    if (this.held.has(name)) return cb(null);
    this.held.add(name);
    try {
      return await cb({ name, mode: "exclusive" } as Lock);
    } finally {
      this.held.delete(name);
    }
  }
}

// ---- in-memory BroadcastChannel: async delivery to every other instance ----
let channels: FakeChannel[];
class FakeChannel {
  onmessage: ((e: MessageEvent) => void) | null = null;
  constructor(public name: string) {
    channels.push(this);
  }
  postMessage(data: unknown) {
    for (const c of channels) {
      if (c !== this && c.name === this.name) setImmediate(() => c.onmessage?.({ data } as MessageEvent));
    }
  }
  close() {}
}

// ---- fetch: every POST recorded; a gate can hold responses open ----
let posts: string[];
let handler: (body: StopCreate, signal?: AbortSignal) => Promise<Response>;
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const created = async (b: StopCreate) => json(201, { ...b, notes: b.notes ?? null });
const netDown = () => Promise.reject(new TypeError("Failed to fetch"));
// A request that never settles on its own; like real fetch, it rejects with the signal's reason on abort.
const hang = (_: StopCreate, signal?: AbortSignal) =>
  new Promise<Response>((_, reject) => signal?.addEventListener("abort", () => reject(signal.reason)));
function gate() {
  let open!: () => void;
  const opened = new Promise<void>((r) => (open = r));
  return { open, handler: async (b: StopCreate) => (await opened, created(b)) };
}

let n = 0;
const stop = (name: string): StopCreate => ({
  id: `00000000-0000-4000-9000-${String(++n).padStart(12, "0")}`,
  name,
  lat: -16.4,
  lng: 133.4,
  locationSource: "gps",
  arrivedAt: "2026-10-03T08:30:00Z",
  notes: null,
});
const item = (data: StopCreate) => ({ kind: "stop" as const, payload: { slug: "abc", data } });

async function stored(): Promise<number> {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open("btj-queue");
    r.onerror = () => reject(r.error);
    r.onsuccess = () => {
      const db = r.result;
      const req = db.transaction("entries", "readonly").objectStore("entries").count();
      req.onsuccess = () => {
        db.close();
        resolve(req.result);
      };
    };
  });
}

let locks: FakeLocks;
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("indexedDB", new IDBFactory());
  channels = [];
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  locks = new FakeLocks();
  Object.defineProperty(navigator, "locks", { configurable: true, value: locks });
  posts = [];
  handler = created;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init!.body)) as StopCreate;
      posts.push(body.id);
      return handler(body, init!.signal ?? undefined);
    }),
  );
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
  channels = [];
  Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Web Locks: one draining tab", () => {
  test("two tabs draining at once send an entry exactly once", async () => {
    const A = await openTab();
    const B = await openTab();
    const g = gate();
    handler = g.handler;
    const x = stop("Katherine");
    await A.Q.enqueue(item(x)); // A drains and holds the lock while its POST is open
    await flush();
    await B.Q.drain(); // B can't take the lock: skipped, not a second send
    await flush();
    expect(posts).toEqual([x.id]);
    g.open();
    await flush();
    expect(posts).toEqual([x.id]);
    expect(await stored()).toBe(0);
  });

  test("an entry enqueued in the other tab mid-drain is sent by the lock holder", async () => {
    const A = await openTab();
    const B = await openTab();
    const g = gate();
    handler = g.handler;
    const x = stop("Mataranka");
    const y = stop("Larrimah");
    await A.Q.enqueue(item(x));
    await flush();
    await B.Q.enqueue(item(y)); // B's own drain is skipped: A holds the lock
    await flush();
    expect(posts).toEqual([x.id]);
    g.open();
    await flush();
    expect(posts).toEqual([x.id, y.id]);
    expect(await stored()).toBe(0);
  });

  test("the lock is released after a drain, so the other tab can drain next", async () => {
    const A = await openTab();
    const B = await openTab();
    handler = netDown;
    const x = stop("Daly Waters");
    await A.Q.enqueue(item(x));
    await flush();
    expect(locks.held.size).toBe(0);
    handler = created;
    await B.Q.drain();
    await flush();
    expect(posts.filter((id) => id === x.id).length).toBeGreaterThanOrEqual(2);
    expect(await stored()).toBe(0);
  });
});

describe("request timeout: a hung send can't hold the lock", () => {
  test("a never-settling POST times out at 30s, stays queued with the attempt counted, and releases the lock", async () => {
    const A = await openTab();
    const B = await openTab();
    handler = hang;
    const x = stop("Three Ways");
    await A.Q.enqueue(item(x));
    await flush();
    expect(posts).toEqual([x.id]);
    expect(locks.held.size).toBe(1);

    await vi.advanceTimersByTimeAsync(29_999);
    await flush();
    expect(locks.held.size).toBe(1); // still in flight just before the timeout

    await vi.advanceTimersByTimeAsync(1);
    await flush();
    expect(locks.held.size).toBe(0);
    const rows = await new Promise<import("./queue").QueueRecord[]>((resolve) => {
      const unsub = B.Q.subscribe((rs) => {
        resolve(rs);
        queueMicrotask(unsub);
      });
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ attempts: 1, failed: false });
    expect(typeof rows[0].lastError).toBe("string");

    handler = created;
    await B.Q.drain(); // the other tab can take the lock and send
    await flush();
    expect(posts).toEqual([x.id, x.id]);
    expect(await stored()).toBe(0);
  });
});

describe("BroadcastChannel: other tabs' notices re-read", () => {
  test("a QueueNotice refreshes when another tab enqueues, and again when it drains", async () => {
    const A = await openTab();
    const B = await openTab();
    const { container } = render(<A.QueueNotice />);
    await act(() => flush());
    expect(container.innerHTML).toBe("");

    handler = netDown;
    await B.Q.enqueue(item(stop("Tennant Creek")));
    await act(() => flush());
    expect(container.textContent).toContain("Waiting to send: 1 stop");

    handler = created;
    await B.Q.drain();
    await act(() => flush());
    expect(container.innerHTML).toBe("");
  });

  test("a dismiss in one tab clears the entry from the other tab's notice", async () => {
    const A = await openTab();
    const B = await openTab();
    const { container, getByText } = render(<A.QueueNotice />);
    handler = async () => json(422, { error: { code: "VALIDATION_ERROR", message: "name is too long" } });
    await B.Q.enqueue(item(stop("Renner Springs")));
    await act(() => flush());
    expect(getByText(/name is too long/)).toBeTruthy();
    const key = await new Promise<number>((resolve) => {
      const unsub = B.Q.subscribe((rs) => {
        if (rs.length) {
          resolve(rs[0].key);
          queueMicrotask(unsub);
        }
      });
    });
    await B.Q.dismiss(key);
    await act(() => flush());
    expect(container.innerHTML).toBe("");
  });
});
