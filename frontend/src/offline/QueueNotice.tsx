import { useEffect, useState } from "react";
import { dismiss, subscribe, type QueueRecord } from "./queue";

/**
 * The rider's view of queued writes that need attention (docs/api-contract.md,
 * Error envelope): every failed entry until dismissed, and every entry still
 * retrying after 10+ attempts. Renders nothing otherwise.
 */
export function QueueNotice() {
  const [entries, setEntries] = useState<QueueRecord[]>([]);
  useEffect(() => subscribe(setEntries), []);

  const failed = entries.filter((e) => e.failed);
  const stuck = entries.filter((e) => !e.failed && e.attempts >= 10);
  if (failed.length === 0 && stuck.length === 0) return null;

  return (
    <section aria-label="Unsent stops" style={{ padding: 16 }}>
      <ul>
        {failed.map((e) => (
          <li key={e.key}>
            <strong>{e.payload.data.name}</strong> could not be sent: {e.lastError}{" "}
            <button type="button" onClick={() => dismiss(e.key).catch(console.error)}>
              Dismiss
            </button>
          </li>
        ))}
        {stuck.map((e) => (
          <li key={e.key}>
            <strong>{e.payload.data.name}</strong> still trying: {e.lastError}
          </li>
        ))}
      </ul>
    </section>
  );
}
