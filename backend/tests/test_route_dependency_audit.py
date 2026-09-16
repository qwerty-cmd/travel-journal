"""
Every registered ``/api`` route declares the right access guard for its method.

The failure this exists to catch is silent. ``require_trip_access`` and
``require_rider_access`` have the same signature and return the same
``TripContext``, so a write endpoint that declares the read guard by mistake
still compiles, still resolves the slug, still returns 200 — and now accepts the
*viewer* slug on a write. Nothing in a per-endpoint test suite notices, because
each endpoint's own tests assert the behaviour of the guard it happens to
declare. The guard is declared by hand, once per route, in five different
modules; it only takes one.

Two things about how this is written are load-bearing, and both come from
decision-log entry 7b — a green test that is green because it checked nothing
looks exactly like a green test that checked everything:

**The route set is derived from the imported production app, never listed.**
``from app.main import app``, then walk. A route added tomorrow is audited
tomorrow with no edit here. A hand-maintained list of paths would reintroduce
precisely the blind spot this module exists to close: the route someone forgot
to add to the list is the same route they forgot to guard.

**The walk descends through FastAPI's lazy router inclusion.** Since FastAPI
0.141 ``app.routes`` does *not* contain the routes of an included router; it
contains one opaque ``_IncludedRouter`` node with ``path=None`` and
``methods=None``, whose real routes come from ``effective_candidates()`` with
prefixes already composed. A scan of ``app.routes`` keyed on ``route.path``
therefore sees ``/api/health``, the catch-all, and *none* of the contract
endpoints — it passes, vacuously, having audited nothing. The recursion below is
duck-typed (``effective_candidates`` → branch, ``dependant`` → leaf) rather than
keyed on the private class, so it also works on the flat pre-0.141 layout, and
``test_audit_is_not_vacuous`` fails loudly if a future version breaks both.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.security import require_rider_access, require_trip_access
from app.main import app

# The guard each HTTP method must declare. A method absent from this mapping —
# a DELETE endpoint, say — is a hard failure rather than a skip: a new verb is a
# new access-control decision, and it should be made here deliberately, not
# inherited by omission.
EXPECTED_GUARD = {
    "GET": require_trip_access,
    "HEAD": require_trip_access,
    "POST": require_rider_access,
    "PATCH": require_rider_access,
}

# The only two routes under /api that legitimately carry no trip guard, excluded
# by route *name* so that renaming a path cannot quietly widen the exemption:
#
#   health            — the container liveness probe. It reads no trip, takes no
#                       {slug}, and must answer before any trip exists.
#   unknown_api_path  — the /api/{rest:path} catch-all that turns an unmatched
#                       API path into the JSON error envelope instead of letting
#                       it fall through to the SPA's index.html. It has no trip
#                       to guard; it exists to 404.
#
# Nothing else is exempt. Every other /api route is asserted to sit under
# /api/trips and to declare a guard — so an unguarded /api/whatever added later
# fails this suite rather than shipping.
UNGUARDED_BY_DESIGN = {"health", "unknown_api_path"}


def _walk(routes: Any) -> list[Any]:
    """Flatten the app's route tree, descending into lazily-included routers."""
    flat = []
    for route in routes:
        if hasattr(route, "effective_candidates"):
            flat.extend(_walk(route.effective_candidates()))
        elif hasattr(route, "dependant"):
            flat.append(route)
    return flat


def _declared_guards(route: Any) -> set[Any]:
    """The access guards reachable from ``route.dependant``, at any depth."""
    found = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dependant = stack.pop()
        if dependant.call in (require_trip_access, require_rider_access):
            found.add(dependant.call)
        stack.extend(dependant.dependencies)
    return found


def _api_routes() -> list[tuple[str, Any]]:
    """Every (method, route) pair registered under /api, one row per method."""
    return [
        (method, route)
        for route in _walk(app.routes)
        if route.path.startswith("/api")
        for method in sorted(route.methods or ())
    ]


AUDITED = [
    (method, route) for method, route in _api_routes() if route.name not in UNGUARDED_BY_DESIGN
]


def test_audit_is_not_vacuous() -> None:
    """
    The enumeration actually found routes — the one way this file can lie.

    An audit that walks zero routes passes every parametrised case below by
    having none, and is indistinguishable from a clean bill of health. So assert
    the shape of what was found: real routes, and at least one of every method
    the guard mapping covers. If FastAPI changes its route tree again, this is
    the test that goes red instead of the suite going quietly green.
    """
    assert AUDITED, "route enumeration found nothing — the walk is broken, not the app"

    methods = {method for method, _ in AUDITED}
    assert methods == set(EXPECTED_GUARD), (
        f"expected at least one route per guarded method, found {sorted(methods)}"
    )


def test_every_audited_route_is_under_trips() -> None:
    """Anything else under /api is either exempt by name above, or unguardable."""
    stray = sorted({route.path for _, route in AUDITED if not route.path.startswith("/api/trips")})
    assert not stray, (
        f"/api routes outside /api/trips and not exempt by name: {stray} — "
        "either they need a trip guard or UNGUARDED_BY_DESIGN needs a reasoned entry"
    )


def test_schema_excluded_head_routes_are_audited() -> None:
    """
    The HEAD siblings are in scope, not filtered out.

    HEAD is registered as a second route with ``include_in_schema=False`` so the
    OpenAPI document does not grow a duplicate operation (see
    ``test_head_method.py``). That flag makes them invisible to anything derived
    from ``app.openapi()`` — but they run the same handler against the same
    ``{slug}``, so an unguarded HEAD leaks exactly what an unguarded GET does.
    """
    head = [route for method, route in AUDITED if method == "HEAD"]
    assert head, "no HEAD routes audited — they are being filtered out somewhere"
    assert all(not route.include_in_schema for route in head)


@pytest.mark.parametrize(
    ("method", "route"),
    AUDITED,
    ids=[f"{method} {route.path}" for method, route in AUDITED],
)
def test_route_declares_the_guard_its_method_requires(method: str, route: Any) -> None:
    """
    Reads take either slug; writes take the rider slug only.

    Asserted as an exact set, so declaring *both* guards fails too: two
    resolutions of one ``{slug}`` is an ambiguity about which one enforces, and
    the answer would depend on parameter ordering.
    """
    assert method in EXPECTED_GUARD, (
        f"{method} {route.path} uses a verb with no agreed guard — "
        f"add it to EXPECTED_GUARD with a decision, do not leave it unaudited"
    )

    expected = EXPECTED_GUARD[method]
    assert _declared_guards(route) == {expected}, (
        f"{method} {route.path} (`{route.name}`) must declare `{expected.__name__}`; "
        f"it declares {sorted(g.__name__ for g in _declared_guards(route)) or 'no trip guard'}"
    )
