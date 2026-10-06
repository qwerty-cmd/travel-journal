import { afterEach, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import * as icons from "./index";

afterEach(cleanup);

const { Icon: _icon, ...components } = icons as Record<string, unknown>;
const entries = Object.entries(components) as [string, (p: icons.IconProps) => React.JSX.Element][];

test("index exports every DESIGN.md 4.5 icon", () => {
  expect(entries.map(([n]) => n).sort()).toEqual(
    [
      "PlusIcon", "ArrowLeftIcon", "MapPinIcon", "PinApproxIcon", "ListIcon", "BikeIcon",
      "UsersIcon", "SettingsIcon", "CameraIcon", "ImageIcon", "CloudOffIcon", "ClockIcon",
      "AlertTriangleIcon", "CheckCircleIcon", "XIcon", "DownloadIcon", "LockIcon", "GlobeIcon",
      "EyeIcon", "EyeOffIcon", "CrosshairIcon", "UserIcon",
    ].sort(),
  );
});

test.each(entries)("%s is decorative by default", (_n, Cmp) => {
  const { container } = render(<Cmp />);
  const svg = container.querySelector("svg")!;
  expect(svg.getAttribute("aria-hidden")).toBe("true");
  expect(svg.getAttribute("role")).toBeNull();
  expect(svg.querySelector("title")).toBeNull();
});

test.each(entries)("%s is an img with a name when titled", (_n, Cmp) => {
  render(<Cmp title="Label" size="sm" />);
  const img = screen.getByRole("img", { name: "Label" });
  expect(img.getAttribute("aria-hidden")).toBeNull();
  expect(img.style.width).toBe("var(--size-icon-sm)");
});
