import { Link } from "@tanstack/react-router";
import type { ViewerRole } from "../api/gen/types/ViewerRole";
import { StatusNotice } from "./StatusNotice";
import "./LegacyNotice.css";

/**
 * Design feature: the persistent "old trip link" notice under the /t/$slug header
 * (legacy-link.md). Legacy routes are read-only; this explains why and where to write.
 *
 * Design format: info StatusNotice "This is an old trip link" on every variant, by viewer.role:
 * anonymous -> "Viewing still works…" + Sign in (next = this URL) + Create an account;
 * rider/leader -> "You're a member…" + primary "Open trip" to /trips/$tripId;
 * none/pending/unknown -> the title only (LegacyJoinPanel supplies the claim / pending copy).
 *
 * APIs called: none (viewer.role comes from the shell's GET /api/trips/{slug}).
 */
export function LegacyNotice({ slug, tripId, role }: { slug: string; tripId: string; role: ViewerRole | undefined }) {
  const member = role === "rider" || role === "leader";
  const anonymous = role === "anonymous";
  return (
    <section className="legacy-notice" aria-label="Old trip link">
      <StatusNotice
        tone="info"
        title="This is an old trip link"
        detail={
          anonymous
            ? "Viewing still works. To add stops and photos, you now need an account and a leader's approval."
            : member
              ? "You're a member of this trip. Open it in the new view to add stops."
              : undefined
        }
      />
      {anonymous && (
        <div className="legacy-notice__actions">
          <Link to="/signin" search={{ next: `/t/${slug}` }} className="btn btn--secondary btn--md">
            Sign in
          </Link>
          <Link to="/signup" className="btn btn--tertiary btn--md">
            Create an account
          </Link>
        </div>
      )}
      {member && (
        <Link to="/trips/$tripId" params={{ tripId }} className="btn btn--primary btn--md">
          Open trip
        </Link>
      )}
    </section>
  );
}
