import { useEffect, useState } from "react";
import { dismiss, isHeld, isPaused, subscribe, type QueueRecord } from "./queue";

const label = (e: QueueRecord) => (e.kind === "photo" ? `Photo for ${e.payload.stopName}` : e.payload.data.name);
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * How long the object URL outlives the click. Revoking in the same tick can
 * cancel the download in some browsers (notably iOS Safari), so it is revoked
 * shortly after instead.
 */
export const REVOKE_DELAY_MS = 1_000;

/** Downloads a failed photo's stored JPEG via an object URL and an `<a download>`, then revokes the URL. */
function savePhoto(e: QueueRecord & { kind: "photo" }) {
  const url = URL.createObjectURL(new Blob([e.blob], { type: "image/jpeg" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${e.payload.stopName} ${e.payload.data.id}.jpg`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
}

/**
 * Design feature: the rider's offline indicator (spec §6 screen 5), mounted in
 * the root shell so it shows on every screen. Tells the rider what hasn't
 * reached the server yet and what needs attention (docs/api-contract.md, Error
 * envelope).
 *
 * Design format: renders nothing only when the queue is empty. Otherwise a
 * section labelled "Unsent stops" with:
 * - "Waiting to send: N stops, M photos" — every non-failed entry; while the
 *   queue is paused by a 401 this line reads "Sign in to send N items" instead;
 * - "N items were saved by another account on this phone. Sign in as that
 *   account to send them." — non-failed entries held for another account
 *   (`isHeld`), counted apart from the line above (DESIGN.md "Held for another
 *   account"); never names the account;
 * - per failed entry: "<label> could not be sent: <lastError>" and a Dismiss
 *   button (the only way a failed entry leaves the queue); a failed photo also
 *   gets "Save photo to this device", which downloads its stored JPEG;
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
  const [{ entries, paused }, setState] = useState<{ entries: QueueRecord[]; paused: boolean }>({ entries: [], paused: false });
  useEffect(() => subscribe((entries) => setState({ entries, paused: isPaused() })), []);

  const failed = entries.filter((e) => e.failed);
  const held = entries.filter((e) => !e.failed && isHeld(e));
  const pending = entries.filter((e) => !e.failed && !isHeld(e));
  const stuck = pending.filter((e) => e.attempts >= 10);
  if (entries.length === 0) return null;

  const pendingStops = pending.filter((e) => e.kind === "stop").length;
  const pendingPhotos = pending.length - pendingStops;
  const waiting = [pendingStops && plural(pendingStops, "stop"), pendingPhotos && plural(pendingPhotos, "photo")].filter(Boolean);

  return (
    <section aria-label="Unsent stops" style={{ padding: 16 }}>
      {pending.length > 0 &&
        (paused ? <p>Sign in to send {plural(pending.length, "item")}</p> : <p>Waiting to send: {waiting.join(", ")}</p>)}
      {held.length > 0 && (
        <p>
          {plural(held.length, "item")} {held.length === 1 ? "was" : "were"} saved by another account on this phone. Sign in
          as that account to send them.
        </p>
      )}
      <ul>
        {failed.map((e) => (
          <li key={e.key}>
            <strong>{label(e)}</strong> could not be sent: {e.lastError}{" "}
            {e.kind === "photo" && (
              <>
                <button type="button" onClick={() => savePhoto(e)}>
                  Save photo to this device
                </button>{" "}
              </>
            )}
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
