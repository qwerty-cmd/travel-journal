import { useState } from "react";
import { setDisplayName } from "../localStore";

/**
 * Design feature: asks a rider for the name that labels their uploads
 * (`uploadedBy`) — a label, not an identity check. Shown by the /t/$slug shell
 * only when the server says `access === "rider"` and this device has no saved
 * name; viewers never see it.
 *
 * Design format: one "Your name" field and a Save button. A blank or
 * whitespace-only name shows "Please enter your name." On save, stores the
 * trimmed name in localStorage (`btj.displayName`, via setDisplayName), then
 * calls `onSaved`. The name is per device, not per trip.
 *
 * APIs called: none. The name is sent later as `uploadedBy` on queued photo
 * uploads.
 */
export function DisplayNamePrompt({ onSaved }: { onSaved: () => void }) {
  const [name, setName] = useState("");
  const [invalid, setInvalid] = useState(false);

  return (
    <form
      style={{ padding: 16 }}
      onSubmit={(e) => {
        e.preventDefault();
        const trimmed = name.trim();
        setInvalid(!trimmed);
        if (!trimmed) return;
        setDisplayName(trimmed);
        onSaved();
      }}
    >
      <label>
        Your name
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          style={{ display: "block", width: "100%" }}
        />
      </label>
      {invalid && <p role="alert">Please enter your name.</p>}
      <button type="submit">Save</button>
    </form>
  );
}
