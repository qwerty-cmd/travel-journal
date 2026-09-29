import { useEffect, useState } from "react";
import { dismiss, subscribe, type QueueRecord } from "./queue";

const label = (e: QueueRecord) => (e.kind === "photo" ? `Photo for ${e.payload.stopName}` : e.payload.data.name);
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * Design feature: the rider's offline indicator (spec §6 screen 5), mounted in
 * the root shell so it shows on every screen. Tells the rider what hasn't
 * reached the server yet and what needs attention (docs/api-contract.md, Error
 * envelope).
 *
 * Design format: renders nothing only when the queue is empty. Otherwise a
 * section labelled "Unsent stops" with:
 * - "Waiting to send: N stops, M photos" — every non-failed entry;
 * - per failed entry: "<label> could not be sent: <lastError>" and a Dismiss
 *   button (the only way a failed entry leaves the queue);
 * - per entry still retrying after 10+ attempts: "<label> still trying:
 *   <lastError>".
 * The label is the stop name, or "Photo for <stop name>". It updates live via
 * `subscribe`, including changes made in other tabs (BroadcastChannel
 * `btj-queue`).
 *
 * APIs called: none. Reads and deletes local IndexedDB entries only; the drain
 * in `queue.ts` does the sending.
 */
export function QueueNotice() {
  const [entries, setEntries] = useState<QueueRecord[]>([]);
  useEffect(() => subscribe(setEntries), []);

  const failed = entries.filter((e) => e.failed);
  const pending = entries.filter((e) => !e.failed);
  const stuck = pending.filter((e) => e.attempts >= 10);
  if (entries.length === 0) return null;

  const pendingStops = pending.filter((e) => e.kind === "stop").length;
  const pendingPhotos = pending.length - pendingStops;
  const waiting = [pendingStops && plural(pendingStops, "stop"), pendingPhotos && plural(pendingPhotos, "photo")].filter(Boolean);

  return (
    <section aria-label="Unsent stops" style={{ padding: 16 }}>
      {waiting.length > 0 && <p>Waiting to send: {waiting.join(", ")}</p>}
      <ul>
        {failed.map((e) => (
          <li key={e.key}>
            <strong>{label(e)}</strong> could not be sent: {e.lastError}{" "}
            <button type="button" onClick={() => dismiss(e.key).catch(console.error)}>
              Dismiss
            </button>
          </li>
        ))}
        {stuck.map((e) => (
          <li key={e.key}>
            <strong>{label(e)}</strong> still trying: {e.lastError}
          </li>
        ))}
      </ul>
    </section>
  );
}
