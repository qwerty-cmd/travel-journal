import { useState } from "react";
import { setDisplayName } from "../localStore";

/**
 * Asks a rider for the name that labels their uploads (`uploadedBy`) — a label,
 * not an identity check. Stores the trimmed name, then calls `onSaved`.
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
