"""
``POST /api/v2/trips/claim`` is registered before every ``/api/v2/trips/{tripId}`` route.

Contract, endpoint table notes: "`/claim` is registered before the `{tripId}`
routes". Routes match in registration order, so a ``{tripId}`` route registered
first could take ``claim`` as a trip id. The order is read with the route
audit's own walk (``test_route_dependency_audit._walk``), which descends into
FastAPI's lazily included routers in the order they match.
"""

from __future__ import annotations

from test_route_dependency_audit import _walk

from app.main import app

TRIP_ID_PREFIX = "/api/v2/trips/{tripId}"


def test_claim_is_registered_before_every_trip_id_route() -> None:
    routes = [route.path for route in _walk(app.routes)]
    claim = routes.index("/api/v2/trips/claim")
    trip_id_routes = [i for i, path in enumerate(routes) if path.startswith(TRIP_ID_PREFIX)]

    assert trip_id_routes, "no /api/v2/trips/{tripId} route found; the check would be vacuous"
    assert claim < min(trip_id_routes), (
        f"/api/v2/trips/claim is route #{claim}, after {routes[min(trip_id_routes)]} "
        f"(#{min(trip_id_routes)})"
    )
