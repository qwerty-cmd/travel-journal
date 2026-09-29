// Test-only Web Locks stand-in. jsdom has no `navigator.locks`, so without this
// the queue suites only exercise withDrainLock's no-lock fallback.

/**
 * In-memory Web Locks: exclusive, `ifAvailable` only (the one form queue.ts
 * uses). A held name calls back with `null`, as the real API does; the lock is
 * released when the callback's promise settles, and `request` resolves with the
 * callback's result. Every request's name is recorded in `requests`.
 */
export class FakeLocks {
  held = new Set<string>();
  requests: string[] = [];
  async request(name: string, opts: LockOptions, cb: (lock: Lock | null) => Promise<unknown>) {
    if (!opts.ifAvailable) throw new Error("fake supports ifAvailable only");
    this.requests.push(name);
    if (this.held.has(name)) return cb(null);
    this.held.add(name);
    try {
      return await cb({ name, mode: "exclusive" } as Lock);
    } finally {
      this.held.delete(name);
    }
  }
}

/** Installs `locks` as `navigator.locks`; `undefined` restores jsdom's no-locks state. */
export function setNavigatorLocks(locks: FakeLocks | undefined): void {
  Object.defineProperty(navigator, "locks", { configurable: true, value: locks });
}

/** The lock name queue.ts drains under. */
export const DRAIN_LOCK = "btj-queue-drain";

/**
 * The drain lock's state right now, for a fetch stub to record at each send:
 * `absent` with no `navigator.locks`, else whether the drain lock is held.
 */
export function drainLockState(): "held" | "free" | "absent" {
  const locks = navigator.locks as unknown as FakeLocks | undefined;
  if (!locks) return "absent";
  return locks.held.has(DRAIN_LOCK) ? "held" : "free";
}

/**
 * The two drain-guard modes the core queue suites run in: real browsers take
 * the `btj-queue-drain` Web Lock; without it each tab uses its in-memory guard.
 */
export const LOCK_MODES = [
  { mode: "Web Locks", locks: true },
  { mode: "no-lock fallback", locks: false },
] as const;
