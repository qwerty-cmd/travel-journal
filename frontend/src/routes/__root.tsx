import { createRootRoute, Outlet } from "@tanstack/react-router";
import { QueueNotice } from "../offline/QueueNotice";

// Shared shell for every screen (spec Section 6). The queue notice lives here
// so an unsent stop is visible on every route.
export const Route = createRootRoute({
  component: () => (
    <>
      <QueueNotice />
      <Outlet />
    </>
  ),
});
