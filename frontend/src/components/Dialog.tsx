import { useEffect, useRef, type ReactNode } from "react";
import "./Dialog.css";

// Design feature: modal dialog and ConfirmDialog shell (DESIGN.md §5.8).
// Design format: native <dialog> opened with showModal() (focus trap, Escape, inert
// background); scrim surface/scrim; card radius/lg, elevation/3, padding 24, max 440
// px; a bottom sheet on phones. `dismissible={false}` blocks Escape and scrim clicks
// (the recovery-code dialog until its checkbox is ticked). Where showModal is missing
// (iOS Safari < 15.4) it falls back to the `open` attribute.
// APIs called: none.
export interface DialogProps {
  open: boolean;
  onClose: () => void;
  titleId: string;
  dismissible?: boolean;
  children: ReactNode;
}

export function Dialog({ open, onClose, titleId, dismissible = true, children }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) {
      if (typeof el.showModal === "function") el.showModal();
      else el.setAttribute("open", "");
    } else if (!open && el.open) {
      if (typeof el.close === "function") el.close();
      else el.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        if (dismissible) onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current && dismissible) onClose();
      }}
    >
      {open ? <div className="dialog__card">{children}</div> : null}
    </dialog>
  );
}

/** The button row inside a dialog: stacked on phones (primary first), right-aligned from bp/md. */
export function DialogActions({ children }: { children: ReactNode }) {
  return <div className="dialog__actions">{children}</div>;
}
