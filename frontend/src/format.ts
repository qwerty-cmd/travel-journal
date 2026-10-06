/**
 * Formats an instant for display in the viewer's own time zone, labelled with
 * that zone's short name (e.g. "ACST", "GMT+9:30"). Stop times are shown in
 * the reader's zone, not the rider's, so the label is what stops a reader in
 * another zone misreading them. Locale and zone come from the browser; the
 * label's exact text depends on the runtime's Intl data.
 *
 * APIs called: none.
 */
export function formatInstant(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { timeZoneName: "short" });
}

/** A calendar date ("2026-10-12") as "12 Oct 2026". Parsed as UTC so no zone shifts the day. */
export function formatDate(isoDate: string): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return isoDate;
  return d.toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** "2 days ago", "3 hours ago", "just now": the largest whole unit between `iso` and `now`. */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31536000],
    ["month", 2592000],
    ["week", 604800],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  const rtf = new Intl.RelativeTimeFormat("en-AU", { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.trunc(seconds / size), unit);
  }
  return "just now";
}
