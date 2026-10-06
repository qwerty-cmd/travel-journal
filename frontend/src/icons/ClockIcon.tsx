// Source: Lucide "clock" (lucide-static 1.52.0), ISC; Feather-derived, MIT. See ./LICENSE.
import { Icon, type IconProps } from "./Icon";

export function ClockIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 6v6l4 2" />
    </Icon>
  );
}
