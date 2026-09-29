import { useState } from "react";
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import { getLastSlug } from "../localStore";
import { parseTripLink } from "../trip";

// Design feature: `/` (decision-log Entry 18). Sends the user back to the last
// trip opened on this device, or, when there is none (e.g. first launch of the
// installed iOS app, whose storage is separate from Safari's), asks for the
// trip link.
// Design format: beforeLoad redirects (replace) to /t/$slug when localStorage
// has a last slug (getLastSlug). Otherwise a one-field form, "Paste your trip
// link", with an "Open trip" button. parseTripLink accepts a full URL whose
// path is /t/<slug> or a bare slug; anything else shows "That doesn't look
// like a trip link." and stays put.
// APIs called: none. The slug is not checked against the server here; the
// /t/$slug shell does that and shows "Trip not found" for a NOT_FOUND envelope.
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
