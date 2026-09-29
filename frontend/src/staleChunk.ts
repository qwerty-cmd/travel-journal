/**
 * Stale chunks after a deploy. Route components are lazy chunks with hashed
 * names, and the service worker auto-updates, so a tab opened before a deploy
 * can ask for a chunk the new build no longer has. Vite reports that as a
 * `vite:preloadError` event; the cure is one full reload onto the new build.
 *
 * The reload is guarded so a chunk that is still missing after the reload
 * (a broken deploy, or offline with nothing precached) can't loop: a second
 * failure within RELOAD_WINDOW_MS of the last reload is left to surface as a
 * normal error. If sessionStorage is unusable the guard can't be recorded, so
 * no reload happens at all: a visible error beats a possible reload loop.
 *
 * TanStack Router's lazyRouteComponent has its own once-per-message reload for
 * a missing module at render time; this covers the import itself, which Vite
 * wraps for every lazy route chunk in the build.
 *
 * APIs called: none (the reload re-fetches static assets only).
 */
const KEY = "btj.staleChunkReloadAt";
export const RELOAD_WINDOW_MS = 30_000;

/** True, and records `now`, if a stale-chunk reload is allowed; false if one just happened or storage fails. */
export function claimReload(now: number = Date.now()): boolean {
  try {
    const last = Number(sessionStorage.getItem(KEY));
    if (last && now - last >= 0 && now - last < RELOAD_WINDOW_MS) return false;
    sessionStorage.setItem(KEY, String(now));
    return true;
  } catch {
    return false;
  }
}

/** Installs the `vite:preloadError` handler. Call once, at app start. */
export function reloadOnStaleChunk(reload: () => void = () => window.location.reload()): void {
  // No preventDefault: if the page doesn't reload, the import still fails as it would without us.
  window.addEventListener("vite:preloadError", () => {
    if (claimReload()) reload();
  });
}
