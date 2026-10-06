import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import type { StopCreate } from "../api/gen/types/StopCreate";
import { setNavigatorLocks } from "./testLocks";

// t-am-fe-rider-add-stop, queue-timing half (Added AC from t-am-queue-classification QA).
// Written from the AC, docs/api-contract.md "Offline-queue classification (Entry 29)"
// and orchestrator ruling 1: a server Retry-After is ended by nothing but its own
// timer; a backoff wait may be ended early by `online`, `visibilitychange`, `drain()`.
// Harness follows queueClassification.test.tsx / queueUserId.test.tsx.

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
const limited = (retryAfter: string) => envelope(429, "RATE_LIMITED", "Too many requests", { "Retry-After": retryAfter });
const boom = () => envelope(500, "INTERNAL_ERROR", "boom");
const ok = () => json(201, {});
const postedIds = () => posts.map((p) => p.id);
/** Per-id scripted replies; an id with no script left gets 201. */
function script(replies: Record<string, Array<() => Response | Promise<Response>>>) {
  handler = (p) => (replies[p.id]?.shift() ?? ok)();
}

// ---- raw IndexedDB access, independent of queue.ts ----
function rawCount(): Promise<number> {
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
      req.onerror = () => reject(req.error);
    };
  });
}

// ---- fixtures ----
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
const v2Stop = (data: StopCreate) => ({ kind: "stop" as const, payload: { tripId: "trip-1", data } });
const signIn = (id: string) => localStorage.setItem("btj.me", JSON.stringify({ id, displayName: "Wes" }));
const U1 = "u1";
const U2 = "u2";

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
const broadcastSignin = () => new BroadcastChannel("auth").postMessage({ type: "signin" });

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
const tick = async (ms: number) => {
  await vi.advanceTimersByTimeAsync(ms);
  await flush();
};
const fireVisibility = () => document.dispatchEvent(new Event("visibilitychange"));
const fireOnline = () => window.dispatchEvent(new Event("online"));

// ---------------------------------------------------------------------------
describe("a pending Retry-After is ended by nothing but its own timer", () => {
  const triggers: Array<[string, () => unknown]> = [
    ["visibilitychange", fireVisibility],
    ["online", fireOnline],
    ["drain()", () => void Q.drain()],
  ];
  for (const [name, fire] of triggers) {
    test(`${name} during Retry-After: 7 does not send early; the entry goes at exactly 7 s`, async () => {
      signIn(U1);
      await start();
      const a = stop("A");
      script({ [a.id]: [() => limited("7")] });
      await Q.enqueue(v2Stop(a), U1);
      await flush();
      expect(postedIds()).toEqual([a.id]);

      await tick(3_000);
      fire();
      await flush();
      expect(postedIds(), `${name} at 3 s`).toEqual([a.id]);
      await expectRetryAt(4_000); // 7 s after the 429, not 7 s after the trigger
      await until(async () => (await rawCount()) === 0, "A sent");
    });
  }

  test("a new enqueue during the wait sends neither entry early; both go at 7 s, FIFO", async () => {
    signIn(U1);
    await start();
    const a = stop("A");
    const b = stop("B");
    script({ [a.id]: [() => limited("7")] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();

    await tick(2_000);
    await Q.enqueue(v2Stop(b), U1);
    await flush();
    expect(postedIds(), "B must not overtake waiting A").toEqual([a.id]);

    await expectRetryAt(5_000);
    await until(() => posts.length === 3, "B after A");
    expect(postedIds()).toEqual([a.id, a.id, b.id]);
    await until(async () => (await rawCount()) === 0, "both sent");
  });

  test("the `auth` signin broadcast during the wait does not send early", async () => {
    useRealBroadcastChannel();
    signIn(U1);
    await start();
    const a = stop("A");
    script({ [a.id]: [() => limited("7")] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();

    await tick(1_000);
    broadcastSignin();
    await flush();
    expect(postedIds()).toEqual([a.id]);
    await expectRetryAt(6_000);
  });

  test("the drain's own `again` loop (triggers arriving while the 429 is in flight) does not skip the wait", async () => {
    signIn(U1);
    await start();
    const a = stop("A");
    let release!: (r: Response) => void;
    script({ [a.id]: [() => new Promise<Response>((r) => (release = r))] });
    await Q.enqueue(v2Stop(a), U1);
    await until(() => posts.length === 1, "A in flight");

    // Triggers land mid-drain, so the drain loops `again` as soon as the 429 is handled.
    fireVisibility();
    fireOnline();
    void Q.drain();
    await flush();
    release(limited("7"));
    await flush();
    expect(postedIds(), "again-loop must not resend A").toEqual([a.id]);
    await expectRetryAt(7_000);
  });

  test("all triggers together across the wait: still exactly 7 s", async () => {
    useRealBroadcastChannel();
    signIn(U1);
    await start();
    const a = stop("A");
    const b = stop("B");
    script({ [a.id]: [() => limited("7")] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();
    await tick(1_000);
    fireVisibility();
    await tick(1_000);
    fireOnline();
    await tick(1_000);
    await Q.enqueue(v2Stop(b), U1);
    await tick(1_000);
    broadcastSignin();
    await tick(1_000);
    void Q.drain();
    await flush();
    expect(postedIds()).toEqual([a.id]);
    await expectRetryAt(2_000);
    await until(() => posts.length === 3, "B after A");
    expect(postedIds()).toEqual([a.id, a.id, b.id]);
  });
});

// ---------------------------------------------------------------------------
describe("held entries", () => {
  test("a Retry-After on another account's (held) entry does not block a later entry of the signed-in account", async () => {
    useRealBroadcastChannel();
    signIn(U1);
    await start();
    const a = stop("A");
    const b = stop("B");
    script({ [a.id]: [() => limited("7")] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();
    expect(postedIds()).toEqual([a.id]);

    // Account switch: A (u1) is now held, and still has 6 s of Retry-After left.
    await tick(1_000);
    signIn(U2);
    broadcastSignin();
    await flush();
    await Q.enqueue(v2Stop(b), U2);
    await until(() => posts.length === 2, "B sent despite held A waiting");
    expect(postedIds()).toEqual([a.id, b.id]);

    // Held A is never sent while u2 is signed in, even after its wait expires.
    await tick(60_000);
    expect(postedIds()).toEqual([a.id, b.id]);
  });

  test("the owner signing back in mid-wait does not skip the remaining Retry-After", async () => {
    useRealBroadcastChannel();
    signIn(U1);
    await start();
    const a = stop("A");
    script({ [a.id]: [() => limited("7")] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();

    await tick(1_000);
    signIn(U2);
    broadcastSignin();
    await tick(1_000);
    signIn(U1);
    broadcastSignin();
    await flush();
    expect(postedIds(), "u1 signin at 2 s must not end A's Retry-After").toEqual([a.id]);
    await expectRetryAt(5_000);
  });
});

// ---------------------------------------------------------------------------
describe("mixed waits and ruling 1", () => {
  test("Retry-After on one entry, then a backoff on the next: each keeps its own timing", async () => {
    signIn(U1);
    await start();
    const a = stop("A");
    const b = stop("B");
    script({ [a.id]: [() => limited("7")], [b.id]: [boom, boom] });
    await Q.enqueue([v2Stop(a), v2Stop(b)], U1);
    await flush();
    expect(postedIds(), "B queued behind waiting A").toEqual([a.id]);

    await expectRetryAt(7_000); // A: 201
    await until(() => posts.length === 3, "B right after A");
    expect(postedIds()).toEqual([a.id, a.id, b.id]); // B: 500 → backoff 5 s
    await flush();
    await expectRetryAt(5_000); // B: 500 → 10 s
    await expectRetryAt(10_000); // B: 201
    await until(async () => (await rawCount()) === 0, "both sent");
  });

  test("`online` ends a backoff wait but not a later Retry-After on the same entry", async () => {
    signIn(U1);
    await start();
    const a = stop("A");
    script({ [a.id]: [boom, () => limited("7")] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();
    expect(posts).toHaveLength(1); // 500 → backoff 5 s

    await tick(1_000);
    fireOnline();
    await until(() => posts.length === 2, "online ends the backoff"); // 429 Retry-After: 7
    await flush(); // let the 429 be handled (its timer armed) before the clock moves
    await tick(1_000);
    fireOnline();
    await flush();
    expect(posts, "online must not end a Retry-After").toHaveLength(2);
    await expectRetryAt(6_000);
    await until(async () => (await rawCount()) === 0, "A sent");
  });

  test("`online` ends a backoff on a later entry only after the Retry-After ahead of it expires (FIFO)", async () => {
    signIn(U1);
    await start();
    const a = stop("A");
    const b = stop("B");
    script({ [a.id]: [() => limited("7")], [b.id]: [boom] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();
    await Q.enqueue(v2Stop(b), U1);
    await flush();
    fireOnline();
    await flush();
    expect(postedIds(), "online does not end A's Retry-After, so B stays behind it").toEqual([a.id]);
    await expectRetryAt(7_000);
    await until(() => posts.length === 3, "B after A");
    await tick(1_000);
    fireOnline(); // B is in backoff now: online ends it
    await until(() => posts.length === 4, "online ends B's backoff");
    expect(postedIds()).toEqual([a.id, a.id, b.id, b.id]);
  });
});

// ---------------------------------------------------------------------------
describe("401 pause", () => {
  test("signin clears a 401 pause and sends at once, even if the entry had a Retry-After before", async () => {
    useRealBroadcastChannel();
    signIn(U1);
    await start();
    const a = stop("A");
    script({ [a.id]: [() => limited("7"), unauth] });
    await Q.enqueue(v2Stop(a), U1);
    await flush();
    await expectRetryAt(7_000); // 401 → paused
    expect(Q.isPaused()).toBe(true);

    await tick(600_000);
    expect(posts, "paused: no timer resends").toHaveLength(2);
    broadcastSignin();
    await until(() => posts.length === 3, "signin resumes the drain");
    await until(async () => (await rawCount()) === 0, "A sent");
    expect(Q.isPaused()).toBe(false);
  });

  test("signin clears a 401 pause on a fresh v2 entry", async () => {
    useRealBroadcastChannel();
    signIn(U1);
    await start();
    const a = stop("A");
    const b = stop("B");
    script({ [a.id]: [unauth] });
    await Q.enqueue([v2Stop(a), v2Stop(b)], U1);
    await flush();
    expect(postedIds()).toEqual([a.id]);
    expect(Q.isPaused()).toBe(true);
    broadcastSignin();
    await until(async () => (await rawCount()) === 0, "both sent after signin");
    expect(postedIds()).toEqual([a.id, a.id, b.id]);
  });
});
