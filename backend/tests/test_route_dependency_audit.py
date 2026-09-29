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
Anything that is *neither* — a plain Starlette ``Route``, a ``Mount``ed sub-app,
an ``app.add_route`` — has no ``dependant`` to audit, so the walk reports it
instead of dropping it, and ``test_nothing_that_could_serve_api_escapes_the_audit``
fails if one could answer under ``/api`` (t-route-audit-non-apiroute-gap).
``test_audit_covers_every_documented_operation`` cross-checks the walk against
the OpenAPI document, which FastAPI builds by its own traversal, so a walk that
loses *part* of the tree — one nesting level, not all of it — is also a failure
rather than a smaller, still-green audit.

**GET/HEAD pairs are checked here too, and that is a deliberate file choice.**
``test_every_get_head_pair_resolves_the_same_dependencies``
(t-head-route-kwarg-divergence) guards a different failure from the guard audit:
not one route declaring the wrong guard, but two routes on one path drifting
apart. The HEAD sibling shares the GET route's *handler*, so dependencies in the
handler signature stay in sync by construction — but a route-level
``dependencies=[...]`` kwarg belongs to the decorator, reaches one route of the
pair, and leaves HEAD serving what GET now refuses. Neither check subsumes the
other; they share this module so they share one traversal of ``app.routes``
rather than growing two.
"""

from __future__ import annotations

from collections import defaultdict
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


def _walk_tree(routes: Any) -> tuple[list[Any], list[Any]]:
    """
    ``(leaves, opaque)`` — auditable routes, and every node the walk cannot audit.

    A leaf carries a ``dependant``; a branch carries ``effective_candidates``.
    Anything else is *opaque*: it may well serve requests, but there is no
    dependency tree on it to inspect. Opaque nodes are returned rather than
    skipped, because skipping is how a live ``POST /api/leaky`` once sat outside
    a green audit.
    """
    leaves: list[Any] = []
    opaque: list[Any] = []
    for route in routes:
        if hasattr(route, "effective_candidates"):
            sub_leaves, sub_opaque = _walk_tree(route.effective_candidates())
            leaves.extend(sub_leaves)
            opaque.extend(sub_opaque)
        elif hasattr(route, "dependant"):
            leaves.append(route)
        else:
            opaque.append(route)
    return leaves, opaque


def _walk(routes: Any) -> list[Any]:
    """Flatten the app's route tree, descending into lazily-included routers."""
    return _walk_tree(routes)[0]


def _could_serve_api(path: str | None) -> bool:
    """
    Whether a node mounted at ``path`` could answer an ``/api`` request.

    ``/api...`` obviously. Also the root (``""`` / ``"/"``): a ``Mount`` there
    matches every path, ``/api`` included. And no path at all (a ``Host``
    router, say) — unprovable, so treated as reachable.
    """
    if path is None:
        return True
    trimmed = path.rstrip("/")
    return trimmed == "" or trimmed.startswith("/api")


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

    # Equality, both directions on purpose: a guarded method dropping to zero
    # routes is a walk that lost part of the tree, and a method with no agreed
    # guard is a decision nobody made. The message names which side failed, so a
    # new verb does not read as "a method is missing" (t-route-audit-new-verb-
    # double-failure) — its per-route case below fails with the actionable text.
    methods = {method for method, _ in AUDITED}
    missing = sorted(set(EXPECTED_GUARD) - methods)
    unagreed = sorted(methods - set(EXPECTED_GUARD))
    assert methods == set(EXPECTED_GUARD), (
        f"audited methods {sorted(methods)} differ from the guarded set {sorted(EXPECTED_GUARD)}: "
        f"no route found for {missing or 'none'} (the walk lost them, or the routes are gone); "
        f"routes with no agreed guard: {unagreed or 'none'} (see the per-route failure)"
    )


def test_nothing_that_could_serve_api_escapes_the_audit() -> None:
    """
    No node the walk cannot audit sits where it could answer an ``/api`` request.

    A Starlette ``Route``, a ``Mount``ed sub-app or an ``app.add_route`` has no
    ``dependant``, so the guard audit has nothing to read on it — and before this
    check it was dropped silently, leaving a live, unguarded ``/api`` handler
    outside a green suite. The ones that exist today (``/docs``,
    ``/openapi.json``, the ``/assets`` mount) are all outside ``/api``.
    """
    _, opaque = _walk_tree(app.routes)
    escaped = [
        f"{type(node).__name__} {getattr(node, 'path', '<no path>')!r}"
        for node in opaque
        if _could_serve_api(getattr(node, "path", None))
    ]
    assert not escaped, (
        f"routes that could answer under /api but carry no FastAPI dependency tree: {escaped} — "
        "register them as APIRoutes with a trip guard, or move them outside /api"
    )


def test_audit_covers_every_documented_operation() -> None:
    """
    Every ``/api`` operation in the OpenAPI document is one the walk found.

    ``test_audit_is_not_vacuous`` catches a *total* break — nothing found, or a
    whole method gone. It cannot see a *partial* one: a FastAPI that flattened
    one nesting level but not another would leave a non-empty set with every
    method present, and a smaller audit would stay green. The document is built
    by FastAPI's own traversal, independently of ``_walk``, so a documented
    operation the walk cannot see is exactly that partial loss. (Schema-excluded
    routes — the HEAD siblings — are not in the document; the pair check and
    ``test_head_method.py`` cover those.)
    """
    documented = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api")
        for method in operations
    }
    walked = {(method, route.path_format) for method, route in _api_routes()}

    assert documented, "the OpenAPI document has no /api operations — this check is vacuous"
    assert documented <= walked, (
        f"documented but not walked, so never audited: {sorted(documented - walked)}"
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


# --------------------------------------------------------------------------
# GET/HEAD pairs resolve the same dependencies (t-head-route-kwarg-divergence)
# --------------------------------------------------------------------------


def _dependency_tree(dependant: Any) -> tuple[Any, ...]:
    """
    A comparable rendering of a resolved ``Dependant``, at every depth.

    The callable, the parameter name it binds to, and the request parameters it
    reads, then the same for each sub-dependency, in resolution order. Two
    routes with equal trees run the same dependencies against the same inputs.
    """
    return (
        dependant.call,
        dependant.name,
        dependant.use_cache,
        tuple(p.name for p in dependant.path_params),
        tuple(p.name for p in dependant.query_params),
        tuple(p.name for p in dependant.header_params),
        tuple(p.name for p in dependant.cookie_params),
        tuple(p.name for p in dependant.body_params),
        tuple(_dependency_tree(sub) for sub in dependant.dependencies),
    )


def _get_head_pairs() -> list[tuple[str, Any, Any]]:
    """
    ``(path, GET route, HEAD route)`` for every ``/api`` path that registers HEAD separately.

    A route that carries both verbs itself (the ``/api/{rest:path}`` catch-all)
    is one object and cannot diverge from itself, so only *distinct* route
    objects pair up.
    """
    by_path: dict[str, list[Any]] = defaultdict(list)
    for route in _walk(app.routes):
        if route.path.startswith("/api"):
            by_path[route.path].append(route)

    return [
        (path, get, head)
        for path, routes in by_path.items()
        for get in routes
        if "GET" in (get.methods or ())
        for head in routes
        if head is not get and "HEAD" in (head.methods or ())
    ]


PAIRS = _get_head_pairs()


def test_every_separately_registered_head_route_is_paired() -> None:
    """
    The pair check below found every HEAD sibling — its own non-vacuity guard.

    Derived, not listed: every ``/api`` route whose methods are exactly
    ``{"HEAD"}`` must appear in ``PAIRS``. A HEAD route the pairing misses is a
    HEAD route whose dependencies nobody compares.
    """
    head_only = {
        id(route)
        for route in _walk(app.routes)
        if route.path.startswith("/api") and set(route.methods or ()) == {"HEAD"}
    }
    paired = {id(head) for _, _, head in PAIRS}

    assert {"/api/health", "/api/trips/{slug}"} <= {path for path, _, _ in PAIRS}, (
        f"known GET/HEAD pairs are missing — the pairing is broken: {[p for p, _, _ in PAIRS]}"
    )
    assert head_only <= paired, "a separately registered HEAD route has no GET sibling to compare"


@pytest.mark.parametrize(
    ("path", "get", "head"),
    PAIRS,
    ids=[path for path, _, _ in PAIRS],
)
def test_every_get_head_pair_resolves_the_same_dependencies(path: str, get: Any, head: Any) -> None:
    """
    The GET route and its HEAD sibling run the same handler with the same dependency tree.

    The failure is silent in every other test: add ``dependencies=[Depends(...)]``
    to the ``@router.get`` decorator and GET enforces it while HEAD — a separate
    route object, registered by ``add_api_route`` without the kwarg — does not.
    Both routes still answer, ``test_head_method.py`` compares them only on an
    unknown slug (404 either way), and a guard that reached only GET would leave
    HEAD serving the requests GET now refuses.

    The route-level ``dependencies`` lists are compared on their own as well as
    inside the resolved tree, so the failure message points at the kwarg.
    """
    assert get.endpoint is head.endpoint, (
        f"{path}: HEAD is served by `{head.endpoint.__name__}`, GET by `{get.endpoint.__name__}` "
        "— the HEAD sibling must reuse the GET handler"
    )

    get_route_level = [d.dependency for d in get.dependencies]
    head_route_level = [d.dependency for d in head.dependencies]
    assert get_route_level == head_route_level, (
        f"{path}: route-level dependencies=[...] differ — GET {get_route_level}, "
        f"HEAD {head_route_level}. The kwarg belongs to the decorator, not the handler, "
        "so pass the same list to the HEAD add_api_route (or declare it in the handler signature)"
    )

    assert _dependency_tree(get.dependant) == _dependency_tree(head.dependant), (
        f"{path}: the GET and HEAD routes resolve different dependency trees"
    )
