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
import type { StopCreate } from '../api/gen/types/StopCreate'

// Photos join this union in t-offline-queue-photos.
export type QueueItem = { kind: 'stop'; payload: { slug: string; data: StopCreate } }
export type QueueEntry = QueueItem & { attempts: number; lastError: string | null; failed: boolean }
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

/** Stores the item; resolves once the write has committed, then starts a drain. */
export async function enqueue(item: QueueItem): Promise<void> {
  const entry: QueueEntry = { ...item, attempts: 0, lastError: null, failed: false }
  await inTx('readwrite', (s) => void s.add(entry))
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
    const { slug, data } = entry.payload
    try {
      await createStopApiTripsSlugStopsPost({ slug, data })
    } catch (err) {
      const attempts = entry.attempts + 1
      if (isNeverRetry(err)) {
        await putEntry({ ...entry, attempts, failed: true, lastError: (err as ApiError).envelope!.error.message })
        await emit()
        continue
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
