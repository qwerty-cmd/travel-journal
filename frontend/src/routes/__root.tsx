import { createRootRoute, Outlet } from "@tanstack/react-router";

// Shared shell for every screen (spec Section 6). Offline indicator lives
// here once src/offline/ exists (Week 3) so it's visible on every route.
export const Route = createRootRoute({
  component: () => <Outlet />,
});
