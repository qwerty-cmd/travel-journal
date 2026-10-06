import { forwardRef, type ReactNode } from "react";
import { AlertTriangleIcon, CheckCircleIcon, ClockIcon, CloudOffIcon } from "../icons";
import "./StatusNotice.css";

// Design feature: an inline banner (DESIGN.md §5.6).
// Design format: 24 px icon + text stack (title body 600, optional detail small).
// Tones: info (clock), success (check-circle), warning (alert-triangle), danger
// (alert-triangle; role="alert" when `alert` is set, i.e. it answers a user action),
// offline (cloud-off). Never auto-dismisses. Focusable (tabIndex -1) so a form can
// move focus to its error summary.
// APIs called: none.
export interface StatusNoticeProps {
  tone: "info" | "success" | "warning" | "danger" | "offline";
  title: ReactNode;
  detail?: ReactNode;
  alert?: boolean;
}

const ICONS = { info: ClockIcon, success: CheckCircleIcon, warning: AlertTriangleIcon, danger: AlertTriangleIcon, offline: CloudOffIcon };

export const StatusNotice = forwardRef<HTMLDivElement, StatusNoticeProps>(function StatusNotice({ tone, title, detail, alert }, ref) {
  const Icon = ICONS[tone];
  return (
    <div ref={ref} tabIndex={-1} className={`notice notice--${tone}`} role={alert ? "alert" : "status"}>
      <Icon />
      <div className="notice__text">
        <p className="notice__title">{title}</p>
        {detail ? <p className="notice__detail">{detail}</p> : null}
      </div>
    </div>
  );
});
