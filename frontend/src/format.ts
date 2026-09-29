/**
 * Formats an instant for display in the viewer's own time zone, labelled with
 * that zone's short name (e.g. "ACST", "GMT+9:30"). Stop times are shown in
 * the reader's zone, not the rider's, so the label is what stops a reader in
 * another zone misreading them.
 */
export function formatInstant(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { timeZoneName: "short" });
}
