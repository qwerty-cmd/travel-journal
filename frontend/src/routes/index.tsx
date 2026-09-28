import { useState } from "react";
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import { getLastSlug } from "../localStore";
import { parseTripLink } from "../trip";

// `/` (decision-log Entry 18): back to the last trip opened on this device, or,
// when there is none (e.g. first launch of the installed iOS app, whose storage
// is separate from Safari's), a one-field "paste your trip link" screen.
export const Route = createFileRoute("/")({
  beforeLoad: () => {
    const slug = getLastSlug();
    if (slug) throw redirect({ to: "/t/$slug", params: { slug }, replace: true });
  },
  component: PasteLink,
});

function PasteLink() {
  const navigate = useNavigate();
  const [link, setLink] = useState("");
  const [invalid, setInvalid] = useState(false);

  return (
    <form
      style={{ padding: 16 }}
      onSubmit={(e) => {
        e.preventDefault();
        const slug = parseTripLink(link);
        setInvalid(!slug);
        if (slug) navigate({ to: "/t/$slug", params: { slug } });
      }}
    >
      <label>
        Paste your trip link
        <input
          value={link}
          onChange={(e) => setLink(e.target.value)}
          style={{ display: "block", width: "100%" }}
        />
      </label>
      {invalid && <p role="alert">That doesn't look like a trip link.</p>}
      <button type="submit">Open trip</button>
    </form>
  );
}
