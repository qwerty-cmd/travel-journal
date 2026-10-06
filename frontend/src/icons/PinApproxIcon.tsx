// Source: in-house original (no stock equivalent; DESIGN.md 4.5): dashed ring around a centre dot.
import { Icon, type IconProps } from "./Icon";

export function PinApproxIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="9" strokeDasharray="3 3.5" />
      <circle cx="12" cy="12" r="2" />
    </Icon>
  );
}
