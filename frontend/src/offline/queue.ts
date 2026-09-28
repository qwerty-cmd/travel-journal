/**
 * The offline write queue (decision-log Entry 19): one auto-increment IndexedDB
 * store, drained FIFO from the page — never Service Worker Background Sync,
 * which iOS lacks — and never gated on `navigator.onLine`; a send attempt is the
 * only reliable connectivity test.
 *
 * Retry classification is docs/api-contract.md, Error envelope: a never-retry
 * failure marks the entry `failed` and keeps it (never auto-deleted) until the
 * rider dismisses it; anything else counts an attempt and stops the drain, which
 * resumes on the next trigger or after a 5s → 300s doubling backoff.
 */
import type { QueryClient } from '@tanstack/react-query'
import { ApiError } from '../api/client'
import { createStopApiTripsSlugStopsPost } from '../api/gen/clients/createStopApiTripsSlugStopsPost'
import { listStopsApiTripsSlugStopsGetQueryKey } from '../api/gen/hooks/useListStopsApiTripsSlugStopsGet'
import { getMapApiTripsSlugMapGetQueryKey } from '../api/gen/hooks/useGetMapApiTripsSlugMapGet'
import { uploadPhotoApiTripsSlugStopsStopIdPhotosPost } from '../api/gen/clients/uploadPhotoApiTripsSlugStopsStopIdPhotosPost'
import { listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey } from '../api/gen/hooks/useListPhotosApiTripsSlugStopsStopIdPhotosGet'
import type { StopCreate } from '../api/gen/types/StopCreate'
import type { BodyUploadPhotoApiTripsSlugStopsStopIdPhotosPost } from '../api/gen/types/BodyUploadPhotoApiTripsSlugStopsStopIdPhotosPost'

export type QueueItem = { kind: 'stop'; payload: { slug: string; data: StopCreate } }
type PhotoPayload = {
  slug: string
  stopId: string
  stopName: string
  data: Omit<BodyUploadPhotoApiTripsSlugStopsStopIdPhotosPost, 'file'>
}
/** A photo as handed to `enqueue`: `file` is the processed JPEG. */
export type PhotoItem = { kind: 'photo'; payload: PhotoPayload; file: Blob }
/**
 * A photo is stored with its bytes as an ArrayBuffer (always image/jpeg), not a
 * Blob — Blob-in-IndexedDB is unreliable in old Safari and in jsdom.
 */
type Meta = { attempts: number; lastError: string | null; failed: boolean }
export type QueueEntry = (QueueItem | { kind: 'photo'; payload: PhotoPayload; blob: ArrayBuffer }) & Meta
export type QueueRecord = QueueEntry & { key: number }

const DB_NAME = 'btj-queue'
const STORE = 'entries'
const NEVER_RETRY = new Set(['VALIDATION_ERROR', 'METHOD_NOT_ALLOWED', 'CONFLICT', 'FORBIDDEN', 'NOT_FOUND'])
const BACKOFF_START_MS = 5_000
const BACKOFF_CAP_MS = 300_000

/** True only for the five contract never-retry codes; no envelope, or an unknown code, retries. */
export function isNeverRetry(err: unknown): boolean {
  return err instanceof ApiError && NEVER_RETRY.has(err.envelope?.error.code as string)
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

// --- enqueue / dismiss ---------------------------------------------------

/**
 * Stores the item(s) in one transaction — so a stop and its photos land
 * together — and resolves once it has committed, then starts one drain.
 */
export async function enqueue(items: QueueItem | PhotoItem | (QueueItem | PhotoItem)[]): Promise<void> {
  const meta: Meta = { attempts: 0, lastError: null, failed: false }
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
  await emit()
}

/** Deletes an entry — the only way a failed entry ever leaves the store. */
export async function dismiss(key: number): Promise<void> {
  await deleteEntry(key)
  await emit()
}

// --- drain ---------------------------------------------------------------

let queryClient: QueryClient | undefined
let running: Promise<void> | undefined
let again = false
let failures = 0
let timer: ReturnType<typeof setTimeout> | undefined

/**
 * Sends pending entries in key order, one at a time. One drain per tab: a call
 * while one is running returns that drain, which then runs once more.
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
        await drainOnce()
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

async function drainOnce(): Promise<void> {
  for (const entry of await readAll()) {
    if (entry.failed) continue
    const { slug } = entry.payload
    try {
      if (entry.kind === 'stop') {
        await createStopApiTripsSlugStopsPost({ slug, data: entry.payload.data })
      } else {
        const { stopId, data } = entry.payload
        const file = new Blob([entry.blob], { type: 'image/jpeg' })
        await uploadPhotoApiTripsSlugStopsStopIdPhotosPost({ slug, stop_id: stopId, data: { ...data, file } })
      }
    } catch (err) {
      const attempts = entry.attempts + 1
      if (isNeverRetry(err)) {
        const failedEntry = { ...entry, attempts, failed: true, lastError: (err as ApiError).envelope!.error.message }
        if (failedEntry.kind === 'photo') {
          await putEntry(failedEntry)
          await emit()
          continue
        }
        await failStop(failedEntry)
        await emit()
        // This pass's snapshot still shows the cascaded photos as pending: re-read.
        again = true
        return
      }
      await putEntry({ ...entry, attempts, lastError: err instanceof Error ? err.message : String(err) })
      await emit()
      failures++
      clearTimeout(timer)
      timer = setTimeout(trigger, Math.min(BACKOFF_START_MS * 2 ** (failures - 1), BACKOFF_CAP_MS))
      return
    }
    failures = 0
    await deleteEntry(entry.key)
    await emit()
    if (entry.kind === 'photo') {
      queryClient?.invalidateQueries({
        queryKey: listPhotosApiTripsSlugStopsStopIdPhotosGetQueryKey({ slug, stop_id: entry.payload.stopId }),
      })
      continue
    }
    queryClient?.invalidateQueries({ queryKey: listStopsApiTripsSlugStopsGetQueryKey({ slug }) })
    queryClient?.invalidateQueries({ queryKey: getMapApiTripsSlugMapGetQueryKey({ slug }) })
  }
}

/** Wires the drain triggers and runs the first drain. Call once, at app start. */
export function startQueue(client: QueryClient): void {
  queryClient = client
  navigator.storage?.persist?.().catch(() => {})
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
