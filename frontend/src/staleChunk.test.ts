import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { RELOAD_WINDOW_MS, claimReload, reloadOnStaleChunk } from "./staleChunk";

// Stale-chunk reload after a deploy: one reload per window, never a loop, and
// no reload at all when sessionStorage can't record the guard.

const T0 = 1_800_000_000_000;

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("claimReload", () => {
  test("first failure claims a reload and records its time", () => {
    expect(claimReload(T0)).toBe(true);
    expect(Object.values({ ...sessionStorage })).toContain(String(T0));
  });

  test("a second failure inside the window is refused: no reload loop", () => {
    expect(claimReload(T0)).toBe(true);
    expect(claimReload(T0 + 1)).toBe(false);
    expect(claimReload(T0 + RELOAD_WINDOW_MS - 1)).toBe(false);
  });

  test("a failure after the window (a later deploy) claims again", () => {
    expect(claimReload(T0)).toBe(true);
    expect(claimReload(T0 + RELOAD_WINDOW_MS)).toBe(true);
    expect(claimReload(T0 + RELOAD_WINDOW_MS + 1)).toBe(false);
  });

  test("a garbage stored value does not block the reload", () => {
    sessionStorage.setItem("btj.staleChunkReloadAt", "not-a-number");
    expect(claimReload(T0)).toBe(true);
  });

  test.each([
    ["getItem throws", () => vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); })],
    ["setItem throws", () => vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); })],
  ])("%s: no reload, no throw", (_, arrange) => {
    arrange();
    expect(() => claimReload(T0)).not.toThrow();
    expect(claimReload(T0)).toBe(false);
  });
});

describe("reloadOnStaleChunk", () => {
  test("vite:preloadError reloads once; a repeat right after does not", () => {
    const reload = vi.fn();
    const add = vi.spyOn(window, "addEventListener");
    reloadOnStaleChunk(reload);
    const fire = () => {
      const e = new Event("vite:preloadError", { cancelable: true });
      window.dispatchEvent(e);
      return e;
    };
    const first = fire();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(first.defaultPrevented).toBe(false);
    fire();
    expect(reload).toHaveBeenCalledTimes(1);
    const [type, fn] = add.mock.calls[0];
    window.removeEventListener(type, fn as EventListener);
  });
});
