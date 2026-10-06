/**
 * The offline write queue (decision-log Entry 19): one auto-increment IndexedDB
 * store, drained FIFO from the page — never Service Worker Background Sync,
 * which iOS lacks — and never gated on `navigator.onLine`; a send attempt is the
 * only reliable connectivity test.
 *
 * Retry classification is docs/api-contract.md, "Offline-queue classification
 * (Entry 29)":
 * - a never-retry failure marks the entry `failed` and keeps it, photo bytes
 *   included (never auto-deleted), until the rider dismisses it;
 * - `UNAUTHENTICATED` pauses the queue: the entry is left exactly as it was (not
 *   failed, attempt not counted) and the drain stops. The pause is kept in
 *   localStorage so it survives a reload; a successful send or a `signin` on
 *   the BroadcastChannel `auth` clears it, and the usual triggers still try;
 * - `RATE_LIMITED` counts the attempt and retries after the response's
 *   `Retry-After` seconds, without advancing the doubling backoff;
 * - anything else counts an attempt and stops the drain, which resumes on the
 *   next trigger or after a 5s → 300s doubling backoff.
 *
 * Several tabs share the one store. Where the Web Locks API exists only the tab
 * holding the `btj-queue-drain` lock drains, so two tabs never send the same
 * entry; elsewhere each tab falls back to its own in-memory guard. Where
 * BroadcastChannel exists every change is announced on `btj-queue`, so other
 * tabs' subscribers (the QueueNotice) re-read, and a new entry wakes a drain
 * in the tab that holds the lock. Every send is aborted after 30s (stop) or
 * 120s (photo), so a hung request can't hold that lock forever.
 *
 * Hold (same contract section): an entry stamped with a `userId` is sent only
 * while that account is signed in (cached `btj.me`, re-read before every
 * send). Otherwise it is held — skipped, not failed, attempts unchanged — and
 * the drain moves on to the next entry. Entries without a `userId` are sent
 * under whatever session is current.
 *
 * APIs called: `{tripId}` entries go to POST /api/v2/trips/{tripId}/stops and
 * POST /api/v2/trips/{tripId}/stops/{stopId}/photos; slug entries to
 * POST /api/trips/{slug}/stops and POST /api/trips/{slug}/stops/{stop_id}/photos
 * (multipart, one request per photo, retried whole under the same client
 * id). After a successful send it
 * invalidates the stops and map queries (stop) or that stop's photos query
 * (photo) so open screens refetch.
 */
import type { QueryClient } from '@tanstack/react-query'
import { ApiError } from '../api/client'
import { AUTH_CHANNEL, type AuthMessage } from '../auth'
import { createStopApiTripsSlugStopsPost } from '../api/gen/clients/createStopApiTripsSlugStopsPost'
import { listStopsApiTripsSlugStopsGetQueryKey } from '../api/gen/hooks/useListStopsApiTripsSlugStopsGet'
import { getMapApiTripsSlugMapGetQueryKey } from '../api/gen/hooks/useGetMapApiTripsSlugMapGet'
import { uploadPhotoApiTripsSlugStopsStopIdPhotosPost } from '../api/gen/clients/uploadPhotoApiTripsSlugStopsStopIdPhotosPost'
import { listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey } from '../api/gen/hooks/useListPhotosApiTripsSlugStopsStopIdPhotosGet'
import type { StopCreate } from '../api/gen/types/StopCreate'
import type { BodyUploadPhotoApiTripsSlugStopsStopIdPhotosPost } from '../api/gen/types/BodyUploadPhotoApiTripsSlugStopsStopIdPhotosPost'
import { createStopApiV2TripsTripIdStopsPost } from '../api/gen/clients/createStopApiV2TripsTripIdStopsPost'
import { uploadPhotoApiV2TripsTripIdStopsStopIdPhotosPost } from '../api/gen/clients/uploadPhotoApiV2TripsTripIdStopsStopIdPhotosPost'
import { listStopsApiV2TripsTripIdStopsGetQueryKey } from '../api/gen/hooks/useListStopsApiV2TripsTripIdStopsGet'
import { getMapApiV2TripsTripIdMapGetQueryKey } from '../api/gen/hooks/useGetMapApiV2TripsTripIdMapGet'
import { listPhotosApiV2TripsTripIdStopsStopIdPhotosGetQueryKey } from '../api/gen/hooks/useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet'
import type { BodyUploadPhotoApiV2TripsTripIdStopsStopIdPhotosPost } from '../api/gen/types/BodyUploadPhotoApiV2TripsTripIdStopsStopIdPhotosPost'
import { getCachedMe } from '../localStore'

type LegacyPhotoPayload = {
  slug: string
  stopId: string
  stopName: string
  data: Omit<BodyUploadPhotoApiTripsSlugStopsStopIdPhotosPost, 'file'>
}
type V2PhotoPayload = {
  tripId: string
  stopId: string
  stopName: string
  data: Omit<BodyUploadPhotoApiV2TripsTripIdStopsStopIdPhotosPost, 'file'>
}
/** A legacy stop, sent to /api/trips/{slug}/stops. */
export type QueueItem = { kind: 'stop'; payload: { slug: string; data: StopCreate } }
/** A legacy photo as handed to `enqueue`: `file` is the processed JPEG. */
export type PhotoItem = { kind: 'photo'; payload: LegacyPhotoPayload; file: Blob }
/** A v2 stop, sent to /api/v2/trips/{tripId}/stops; enqueued only with a `userId`. */
export type V2StopItem = { kind: 'stop'; payload: { tripId: string; data: StopCreate } }
/** A v2 photo, sent to /api/v2/trips/{tripId}/stops/{stopId}/photos; enqueued only with a `userId`. */
export type V2PhotoItem = { kind: 'photo'; payload: V2PhotoPayload; file: Blob }
type PhotoPayload = LegacyPhotoPayload | V2PhotoPayload
/**
 * A photo is stored with its bytes as an ArrayBuffer (always image/jpeg), not a
 * Blob — Blob-in-IndexedDB is unreliable in old Safari and in jsdom.
 *
 * `userId` is the account the entry was captured under (an identifier, not a
 * secret). Absent on entries from before the upgrade and from the legacy add
 * route; those are sent under whatever session is current. Optional, so the
 * store needs no schema change.
 */
type Meta = { attempts: number; lastError: string | null; failed: boolean; userId?: string }
export type QueueEntry = (QueueItem | V2StopItem | { kind: 'photo'; payload: PhotoPayload; blob: ArrayBuffer }) & Meta
export type QueueRecord = QueueEntry & { key: number }

const DB_NAME = 'btj-queue'
const STORE = 'entries'
/**
 * `FORBIDDEN` stays never-retry on purpose (decision-log Entry 29, "no grace"):
 * once a rider is revoked, every queued item of theirs — including items
 * captured before the revocation — fails with 403 and is surfaced, never sent
 * and never dropped. A grace window for pre-revocation captures was rejected
 * because `arrivedAt`/`takenAt` are client-asserted and could be backdated; the
 * rider can still keep a failed photo via "Save photo to this device".
 */
const NEVER_RETRY = new Set(['VALIDATION_ERROR', 'METHOD_NOT_ALLOWED', 'CONFLICT', 'FORBIDDEN', 'NOT_FOUND'])
const BACKOFF_START_MS = 5_000
const BACKOFF_CAP_MS = 300_000
/**
 * Every send is aborted after a timeout, so a request that never settles can't
 * hold the cross-tab drain lock forever. A stop is a small JSON body; a ~1 MiB
 * photo on a weak cellular link can take over a minute, so it gets longer.
 * An abort has no envelope, so it retries with backoff like a network failure.
 */
const STOP_TIMEOUT_MS = 30_000
const PHOTO_TIMEOUT_MS = 120_000

/** Runs `send` with a signal that aborts after `ms` (AbortSignal.timeout, else an AbortController + setTimeout). */
async function withTimeout<T>(ms: number, send: (signal: AbortSignal) => Promise<T>): Promise<T> {
  if (typeof AbortSignal.timeout === 'function') return send(AbortSignal.timeout(ms))
  const controller = new AbortController()
  const t = setTimeout(() => controller.abort(new DOMException('The operation timed out.', 'TimeoutError')), ms)
  try {
    return await send(controller.signal)
  } finally {
    clearTimeout(t)
  }
}

/** True only for the five contract never-retry codes; no envelope, or an unknown code, retries. */
export function isNeverRetry(err: unknown): boolean {
  return err instanceof ApiError && NEVER_RETRY.has(err.envelope?.error.code as string)
}

const errorCode = (err: unknown) => (err instanceof ApiError ? err.envelope?.error.code : undefined)

// --- 401 pause ------------------------------------------------------------

/**
 * Set by a `401 UNAUTHENTICATED` send, cleared by a successful send or a
 * sign-in. localStorage rather than IndexedDB, so the store's schema (version 1,
 * entries only) is unchanged; every access is wrapped because storage can throw
 * (Safari private mode, disabled), which then reads as "not paused".
 */
const PAUSED_KEY = 'btj.queue.paused'

/** True while the queue is paused for sign-in (see the module comment). */
export function isPaused(): boolean {
  try {
    return localStorage.getItem(PAUSED_KEY) === '1'
  } catch {
    return false
  }
}

function setPaused(paused: boolean): void {
  try {
    if (paused) localStorage.setItem(PAUSED_KEY, '1')
    else localStorage.removeItem(PAUSED_KEY)
  } catch {
    // Storage unavailable: the pause just won't survive a reload.
  }
}

let dbPromise: Promise<IDBDatabase> | undefined

function openDb(): Promise<IDBDatabase> {
  dbPromise ??= new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { autoIncrement: true })
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  }).catch((e) => {
    dbPromise = undefined
    throw e
  })
  return dbPromise
}

/** Runs `fn` in one transaction and resolves with its request's result only once the transaction commits. */
async function inTx<T>(mode: IDBTransactionMode, fn: (store: IDBObjectStore) => (() => T) | void): Promise<T> {
  const db = await openDb()
  return new Promise<T>((resolve, reject) => {
    const tx = db.transaction(STORE, mode)
    const result = fn(tx.objectStore(STORE))
    tx.oncomplete = () => resolve(result ? result() : (undefined as T))
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error)
  })
}

function readAll(): Promise<QueueRecord[]> {
  return inTx('readonly', (store) => {
    const keys = store.getAllKeys()
    const values = store.getAll()
    return () => values.result.map((v: QueueEntry, i) => ({ ...v, key: keys.result[i] as number }))
  })
}

const putEntry = ({ key, ...entry }: QueueRecord) => inTx('readwrite', (s) => void s.put(entry, key))

/**
 * Marks a never-retry stop failed and, in the same transaction, every pending
 * photo of that stop — a photo whose stop was never created can never succeed,
 * so it is failed without being sent.
 */
const failStop = ({ key, ...entry }: QueueRecord & { kind: 'stop' }) =>
  inTx('readwrite', (s) => {
    s.put(entry, key)
    s.openCursor().onsuccess = (ev) => {
      const cursor = (ev.target as IDBRequest<IDBCursorWithValue | null>).result
      if (!cursor) return
      const v = cursor.value as QueueEntry
      if (v.kind === 'photo' && !v.failed && v.payload.stopId === entry.payload.data.id) {
        cursor.update({ ...v, failed: true, lastError: `Not sent: stop "${entry.payload.data.name}" failed` })
      }
      cursor.continue()
    }
  })
const deleteEntry = (key: number) => inTx('readwrite', (s) => void s.delete(key))

// --- change notification -------------------------------------------------

type Listener = (entries: QueueRecord[]) => void
const listeners = new Set<Listener>()

async function emit(only?: Listener): Promise<void> {
  const entries = await readAll()
  for (const l of only ? [only] : listeners) if (listeners.has(l)) l(entries)
}

/** Calls `listener` with every stored entry now and after every queue change. Returns the unsubscribe. */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener)
  emit(listener).catch(console.error)
  return () => void listeners.delete(listener)
}

// --- cross-tab ------------------------------------------------------------

const LOCK = 'btj-queue-drain'
const CHANNEL = 'btj-queue'
/** `enqueued` also wakes the receiver's drain; `changed` only refreshes its subscribers. */
type Broadcast = 'enqueued' | 'changed'
let channel: BroadcastChannel | undefined

/** Tells every other tab, and this tab's subscribers, that the store changed. */
async function changed(msg: Broadcast = 'changed'): Promise<void> {
  channel?.postMessage(msg)
  await emit()
}

/** Runs `fn` only if this tab gets the cross-tab drain lock; without Web Locks, always. */
async function withDrainLock(fn: () => Promise<void>): Promise<void> {
  const locks = navigator.locks
  if (!locks?.request) return fn()
  await locks.request(LOCK, { ifAvailable: true }, async (lock) => {
    if (lock) await fn()
  })
}

// --- enqueue / dismiss ---------------------------------------------------

type Items<T> = T | T[]

/**
 * Stores the item(s) in one transaction — so a stop and its photos land
 * together — and resolves once it has committed, then starts one drain.
 * `userId` (the signed-in account's id) is stamped on every item; it is
 * required for `{tripId}` items and optional only for legacy slug items.
 */
export async function enqueue(items: Items<QueueItem | PhotoItem>, userId?: string): Promise<void>
export async function enqueue(items: Items<QueueItem | PhotoItem | V2StopItem | V2PhotoItem>, userId: string): Promise<void>
export async function enqueue(
  items: Items<QueueItem | PhotoItem | V2StopItem | V2PhotoItem>,
  userId?: string,
): Promise<void> {
  const meta: Meta = { attempts: 0, lastError: null, failed: false, ...(userId === undefined ? {} : { userId }) }
  // Bytes are read before the transaction opens: an await inside it would let it auto-commit.
  const entries: QueueEntry[] = await Promise.all(
    [items].flat().map(async (item): Promise<QueueEntry> => {
      if (item.kind === 'stop') return { ...item, ...meta }
      const { file, ...rest } = item
      return { ...rest, blob: await file.arrayBuffer(), ...meta }
    }),
  )
  await inTx('readwrite', (s) => entries.forEach((e) => s.add(e)))
  trigger()
  await changed('enqueued')
}

/** Deletes an entry — the only way a failed entry ever leaves the store. */
export async function dismiss(key: number): Promise<void> {
  await deleteEntry(key)
  await changed()
}

// --- drain ---------------------------------------------------------------

let queryClient: QueryClient | undefined
let running: Promise<void> | undefined
let again = false
let failures = 0
let timer: ReturnType<typeof setTimeout> | undefined

/**
 * Sends pending entries in key order, one at a time. One drain per tab: a call
 * while one is running returns that drain, which then runs once more. Each pass
 * takes the cross-tab lock; a pass that can't get it is skipped, since the tab
 * holding it is draining the same store.
 */
export function drain(): Promise<void> {
  if (running) {
    again = true
    return running
  }
  running = (async () => {
    try {
      do {
        again = false
        await withDrainLock(drainOnce)
      } while (again)
    } finally {
      running = undefined
    }
  })()
  return running
}

function trigger(): void {
  drain().catch(console.error)
}

/**
 * Held: the entry carries a `userId` and it isn't the signed-in account's — a
 * different account, or nobody signed in. Read from the cached `btj.me` on every
 * call, never stored, so the hold follows sign-in/sign-out and survives a reload.
 */
export function isHeld(entry: QueueEntry): boolean {
  return entry.userId !== undefined && entry.userId !== getCachedMe()?.id
}

/** Sends one entry through the v2 client (`tripId` payload) or the legacy one (`slug` payload). */
function send(entry: QueueRecord): Promise<unknown> {
  if (entry.kind === 'stop') {
    const p = entry.payload
    return withTimeout(STOP_TIMEOUT_MS, (signal) =>
      'tripId' in p
        ? createStopApiV2TripsTripIdStopsPost({ tripId: p.tripId, data: p.data }, { signal })
        : createStopApiTripsSlugStopsPost({ slug: p.slug, data: p.data }, { signal }),
    )
  }
  const p = entry.payload
  const file = new Blob([entry.blob], { type: 'image/jpeg' })
  return withTimeout(PHOTO_TIMEOUT_MS, (signal) =>
    'tripId' in p
      ? uploadPhotoApiV2TripsTripIdStopsStopIdPhotosPost(
          { tripId: p.tripId, stopId: p.stopId, data: { ...p.data, file } },
          { signal },
        )
      : uploadPhotoApiTripsSlugStopsStopIdPhotosPost({ slug: p.slug, stop_id: p.stopId, data: { ...p.data, file } }, { signal }),
  )
}

/** After a successful send, the open screens showing that entry refetch. */
function invalidate(entry: QueueRecord): void {
  const p = entry.payload
  const keys =
    entry.kind === 'photo'
      ? [
          'tripId' in p
            ? listPhotosApiV2TripsTripIdStopsStopIdPhotosGetQueryKey({ tripId: p.tripId, stopId: entry.payload.stopId })
            : listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey({ slug: p.slug, stop_id: entry.payload.stopId }),
        ]
      : 'tripId' in p
        ? [listStopsApiV2TripsTripIdStopsGetQueryKey({ tripId: p.tripId }), getMapApiV2TripsTripIdMapGetQueryKey({ tripId: p.tripId })]
        : [listStopsApiTripsSlugStopsGetQueryKey({ slug: p.slug }), getMapApiTripsSlugMapGetQueryKey({ slug: p.slug })]
  for (const queryKey of keys) queryClient?.invalidateQueries({ queryKey })
}

async function drainOnce(): Promise<void> {
  for (const entry of await readAll()) {
    // Held entries are skipped untouched (not sent, not failed, attempts unchanged); later entries still go.
    if (entry.failed || isHeld(entry)) continue
    try {
      await send(entry)
    } catch (err) {
      const code = errorCode(err)
      if (code === 'UNAUTHENTICATED') {
        // Pause: the entry is untouched (not failed, attempt not counted) and no backoff is scheduled.
        setPaused(true)
        clearTimeout(timer)
        await changed()
        return
      }
      const attempts = entry.attempts + 1
      const retryAfter = code === 'RATE_LIMITED' ? (err as ApiError).retryAfter : undefined
      if (retryAfter !== undefined) {
        // The server's wait replaces the doubling backoff, which is left where it was.
        await putEntry({ ...entry, attempts, lastError: (err as ApiError).message })
        await changed()
        clearTimeout(timer)
        timer = setTimeout(trigger, retryAfter * 1000)
        return
      }
      if (isNeverRetry(err)) {
        const failedEntry = { ...entry, attempts, failed: true, lastError: (err as ApiError).envelope!.error.message }
        if (failedEntry.kind === 'photo') {
          await putEntry(failedEntry)
          await changed()
          continue
        }
        await failStop(failedEntry)
        await changed()
        // This pass's snapshot still shows the cascaded photos as pending: re-read.
        again = true
        return
      }
      await putEntry({ ...entry, attempts, lastError: err instanceof Error ? err.message : String(err) })
      await changed()
      failures++
      clearTimeout(timer)
      timer = setTimeout(trigger, Math.min(BACKOFF_START_MS * 2 ** (failures - 1), BACKOFF_CAP_MS))
      return
    }
    failures = 0
    if (isPaused()) setPaused(false)
    await deleteEntry(entry.key)
    await changed()
    invalidate(entry)
  }
}

/** Wires the drain triggers and runs the first drain. Call once, at app start. */
export function startQueue(client: QueryClient): void {
  queryClient = client
  navigator.storage?.persist?.().catch(() => {})
  if (typeof BroadcastChannel === 'function') {
    channel = new BroadcastChannel(CHANNEL)
    channel.onmessage = (e: MessageEvent<Broadcast>) => {
      emit().catch(console.error)
      if (e.data === 'enqueued') trigger()
    }
    // A sign-in in any tab (this one included: auth.ts posts from its own channel object) lifts the 401 pause
    // and re-evaluates which entries are held. A sign-out only refreshes the notice: `btj.me` is already
    // cleared, so the drain's per-entry `isHeld` check stops sending entries that carry a `userId`.
    const auth = new BroadcastChannel(AUTH_CHANNEL)
    auth.onmessage = (e: MessageEvent<AuthMessage>) => {
      if (e.data?.type === 'signout') emit().catch(console.error)
      if (e.data?.type !== 'signin') return
      setPaused(false)
      emit().catch(console.error)
      trigger()
    }
  }
  window.addEventListener('online', () => {
    clearTimeout(timer)
    failures = 0
    trigger()
  })
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') trigger()
  })
  trigger()
}
