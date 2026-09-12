import { createFileRoute } from "@tanstack/react-router";

// Trip home (spec Section 6, screen 2): map + trail + timeline feed.
// Built in Week 3 once the API contract (Session 1) and map GeoJSON endpoint
// (Week 2) exist — this is routing scaffolding only.
export const Route = createFileRoute("/")({
  component: TripHome,
});

function TripHome() {
  return <div>Bike Trip Journal</div>;
}
