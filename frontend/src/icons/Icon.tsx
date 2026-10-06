import type { ReactNode, SVGProps } from "react";

export interface IconProps
  extends Omit<SVGProps<SVGSVGElement>, "children" | "title" | "size"> {
  /** Maps to the size/icon/* tokens: md = 24 px (buttons, bars), sm = 18 px (badges, notices). */
  size?: "md" | "sm";
  /** Accessible name. When set the icon is role="img"; otherwise it is decorative (aria-hidden). */
  title?: string;
}

/** Shared 24 px-grid SVG shell (DESIGN.md 4.5). Inherits colour via currentColor. */
export function Icon({ size = "md", title, style, children, ...rest }: IconProps & { children?: ReactNode }) {
  const dim = `var(--size-icon-${size})`;
  const a11y = title
    ? ({ role: "img" } as const)
    : ({ "aria-hidden": true, focusable: false } as const);
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ width: dim, height: dim, flexShrink: 0, ...style }}
      {...a11y}
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  );
}
