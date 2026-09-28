import { createFileRoute } from "@tanstack/react-router";

// Trip home (spec Section 6, screen 2). Map and timeline land here in
// t-frontend-map-pins-trail and t-frontend-timeline-feed.
export const Route = createFileRoute("/t/$slug/")({
  component: TripHome,
});

function TripHome() {
  return <main style={{ padding: 16 }} />;
}
