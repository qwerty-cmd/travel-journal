import type { ButtonHTMLAttributes, ReactNode } from "react";
import "./Button.css";

// Design feature: the one button (DESIGN.md §5.1).
// Design format: variant primary | secondary | tertiary | danger | danger-secondary,
// size md (48 px) | lg (56 px, full width on phones). `loading` swaps the label for
// `loadingLabel`, sets aria-busy and disables the button so it can't be re-clicked.
// APIs called: none.
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "tertiary" | "danger" | "danger-secondary";
  size?: "md" | "lg";
  loading?: boolean;
  loadingLabel?: string;
  leadingIcon?: ReactNode;
}

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  loadingLabel,
  leadingIcon,
  className,
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={["btn", `btn--${variant}`, `btn--${size}`, className].filter(Boolean).join(" ")}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <span className="btn__spinner" aria-hidden="true" /> : leadingIcon}
      {loading && loadingLabel ? loadingLabel : children}
    </button>
  );
}
