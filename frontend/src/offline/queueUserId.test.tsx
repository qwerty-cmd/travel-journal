import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import type { StopCreate } from "../api/gen/types/StopCreate";
import { setNavigatorLocks } from "./testLocks";

// t-am-queue-userid: dev's driving tests (hold rule + v2 routing). The full
// obligation-14 userId suite is test-writer's. A module reset over the same
// fake IndexedDB is the "reload".

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

let posts: { url: string; id: string; body: unknown }[];
const signIn = (id: string) => localStorage.setItem("btj.me", JSON.stringify({ id, displayName: "Wes" }));

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
const v2Photo = (s: StopCreate, id: string) => ({
  kind: "photo" as const,
  payload: { tripId: "trip-1", stopId: s.id, stopName: s.name, data: { id, takenAt: "2026-10-03T08:41:07+09:30" } },
  file: new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" }),
});
const legacyStop = (data: StopCreate) => ({ kind: "stop" as const, payload: { slug: "abc", data } });

// ---- test-writer harness additions (follows queueClassification.test.tsx) ----
type Post = { url: string; id: string; body: unknown };
let handler: (p: Post) => Response | Promise<Response>;
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (status: number, code: string, message: string, headers?: Record<string, string>) =>
  json(status, { error: { code, message } }, headers);
const ok = () => json(201, {});
const path = (p: { url: string }) => new URL(p.url, "https://x").pathname;
async function until(cond: () => boolean | Promise<boolean>, what = "condition") {
  for (let i = 0; i < 500; i++) {
    if (await cond()) return;
    await new Promise<void>((r) => setImmediate(r));
  }
  throw new Error(`timed out waiting for ${what}`);
}

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
const meta = async () =>
  (await rawAll()).map((r) => ({
    id: (r.value.payload as { data: { id: string } }).data.id,
    userId: r.value.userId,
    attempts: r.value.attempts,
    lastError: r.value.lastError,
    failed: r.value.failed,
  }));

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
const broadcastAuth = (type: "signin" | "signout") => new BroadcastChannel("auth").postMessage({ type });

// Distinctive ids so "the notice never names the account" can be checked by substring.
const OWNER = "owner-acct-7f3e";
const OTHER = "other-acct-91ab";

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
      const isForm = init.body instanceof FormData;
      const body = isForm ? Object.fromEntries((init.body as FormData).entries()) : JSON.parse(String(init.body));
      const p = { url: String(input), id: String((body as { id: string }).id), body };
      posts.push(p);
      return handler(p);
    }),
  );
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
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

test("v2 entries of the signed-in user go to the v2 clients; slug entries without userId to the legacy ones", async () => {
  signIn("u1");
  await start();
  const a = stop("A");
  const b = stop("B");
  await Q.enqueue([v2Stop(a), v2Photo(a, "p-1")], "u1");
  await Q.enqueue(legacyStop(b));
  await flush();
  expect(posts.map((p) => [new URL(p.url, "https://x").pathname, p.id])).toEqual([
    ["/api/v2/trips/trip-1/stops", a.id],
    [`/api/v2/trips/trip-1/stops/${a.id}/photos`, "p-1"],
    ["/api/trips/abc/stops", b.id],
  ]);
});

test("another account's or a signed-out entry is held: not sent, not failed, attempts 0, counted in the notice; later entries still go", async () => {
  signIn("u2");
  await start();
  const { QueueNotice } = await import("./QueueNotice");
  const a = stop("A");
  const b = stop("B");
  await Q.enqueue(v2Stop(a), "u1");
  await Q.enqueue(legacyStop(b));
  render(<QueueNotice />);
  await act(() => flush());
  expect(posts.map((p) => p.id)).toEqual([b.id]);
  const held = await new Promise<import("./queue").QueueRecord[]>((r) => {
    const off = Q.subscribe((e) => (off(), r(e)));
  });
  expect(held).toHaveLength(1);
  expect(held[0]).toMatchObject({ userId: "u1", failed: false, attempts: 0, lastError: null });
  expect(screen.getByText(/1 item was saved by another account on this phone/)).toBeTruthy();
  expect(screen.queryByText(/Waiting to send/)).toBeNull();

  localStorage.removeItem("btj.me");
  await Q.drain();
  expect(posts).toHaveLength(1);
});

test("the hold survives a reload and lifts once the owning account is signed in", async () => {
  signIn("u2");
  await start();
  const a = stop("A");
  await Q.enqueue(v2Stop(a), "u1");
  await flush();
  expect(posts).toHaveLength(0);

  await start(); // reload over the same IndexedDB
  expect(posts).toHaveLength(0);
  signIn("u1");
  await Q.drain();
  expect(posts.map((p) => p.id)).toEqual([a.id]);
});

// ---------------------------------------------------------------------------
// test-writer: obligation-14 userId suite, written from the AC and
// docs/api-contract.md "Offline-queue classification (Entry 29)" + DESIGN.md
// "Held for another account".

const v2PhotoNamed = (s: StopCreate, id: string) => v2Photo(s, id);
const legacyPhoto = (s: StopCreate, id: string) => ({
  kind: "photo" as const,
  payload: { slug: "abc", stopId: s.id, stopName: s.name, data: { id, uploadedBy: "Wes", takenAt: "2026-10-03T08:41:07+09:30" } },
  file: new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" }),
});
const settle = () => act(() => flush());
const notice = () => screen.queryByRole("region", { name: "Unsent stops" });

// Every drain trigger the contract names, then 600 s of fake time.
async function hammer() {
  for (let i = 0; i < 5; i++) await Q.drain();
  window.dispatchEvent(new Event("online"));
  document.dispatchEvent(new Event("visibilitychange"));
  await flush();
  await vi.advanceTimersByTimeAsync(600_000);
  await flush();
}

describe("hold", () => {
  test.each([
    ["another account is signed in", () => signIn(OTHER)],
    ["nobody is signed in", () => {}],
  ])("%s: not sent, not failed, attempts/lastError untouched across drains and 600 s", async (_label, who) => {
    who();
    await start();
    const a = stop("A");
    await Q.enqueue([v2Stop(a), v2PhotoNamed(a, "p-h1")], OWNER);
    await hammer();
    expect(posts).toEqual([]);
    expect(await meta()).toEqual([
      { id: a.id, userId: OWNER, attempts: 0, lastError: null, failed: false },
      { id: "p-h1", userId: OWNER, attempts: 0, lastError: null, failed: false },
    ]);
  });

  test("held entries are counted only in the held line, which never names the account", async () => {
    signIn(OTHER);
    await start();
    const { QueueNotice } = await import("./QueueNotice");
    const a = stop("Held stop");
    const b = stop("Mine");
    handler = (p) => (p.id === b.id ? envelope(500, "INTERNAL_ERROR", "boom") : ok());
    await Q.enqueue([v2Stop(a), v2PhotoNamed(a, "p-h2")], OWNER);
    await Q.enqueue(v2Stop(b), OTHER);
    render(<QueueNotice />);
    await settle();
    const text = notice()!.textContent!;
    expect(text).toContain("Waiting to send: 1 stop");
    expect(text).not.toMatch(/photo/);
    expect(text).toContain("2 items were saved by another account on this phone. Sign in as that account to send them.");
    expect(text).not.toContain(OWNER);
    expect(text).not.toContain("Held stop");
    expect(screen.queryByRole("button", { name: /sign in/i })).toBeNull();
  });
});

test("legacy entries without a userId are sent while signed out", async () => {
  await start();
  const a = stop("A");
  await Q.enqueue([legacyStop(a), legacyPhoto(a, "p-l1")]);
  await flush();
  expect(posts.map((p) => [path(p), p.id])).toEqual([
    ["/api/trips/abc/stops", a.id],
    [`/api/trips/abc/stops/${a.id}/photos`, "p-l1"],
  ]);
  expect(await rawAll()).toEqual([]);
});

test("interleaving: held entries don't block later current-user or legacy entries, in key order", async () => {
  signIn(OTHER);
  await start();
  const h1 = stop("H1");
  const m = stop("M");
  const l = stop("L");
  const h2 = stop("H2");
  await Q.enqueue(v2Stop(h1), OWNER);
  await Q.enqueue([v2Stop(m), v2PhotoNamed(m, "p-m")], OTHER);
  await Q.enqueue(legacyStop(l));
  await Q.enqueue(v2Stop(h2), OWNER);
  await Q.enqueue(legacyPhoto(l, "p-l"));
  await flush();
  expect(posts.map((p) => p.id)).toEqual([m.id, "p-m", l.id, "p-l"]);
  expect((await meta()).map((e) => [e.id, e.attempts])).toEqual([
    [h1.id, 0],
    [h2.id, 0],
  ]);
});

describe("corrupt btj.me is treated as signed out", () => {
  test.each([
    ["not JSON", "{oops"],
    ["null", "null"],
    ["id not a string", JSON.stringify({ id: 7, displayName: "Wes" })],
    ["right id but no displayName", JSON.stringify({ id: "owner-acct-7f3e" })],
  ])("%s: userId entries held, legacy entries sent", async (_l, raw) => {
    localStorage.setItem("btj.me", raw);
    await start();
    const a = stop("A");
    const b = stop("B");
    await Q.enqueue(v2Stop(a), OWNER);
    await Q.enqueue(legacyStop(b));
    await hammer();
    expect(posts.map((p) => p.id)).toEqual([b.id]);
    expect(await meta()).toEqual([{ id: a.id, userId: OWNER, attempts: 0, lastError: null, failed: false }]);
  });
});

describe("auth broadcasts", () => {
  test("signin as the owning account releases held entries without any other trigger; signin as someone else doesn't", async () => {
    useRealBroadcastChannel();
    await start();
    const a = stop("A");
    await Q.enqueue([v2Stop(a), v2PhotoNamed(a, "p-b1")], OWNER);
    await flush();
    expect(posts).toEqual([]);

    signIn(OTHER);
    broadcastAuth("signin");
    await flush();
    expect(posts).toEqual([]);

    signIn(OWNER);
    broadcastAuth("signin");
    await until(() => posts.length === 2, "held entries sent after signin");
    expect(posts.map((p) => p.id)).toEqual([a.id, "p-b1"]);
    await until(async () => (await rawAll()).length === 0, "queue emptied");
  });

  test("signout: the user's pending entries become held, the notice switches lines, and nothing is sent afterwards", async () => {
    useRealBroadcastChannel();
    signIn(OWNER);
    await start();
    const { QueueNotice } = await import("./QueueNotice");
    const a = stop("A");
    handler = () => envelope(500, "INTERNAL_ERROR", "boom");
    await Q.enqueue(v2Stop(a), OWNER);
    render(<QueueNotice />);
    await settle();
    expect(posts).toHaveLength(1);
    expect(notice()!.textContent).toContain("Waiting to send: 1 stop");

    handler = ok;
    localStorage.removeItem("btj.me");
    broadcastAuth("signout");
    await settle();
    await act(() => until(() => /saved by another account/.test(notice()?.textContent ?? ""), "held line after signout"));
    expect(notice()!.textContent).not.toContain("Waiting to send");
    expect(notice()!.textContent).toContain("1 item was saved by another account on this phone.");

    await hammer(); // the 5 s backoff timer included
    expect(posts).toHaveLength(1);
    expect(await meta()).toEqual([{ id: a.id, userId: OWNER, attempts: 1, lastError: expect.any(String), failed: false }]);
  });
});

test("reload while signed out: the hold, its untouched metadata and the held line all survive", async () => {
  await start();
  const a = stop("A");
  await Q.enqueue([v2Stop(a), v2PhotoNamed(a, "p-r1")], OWNER);
  await flush();
  const before = await meta();

  await start(); // reload over the same IndexedDB
  const { QueueNotice } = await import("./QueueNotice");
  render(<QueueNotice />);
  await hammer();
  await settle();
  expect(posts).toEqual([]);
  expect(await meta()).toEqual(before);
  expect(notice()!.textContent).toContain("2 items were saved by another account on this phone.");
});

describe("routing", () => {
  test("{tripId} payloads go through the v2 clients with ids and bodies preserved; the userId is never sent", async () => {
    signIn(OWNER);
    await start();
    const a = stop("A");
    await Q.enqueue([v2Stop(a), v2PhotoNamed(a, "photo-id-v2")], OWNER);
    await flush();
    expect(posts.map((p) => [path(p), p.id])).toEqual([
      ["/api/v2/trips/trip-1/stops", a.id],
      [`/api/v2/trips/trip-1/stops/${a.id}/photos`, "photo-id-v2"],
    ]);
    expect(posts[0].body).toEqual(a);
    expect(posts[1].body).toMatchObject({ id: "photo-id-v2", takenAt: "2026-10-03T08:41:07+09:30" });
    expect(posts[1].body).toHaveProperty("file");
    expect(JSON.stringify(posts.map((p) => p.body))).not.toContain(OWNER);
  });

  test("slug payloads go through the legacy clients with ids preserved", async () => {
    signIn(OWNER);
    await start();
    const a = stop("A");
    await Q.enqueue([legacyStop(a), legacyPhoto(a, "photo-id-legacy")]);
    await flush();
    expect(posts.map((p) => [path(p), p.id])).toEqual([
      ["/api/trips/abc/stops", a.id],
      [`/api/trips/abc/stops/${a.id}/photos`, "photo-id-legacy"],
    ]);
    expect(posts[0].body).toEqual(a);
    expect(posts[1].body).toMatchObject({ id: "photo-id-legacy", uploadedBy: "Wes" });
  });
});

test("held stop with photos: photos are held too, and on release the stop is sent before them", async () => {
  useRealBroadcastChannel();
  signIn(OTHER);
  await start();
  const a = stop("A");
  const mine = stop("Mine");
  await Q.enqueue([v2Stop(a), v2PhotoNamed(a, "p-s1"), v2PhotoNamed(a, "p-s2")], OWNER);
  await Q.enqueue(v2Stop(mine), OTHER);
  await hammer();
  expect(posts.map((p) => p.id)).toEqual([mine.id]);

  signIn(OWNER);
  broadcastAuth("signin");
  await until(() => posts.length === 4, "released");
  expect(posts.slice(1).map((p) => [path(p), p.id])).toEqual([
    ["/api/v2/trips/trip-1/stops", a.id],
    [`/api/v2/trips/trip-1/stops/${a.id}/photos`, "p-s1"],
    [`/api/v2/trips/trip-1/stops/${a.id}/photos`, "p-s2"],
  ]);
});

describe("hold combined with 401 pause and 429 wait", () => {
  test("401 on the current user's entry pauses it; the held entry stays untouched and is counted only in the held line", async () => {
    useRealBroadcastChannel();
    signIn(OTHER);
    await start();
    const { QueueNotice } = await import("./QueueNotice");
    const h = stop("H");
    const m = stop("M");
    handler = () => envelope(401, "UNAUTHENTICATED", "Sign in to continue");
    await Q.enqueue(v2Stop(h), OWNER);
    await Q.enqueue(v2Stop(m), OTHER);
    render(<QueueNotice />);
    await settle();
    expect(posts.map((p) => p.id)).toEqual([m.id]);
    expect(Q.isPaused()).toBe(true);
    const text = notice()!.textContent!;
    expect(text).toContain("Sign in to send 1 item");
    expect(text).toContain("1 item was saved by another account on this phone.");
    expect((await meta()).map((e) => [e.id, e.attempts, e.failed])).toEqual([
      [h.id, 0, false],
      [m.id, 0, false],
    ]);

    handler = ok;
    broadcastAuth("signin"); // same account signs back in
    await until(() => posts.length === 2, "resumed");
    await hammer();
    expect(posts.map((p) => p.id)).toEqual([m.id, m.id]);
    expect(Q.isPaused()).toBe(false);
    expect(await meta()).toEqual([{ id: h.id, userId: OWNER, attempts: 0, lastError: null, failed: false }]);
  });

  test("429 wait: the held entry is untouched; switching account mid-wait sends the released entry and the timer doesn't send the newly held one", async () => {
    useRealBroadcastChannel();
    signIn(OTHER);
    await start();
    const h = stop("H");
    const m = stop("M");
    handler = (p) => (p.id === m.id ? envelope(429, "RATE_LIMITED", "Too many requests", { "Retry-After": "7" }) : ok());
    await Q.enqueue(v2Stop(h), OWNER);
    await Q.enqueue(v2Stop(m), OTHER);
    await flush();
    expect(posts.map((p) => p.id)).toEqual([m.id]);
    expect((await meta()).map((e) => [e.id, e.attempts])).toEqual([
      [h.id, 0],
      [m.id, 1],
    ]);

    // Retry-After honoured for the current user's entry while the held one waits untouched.
    handler = ok;
    await vi.advanceTimersByTimeAsync(6_999);
    await flush();
    expect(posts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await until(() => posts.length === 2, "retry at 7 s");
    expect(await meta()).toEqual([{ id: h.id, userId: OWNER, attempts: 0, lastError: null, failed: false }]);
  });

  test("429 wait then sign-in as the held entry's owner: it goes now, and the rate-limited entry is held from then on", async () => {
    useRealBroadcastChannel();
    signIn(OTHER);
    await start();
    const h = stop("H");
    const m = stop("M");
    handler = (p) => (p.id === m.id ? envelope(429, "RATE_LIMITED", "Too many requests", { "Retry-After": "7" }) : ok());
    await Q.enqueue(v2Stop(h), OWNER);
    await Q.enqueue(v2Stop(m), OTHER);
    await flush();

    handler = ok;
    signIn(OWNER);
    broadcastAuth("signin");
    await until(() => posts.length === 2, "owner's entry sent on signin");
    await hammer(); // the 7 s Retry-After timer fires in here
    expect(posts.map((p) => p.id)).toEqual([m.id, h.id]);
    expect(await meta()).toEqual([{ id: m.id, userId: OTHER, attempts: 1, lastError: "Too many requests", failed: false }]);
  });
});
