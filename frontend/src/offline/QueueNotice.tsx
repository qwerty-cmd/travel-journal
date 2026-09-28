import { useEffect, useState } from "react";
import { dismiss, subscribe, type QueueRecord } from "./queue";

const label = (e: QueueRecord) => (e.kind === "photo" ? `Photo for ${e.payload.stopName}` : e.payload.data.name);
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * The rider's offline indicator (spec §6 screen 5): a count of every non-failed
 * entry still waiting to send, split into stops and photos, plus the entries
 * that need attention (docs/api-contract.md, Error envelope) — every failed
 * entry until dismissed, and every entry still retrying after 10+ attempts.
 * Renders nothing only when the queue is empty.
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
