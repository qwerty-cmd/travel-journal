import { afterEach, expect, test, vi } from "vitest";
import { formatInstant } from "./format";

// Architect ruling (A): stop times stay in the viewer's zone, labelled with it.
// TZ is switched per test; Node re-reads it for Date and Intl on change.

const ISO = "2026-10-03T08:30:00Z";
const zonePart = (iso: string) =>
  new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
    .formatToParts(new Date(iso))
    .find((p) => p.type === "timeZoneName")!.value;

afterEach(() => {
  vi.unstubAllEnvs();
});

test("under UTC the output carries the UTC label", () => {
  vi.stubEnv("TZ", "UTC");
  expect(formatInstant(ISO)).toContain("UTC");
});

test("under Australia/Darwin the output is the Darwin wall time with its zone label", () => {
  vi.stubEnv("TZ", "Australia/Darwin");
  expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe("Australia/Darwin");
  const out = formatInstant(ISO);
  expect(zonePart(ISO)).toMatch(/ACST|9:30/);
  expect(out).toContain(zonePart(ISO));
  // 08:30Z is 18:00 in Darwin (UTC+9:30, no DST); the UTC hour must not appear.
  expect(out).toMatch(/\b(6:00|18:00)/);
  expect(out).not.toMatch(/\b(8:30|08:30)/);
});

test("an offset in the input does not leak into the output: same instant, same string", () => {
  vi.stubEnv("TZ", "UTC");
  expect(formatInstant("2026-10-03T18:00:00+09:30")).toBe(formatInstant(ISO));
});
