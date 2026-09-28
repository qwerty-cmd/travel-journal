import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey } from "../api/gen/hooks/useListPhotosApiTripsSlugStopsStopIdPhotosGet";
import type { StopCreate } from "../api/gen/types/StopCreate";

// t-offline-queue-photos. Written from the AC and docs/api-contract.md
// (POST /trips/{slug}/stops/{id}/photos, Idempotency, Error envelope) — not
// from queue.ts. Harness follows queue.test.tsx: fresh module + fresh fake
// IndexedDB per test; a module reset over the same IndexedDB is the "restart".

type QueueMod = typeof import("./queue");
let Q: QueueMod;
let qc: QueryClient;

async function load() {
  vi.resetModules();
  Q = await import("./queue");
}
async function start() {
  await load();
  qc = new QueryClient();
  Q.startQueue(qc);
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

// ---- fetch stub: stop POSTs carry JSON, photo POSTs carry FormData ----
type Post = { url: string; id: string; json?: StopCreate; form?: FormData };
let posts: Post[];
let handler: (p: Post) => Response | Promise<Response>;
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const envelope = (status: number, code: string, message: string) => json(status, { error: { code, message } });
const netDown = () => Promise.reject(new TypeError("Failed to fetch"));
const isPhoto = (p: Post) => p.form !== undefined;
const created = (p: Post, status = 201) =>
  isPhoto(p)
    ? json(status, {
        id: p.id,
        stopId: p.url.split("/")[5],
        url: `https://r2.example/${p.id}`,
        uploadedBy: p.form!.get("uploadedBy"),
        takenAt: p.form!.get("takenAt"),
        archived: false,
      })
    : json(status, { ...p.json, notes: p.json!.notes ?? null });

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

// ---- fixtures ----
let n = 0;
const uuid = () => `00000000-0000-4000-8000-${String(++n).padStart(12, "0")}`;
const stop = (name: string): StopCreate => ({
  id: uuid(),
  name,
  lat: -16.4,
  lng: 133.4,
  locationSource: "gps",
  arrivedAt: "2026-10-03T08:30:00Z",
  notes: null,
});
const stopItem = (data: StopCreate, slug = "abc") => ({ kind: "stop" as const, payload: { slug, data } });

// Bytes that break any text round-trip (NUL, 0xFF, invalid UTF-8) and aren't all one value.
const jpegBytes = (seed: number) => {
  const b = new Uint8Array(2048);
  for (let i = 0; i < b.length; i++) b[i] = (i * 31 + seed * 7) & 0xff;
  b.set([0xff, 0xd8, 0xff, 0xe0, 0x00, 0xc3, 0x28], 0);
  return b;
};
type Photo = { id: string; uploadedBy: string; takenAt: string; bytes: Uint8Array<ArrayBuffer>; stopId: string; stopName: string };
const photo = (s: StopCreate, seed = 1): Photo => ({
  id: uuid(),
  uploadedBy: "Wes",
  takenAt: "2026-10-03T08:41:07+09:30",
  bytes: jpegBytes(seed),
  stopId: s.id,
  stopName: s.name,
});
const photoItem = (p: Photo, slug = "abc") => ({
  kind: "photo" as const,
  payload: { slug, stopId: p.stopId, stopName: p.stopName, data: { id: p.id, uploadedBy: p.uploadedBy, takenAt: p.takenAt } },
  file: new Blob([p.bytes], { type: "image/jpeg" }),
});

const postedIds = () => posts.map((p) => p.id);
const isArrayBuffer = (v: unknown) => Object.prototype.toString.call(v) === "[object ArrayBuffer]";
const bytesOf = (v: unknown) => Array.from(new Uint8Array(v as ArrayBuffer));
const blobBytes = (b: Blob) =>
  new Promise<number[]>((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(bytesOf(fr.result));
    fr.onerror = () => reject(fr.error);
    fr.readAsArrayBuffer(b);
  });
const photoUrl = (stopId: string, slug = "abc") => `/api/trips/${slug}/stops/${stopId}/photos`;

// ---- environment ----
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("indexedDB", new IDBFactory());
  posts = [];
  handler = created;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "POST") throw new Error(`unstubbed ${init?.method} ${String(input)}`);
      const url = String(input);
      let p: Post;
      if (init.body instanceof FormData) {
        p = { url, id: String(init.body.get("id")), form: init.body };
      } else {
        const body = JSON.parse(String(init.body)) as StopCreate;
        p = { url, id: body.id, json: body };
      }
      posts.push(p);
      return handler(p);
    }),
  );
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  Object.defineProperty(navigator, "storage", { configurable: true, value: { persist: vi.fn(async () => true) } });
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

// Asserts a sent photo request matches the queued photo, field by field and byte by byte.
async function expectPhotoRequest(p: Post | undefined, ph: Photo, slug = "abc") {
  expect(p, `request for photo ${ph.id}`).toBeDefined();
  expect(p!.url).toBe(photoUrl(ph.stopId, slug));
  const f = p!.form!;
  expect(f.get("id")).toBe(ph.id);
  expect(f.get("uploadedBy")).toBe(ph.uploadedBy);
  expect(f.get("takenAt")).toBe(ph.takenAt);
  const file = f.get("file");
  expect(file).toBeInstanceOf(Blob);
  expect((file as Blob).type).toBe("image/jpeg");
  expect(await blobBytes(file as Blob)).toEqual(Array.from(ph.bytes));
}

// ---------------------------------------------------------------------------
describe("AC1: photo storage shape and array enqueue", () => {
  test("a photo is stored with its bytes as an ArrayBuffer, attempts:0, lastError:null, failed:false", async () => {
    await start();
    handler = () => new Promise<Response>(() => {}); // hold the drain so the record is untouched
    const s = stop("Daly Waters Pub");
    const ph = photo(s);
    await Q.enqueue(photoItem(ph));
    const rows = await rawAll();
    expect(rows).toHaveLength(1);
    const { blob, ...rest } = rows[0].value;
    expect(rest).toEqual({
      kind: "photo",
      payload: { slug: "abc", stopId: s.id, stopName: s.name, data: { id: ph.id, uploadedBy: "Wes", takenAt: ph.takenAt } },
      attempts: 0,
      lastError: null,
      failed: false,
    });
    expect(isArrayBuffer(blob)).toBe(true);
    expect(bytesOf(blob)).toEqual(Array.from(ph.bytes));
  });

  test("an array is written in one readwrite transaction, resolves after commit, in order, then drains once", async () => {
    await start();
    handler = () => new Promise<Response>(() => {});
    let committed = 0;
    const orig = IDBDatabase.prototype.transaction;
    vi.spyOn(IDBDatabase.prototype, "transaction").mockImplementation(function (this: IDBDatabase, ...args) {
      const tx = orig.apply(this, args as Parameters<typeof orig>);
      if (tx.mode === "readwrite") tx.addEventListener("complete", () => committed++);
      return tx;
    });
    const s = stop("Larrimah");
    const [p1, p2] = [photo(s, 1), photo(s, 2)];
    await Q.enqueue([stopItem(s), photoItem(p1), photoItem(p2)]);
    expect(committed).toBe(1);
    const rows = await rawAll();
    expect(rows.map((r) => (r.value.payload as { data: { id: string } }).data.id)).toEqual([s.id, p1.id, p2.id]);
    expect(bytesOf(rows[1].value.blob)).toEqual(Array.from(p1.bytes));
    expect(bytesOf(rows[2].value.blob)).toEqual(Array.from(p2.bytes));
    await until(() => posts.length === 1, "drain after array enqueue");
    await flush();
    expect(postedIds()).toEqual([s.id]); // the stop is in flight; nothing else sent concurrently
  });
});

// ---------------------------------------------------------------------------
describe("AC2/AC3: drain sends photos FIFO with stops, deletes + invalidates on 2xx", () => {
  test("interleaved stops and photos go out in enqueue order with the original fields and bytes", async () => {
    await start();
    handler = netDown;
    const a = stop("A");
    const b = stop("B");
    const pa = photo(a, 1);
    const pb1 = photo(b, 2);
    const pb2 = photo(b, 3);
    await Q.enqueue([stopItem(a), photoItem(pa)]);
    await Q.enqueue(stopItem(b));
    await Q.enqueue([photoItem(pb1), photoItem(pb2)]);
    await flush();
    expect(new Set(postedIds())).toEqual(new Set([a.id])); // held at A while retrying
    posts = [];
    handler = created;
    await Q.drain();
    expect(postedIds()).toEqual([a.id, pa.id, b.id, pb1.id, pb2.id]);
    expect(posts[0].url).toBe("/api/trips/abc/stops");
    expect(posts[2].url).toBe("/api/trips/abc/stops");
    await expectPhotoRequest(posts[1], pa);
    await expectPhotoRequest(posts[3], pb1);
    await expectPhotoRequest(posts[4], pb2);
    expect(await rawAll()).toEqual([]);
  });

  test("201 and 200 replay both delete the photo and invalidate that stop's photo list", async () => {
    await start();
    handler = netDown;
    const s1 = stop("S1");
    const s2 = stop("S2");
    const p1 = photo(s1, 1);
    const p2 = photo(s2, 2);
    await Q.enqueue(photoItem(p1, "abc"));
    await Q.enqueue(photoItem(p2, "xyz"));
    await flush();
    const k1 = listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey({ slug: "abc", stop_id: s1.id });
    const k2 = listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey({ slug: "xyz", stop_id: s2.id });
    const other = listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey({ slug: "abc", stop_id: "not-a-queued-stop" });
    for (const k of [k1, k2, other]) qc.setQueryData(k, []);
    handler = (p) => (p.id === p1.id ? created(p, 201) : created(p, 200)); // p2 is a replay
    await Q.drain();
    expect(await rawAll()).toEqual([]);
    expect(posts.at(-1)!.url).toBe(photoUrl(s2.id, "xyz"));
    expect(qc.getQueryState(k1)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(k2)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(other)?.isInvalidated).toBe(false);
  });
});

// ---------------------------------------------------------------------------
describe("AC3/AC4: a photo is never deleted on a non-2xx", () => {
  test.each([
    [422, "VALIDATION_ERROR"],
    [405, "METHOD_NOT_ALLOWED"],
    [409, "CONFLICT"],
    [403, "FORBIDDEN"],
    [404, "NOT_FOUND"],
  ])("never-retry %i %s: failed:true with the envelope message, blob kept, drain continues", async (status, code) => {
    await start();
    const s = stop("S");
    const bad = photo(s, 1);
    const good = photo(s, 2);
    const msg = `server says ${code}`;
    handler = (p) => (p.id === bad.id ? envelope(status, code, msg) : created(p));
    // One enqueue and no explicit drain(): the triggered pass itself must continue past `bad`.
    await Q.enqueue([photoItem(bad), photoItem(good)]);
    await until(() => posts.length === 2, "drain continues past a never-retry photo");
    await flush();
    expect(postedIds()).toEqual([bad.id, good.id]);
    const rows = await rawAll();
    expect(rows).toHaveLength(1);
    expect(rows[0].value).toMatchObject({ kind: "photo", failed: true, lastError: msg, payload: { data: { id: bad.id } } });
    expect(bytesOf(rows[0].value.blob)).toEqual(Array.from(bad.bytes));

    // Never re-sent, never auto-deleted.
    window.dispatchEvent(new Event("online"));
    await vi.advanceTimersByTimeAsync(600_000);
    await Q.drain();
    expect(postedIds().filter((id) => id === bad.id)).toHaveLength(1);
    expect(await rawById(bad.id)).toBeDefined();
  });

  test.each<[string, () => Response | Promise<Response>]>([
    ["500 INTERNAL_ERROR", () => envelope(500, "INTERNAL_ERROR", "boom")],
    ["network error", netDown],
    ["502 HTML from an intermediary", () => new Response("<html>Bad Gateway</html>", { status: 502 })],
    ["409 without an envelope", () => new Response("<html>conflict</html>", { status: 409 })],
  ])("retryable %s: attempts++, blob kept, later entries held", async (_, respond) => {
    await start();
    const s = stop("S");
    const ph = photo(s, 1);
    const later = stop("Later");
    handler = (p) => (p.id === ph.id ? respond() : created(p));
    await Q.enqueue(photoItem(ph));
    await flush();
    await Q.enqueue(stopItem(later));
    await flush();
    expect(new Set(postedIds())).toEqual(new Set([ph.id]));
    const row = await rawById(ph.id);
    expect(row?.value.failed).toBe(false);
    expect(row?.value.attempts as number).toBeGreaterThanOrEqual(1);
    expect(row?.value.attempts).toBe(posts.length);
    expect(typeof row?.value.lastError).toBe("string");
    expect((row?.value.lastError as string).length).toBeGreaterThan(0);
    expect(bytesOf(row?.value.blob)).toEqual(Array.from(ph.bytes));
    expect(await rawById(later.id)).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
describe("AC5: a never-retry stop fails its queued photos without sending them", () => {
  test("stop 422 + 2 photos in one enqueue: exactly 1 fetch, 3 failed entries, bytes kept", async () => {
    await start();
    const s = stop("Daly Waters Pub");
    const [p1, p2] = [photo(s, 1), photo(s, 2)];
    handler = (p) => (p.id === s.id ? envelope(422, "VALIDATION_ERROR", "name is too long") : created(p));
    await Q.enqueue([stopItem(s), photoItem(p1), photoItem(p2)]);
    await Q.drain();
    await vi.advanceTimersByTimeAsync(600_000);
    await Q.drain();
    expect(postedIds()).toEqual([s.id]);
    const rows = await rawAll();
    expect(rows).toHaveLength(3);
    expect(rows.every((r) => r.value.failed === true)).toBe(true);
    expect((await rawById(s.id))!.value.lastError).toBe("name is too long");
    for (const ph of [p1, p2]) {
      const v = (await rawById(ph.id))!.value;
      expect(v.lastError).toBe('Not sent: stop "Daly Waters Pub" failed');
      expect(bytesOf(v.blob)).toEqual(Array.from(ph.bytes));
    }
  });

  test("cascade is scoped to that stop's non-failed photos; other stops' photos still send", async () => {
    await start();
    const bad = stop("Bad");
    const good = stop("Good");
    const orphan = photo(bad, 9); // queued before its stop, fails on its own
    const pBad = photo(bad, 1);
    const pGood = photo(good, 2);
    handler = (p) => {
      if (p.id === orphan.id) return envelope(404, "NOT_FOUND", "no such stop");
      if (p.id === bad.id) return envelope(409, "CONFLICT", "clash");
      return created(p);
    };
    await Q.enqueue(photoItem(orphan));
    await Q.drain();
    // All four in one enqueue, so pGood is already stored when the cascade runs, and no
    // explicit drain(): the triggered pass must re-read and carry on to `good`.
    await Q.enqueue([stopItem(bad), photoItem(pBad), stopItem(good), photoItem(pGood)]);
    await until(() => posts.length === 4, "drain continues past the failed stop");
    await flush();
    expect(postedIds()).toEqual([orphan.id, bad.id, good.id, pGood.id]);
    expect((await rawById(orphan.id))!.value).toMatchObject({ failed: true, lastError: "no such stop" });
    expect((await rawById(pBad.id))!.value).toMatchObject({ failed: true, lastError: 'Not sent: stop "Bad" failed' });
    expect(await rawById(pGood.id)).toBeUndefined();
    expect((await rawAll()).map((r) => (r.value.payload as { data: { id: string } }).data.id)).toEqual([
      orphan.id,
      bad.id,
      pBad.id,
    ]);
  });

  test("the cascade happens in the same transaction that marks the stop failed", async () => {
    await start();
    const s = stop("S");
    const ph = photo(s, 1);
    let release!: (r: Response) => void;
    handler = () => new Promise<Response>((r) => (release = r));
    await Q.enqueue([stopItem(s), photoItem(ph)]);
    await until(() => posts.length === 1);
    // Snapshot the store right after every readwrite commit, on the same connection,
    // so the snapshot is ordered before any readwrite transaction opened afterwards.
    const snaps: boolean[][] = [];
    const orig = IDBDatabase.prototype.transaction;
    vi.spyOn(IDBDatabase.prototype, "transaction").mockImplementation(function (this: IDBDatabase, ...args) {
      const tx = orig.apply(this, args as Parameters<typeof orig>);
      if (tx.mode === "readwrite")
        tx.addEventListener("complete", () => {
          const out: boolean[] = [];
          const c = orig.call(this, "entries", "readonly").objectStore("entries").openCursor();
          c.onsuccess = () => {
            if (c.result) {
              out.push(c.result.value.failed as boolean);
              c.result.continue();
            } else snaps.push(out);
          };
        });
      return tx;
    });
    release(envelope(422, "VALIDATION_ERROR", "bad"));
    await Q.drain();
    await flush();
    expect((await rawAll()).map((r) => r.value.failed)).toEqual([true, true]);
    expect(snaps.length).toBeGreaterThan(0);
    // No committed state ever had the stop failed while its photo was still pending.
    for (const snap of snaps) expect(snap).not.toEqual([true, false]);
    expect(posts.filter((p) => p.id === ph.id)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
describe("AC6: QueueNotice labels photos", () => {
  const settle = () => act(() => flush());

  test("'Photo for <stopName>' in failed and still-trying lists; Dismiss deletes only that entry", async () => {
    await start();
    const { QueueNotice } = await import("./QueueNotice");
    const bad = stop("Daly Waters Pub");
    const pBad = photo(bad, 1);
    const other = stop("Larrimah Hotel");
    const stuck = photo(other, 2);
    handler = (p) => (p.id === bad.id ? envelope(422, "VALIDATION_ERROR", "name is too long") : netDown());
    await Q.enqueue([stopItem(bad), photoItem(pBad)]);
    await Q.enqueue(photoItem(stuck));
    await flush();
    const attempts = async () => (await rawById(stuck.id))!.value.attempts as number;
    for (let i = 0; i < 30 && (await attempts()) < 10; i++) await Q.drain();
    expect(await attempts()).toBe(10);

    const { container } = render(<QueueNotice />);
    await settle();
    const items = screen.getAllByRole("listitem");
    const failedPhoto = items.find((li) => li.textContent?.includes("Photo for Daly Waters Pub"));
    const stuckPhoto = items.find((li) => li.textContent?.includes("Photo for Larrimah Hotel"));
    expect(failedPhoto, "failed photo row").toBeDefined();
    expect(stuckPhoto, "still-trying photo row").toBeDefined();
    expect(stuckPhoto!.textContent).toMatch(/still trying/i);
    expect(within(stuckPhoto!).queryByRole("button", { name: /dismiss/i })).toBeNull();
    expect(screen.getAllByRole("button", { name: /dismiss/i })).toHaveLength(2); // stop + its photo

    fireEvent.click(within(failedPhoto!).getByRole("button", { name: /dismiss/i }));
    await settle();
    expect(await rawById(pBad.id)).toBeUndefined();
    expect((await rawById(bad.id))?.value.failed).toBe(true);
    expect(await rawById(stuck.id)).toBeDefined();
    expect(container.textContent).not.toContain("Photo for Daly Waters Pub");
    expect(screen.getAllByRole("button", { name: /dismiss/i })).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
test("AC7 (spec §12): stop sent, photo lost to the network, restart, photo replayed with original id and bytes", async () => {
  await start();
  const s = stop("Tennant Creek");
  const ph = photo(s, 5);
  handler = (p) => (isPhoto(p) ? netDown() : created(p));
  await Q.enqueue([stopItem(s), photoItem(ph)]);
  await until(() => posts.length === 2);
  await flush();
  const before = await rawAll();
  expect(before).toHaveLength(1);
  expect(before[0].value).toMatchObject({ kind: "photo", attempts: 1, failed: false, payload: { data: { id: ph.id } } });
  expect(bytesOf(before[0].value.blob)).toEqual(Array.from(ph.bytes));

  // Restart: brand new module instance, same IndexedDB global.
  posts = [];
  handler = (p) => created(p, 200); // the server already had it: replay
  await load();
  Q.startQueue(new QueryClient());
  await Q.drain();
  await until(async () => (await rawAll()).length === 0, "store drained after restart");
  expect(posts).toHaveLength(1);
  await expectPhotoRequest(posts[0], ph);
});
