import type { ReactNode } from "react";
import "./BottomActionBar.css";

// Design feature: the fixed bottom action bar (DESIGN.md §5.13, D6), holding one
// primary lg full-width action (Add stop on the trip, Save stop on add stop).
// Design format: surface/card, elevation/2, padding 12 16 + the safe-area inset,
// pinned to the viewport bottom above the map (z/bar), content max 480 centred.
// A spacer of the same height is rendered in the flow so the bar never covers
// the page's last content (WCAG 2.4.11). `helper` is an optional line above the
// action (add stop's disabled reason).
// APIs called: none.
export function BottomActionBar({ helper, children }: { helper?: ReactNode; children: ReactNode }) {
  return (
    <>
      <div className={helper ? "action-bar__spacer action-bar__spacer--helper" : "action-bar__spacer"} aria-hidden="true" />
      <div className="action-bar">
        <div className="action-bar__inner">
          {helper ? <p className="action-bar__helper">{helper}</p> : null}
          {children}
        </div>
      </div>
    </>
  );
}
