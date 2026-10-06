import type { ComponentType } from "react";
import { BikeIcon, ClockIcon, GlobeIcon, LockIcon, UsersIcon, type IconProps } from "../icons";
import "./Badge.css";

// Design feature: a small status label (DESIGN.md §5.9). Text is always present;
// the icon is decorative.
// Design format: 24 px tall, padding 0 8, radius/sm, small 600, 18 px icon.
// Variants used so far: public (globe), private (lock), pending (clock), leader
// (users), rider (bike), legacy (no icon; the text is passed in).
// APIs called: none.
export type BadgeVariant = "public" | "private" | "pending" | "leader" | "rider" | "legacy";

const PRESETS: Record<BadgeVariant, { icon?: ComponentType<IconProps>; text: string }> = {
  public: { icon: GlobeIcon, text: "Public" },
  private: { icon: LockIcon, text: "Private" },
  pending: { icon: ClockIcon, text: "Pending" },
  leader: { icon: UsersIcon, text: "Leader" },
  rider: { icon: BikeIcon, text: "Rider" },
  legacy: { text: "" },
};

export function Badge({ variant, children }: { variant: BadgeVariant; children?: string }) {
  const { icon: Icon, text } = PRESETS[variant];
  return (
    <span className={`badge badge--${variant}`}>
      {Icon && <Icon size="sm" />}
      {children ?? text}
    </span>
  );
}
