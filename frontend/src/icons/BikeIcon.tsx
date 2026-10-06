// Source: Lucide "bike" (lucide-static 1.52.0), ISC. See ./LICENSE.
import { Icon, type IconProps } from "./Icon";

export function BikeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="18.5" cy="17.5" r="3.5" />
      <circle cx="5.5" cy="17.5" r="3.5" />
      <circle cx="15" cy="5" r="1" />
      <path d="M12 17.5V14l-3-3 4-3 2 3h2" />
    </Icon>
  );
}
