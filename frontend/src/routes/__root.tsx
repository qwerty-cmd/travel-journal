import { createRootRoute, Outlet } from "@tanstack/react-router";
import { QueueNotice } from "../offline/QueueNotice";

// Design feature: root shell for every screen (spec Section 6). The offline
// queue notice lives here so an unsent or failed stop/photo is visible on every
// route, including the paste-link screen.
// Design format: <QueueNotice /> (renders nothing when the queue is empty),
// then the matched child route via <Outlet />. No header or layout of its own;
// the trip header belongs to the /t/$slug shell.
// APIs called: none. QueueNotice reads only the local IndexedDB queue; sending
// is done by the drain started in main.tsx (startQueue), not by this route.
export const Route = createRootRoute({
  component: () => (
    <>
      <QueueNotice />
      <Outlet />
    </>
  ),
});
