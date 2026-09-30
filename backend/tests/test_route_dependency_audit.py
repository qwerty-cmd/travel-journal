"""
Every registered ``/api`` route declares the access guard its route class requires.

The failure this exists to catch is silent. ``require_trip_access`` and
``require_trip_writer`` take the same ``{slug}`` and both hand the handler a
``TripContext``, so a write endpoint that declares the read guard by mistake
still compiles, still resolves the slug, still returns 200 — and now accepts a
write from anyone holding either link, signed in or not. The same goes for an account route that forgets
``require_session``: it still answers, for nobody in particular. Nothing in a
per-endpoint test suite notices, because each endpoint's own tests assert the
behaviour of the guard it happens to declare. The guard is declared by hand,
once per route, in several modules; it only takes one.

**Route classes, not HTTP methods** (``t-am-auth-sessions``). The guard used to
be looked up by method alone (GET → read guard, POST → rider guard). Entry 29
added routes where the method doesn't decide it: ``GET /api/v2/auth/me`` is a
read that needs a *session*, not a trip. So every route falls into exactly one
class, decided by name first and path second:

- **Anonymous** (``ANONYMOUS_BY_DESIGN``, by route name): no guard at all.
  Health, the ``/api`` catch-all, signup, signin, signout, recover and the public trip list (``list_public_trips``). A guard declared on one of these fails too — signout, for one, must
  answer ``204`` whether or not a session was sent.
- **Account-scoped** (``ACCOUNT_SCOPED``, by route name): exactly
  ``require_session``, reads and unsafe methods alike, and only under
  ``/api/v2``.
- **Trip-scoped** (everything else): must sit under ``/api/trips`` or
  ``/api/v2/trips``. A read (GET/HEAD) declares the trip read guard. An unsafe
  method declares exactly one membership gate from ``TRIP_UNSAFE_GUARDS`` —
  ``require_trip_writer``, and ``require_trip_leader`` once it is built
  (``t-am-trip-create``) — and nothing else (``t-am-write-gate-legacy``, Entry
  29 obligation 4). ``require_session`` alone is not enough on a trip write: it
  says who is asking, not whether they may write to this trip.

Both allowlists are by *name*, so renaming a path cannot quietly widen an
exemption, and each name must match routes on exactly one path, so a
same-named handler elsewhere cannot ride on it.

Two things about how this is written are load-bearing, and both come from
decision-log entry 7b — a green test that is green because it checked nothing
looks exactly like a green test that checked everything:

**The route set is derived from the imported production app, never listed.**
``from app.main import app``, then walk. A route added tomorrow is audited
tomorrow with no edit here. A hand-maintained list of paths would reintroduce
precisely the blind spot this module exists to close: the route someone forgot
to add to the list is the same route they forgot to guard. (The two allowlists
above list *exemptions*, which is the direction that fails safe: a route
missing from them is held to the strictest rule, the trip-scoped one.)

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
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends

from app.core.security import require_session, require_trip_access, require_trip_writer
from app.main import app

# Every guard the audit recognises. `_declared_guards` reports any of these found
# at any depth of a route's dependency tree.
GUARDS = (require_trip_access, require_trip_writer, require_session)

# Methods that only read, and methods that change something. A method in
# neither set — a DELETE endpoint, say — is a hard failure rather than a skip:
# a new verb is a new access-control decision, and it should be made here
# deliberately, not inherited by omission.
READ_METHODS = frozenset({"GET", "HEAD"})
UNSAFE_METHODS = frozenset({"POST", "PATCH"})

# The trip guard a trip-scoped read must declare.
TRIP_READ_GUARD = require_trip_access

# The membership gates an unsafe trip-scoped route may declare, exactly one of.
# `require_trip_leader` joins this set (and GUARDS) when `t-am-trip-create`
# builds it; until then no route can declare it.
TRIP_UNSAFE_GUARDS = frozenset({require_trip_writer})

# Trip-scoped routes live under one of these (legacy slug routes, v2 trip-id routes).
TRIP_PATH_PREFIXES = ("/api/trips/", "/api/v2/trips")

# Routes under /api that legitimately carry no guard at all, by route *name*:
#
#   health            — the container liveness probe. It reads no trip, takes no
#                       {slug}, and must answer before any trip exists.
#   unknown_api_path  — the /api/{rest:path} catch-all that turns an unmatched
#                       API path into the JSON error envelope instead of letting
#                       it fall through to the SPA's index.html. It has no trip
#                       to guard; it exists to 404. It also carries every verb,
#                       so it is exempt from the method classes above.
#   signup, signin    — how a caller *gets* a session; they can't require one
#                       (contract, "The gates": none (anonymous)).
#   signout           — anonymous with the session optional: always 204, and it
#                       clears the cookie whether or not a session was sent.
#   recover           — how a caller who forgot the password gets a session back
#                       with the recovery code; it can't require one either.
#   list_public_trips — GET/HEAD /api/v2/trips, the Discover list. Public trips
#                       only, the same for every caller; it reads no session and
#                       no single trip, so there is nothing to gate (contract,
#                       "The gates": none (anonymous)).
ANONYMOUS_BY_DESIGN = {
    "health",
    "unknown_api_path",
    "signup",
    "signin",
    "signout",
    "recover",
    "list_public_trips",
}

# Routes about the caller's own account rather than a trip, by route name. Each
# declares exactly `require_session`, and sits under /api/v2.
#
#   get_me               — GET/HEAD /api/v2/auth/me, the signed-in account.
#   signout_all          — POST /api/v2/auth/signout-all, every session of the account.
#   change_password      — POST /api/v2/auth/password, re-confirms the current password.
#   rotate_recovery_code — POST /api/v2/auth/recovery-code, re-confirms the password.
ACCOUNT_SCOPED = {"get_me", "signout_all", "change_password", "rotate_recovery_code"}


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
        if dependant.call in GUARDS:
            found.add(dependant.call)
        stack.extend(dependant.dependencies)
    return found


def _route_class(route: Any) -> str:
    """``"anonymous"``, ``"account"`` or ``"trip"`` — which rule ``route`` is held to."""
    if route.name in ANONYMOUS_BY_DESIGN:
        return "anonymous"
    if route.name in ACCOUNT_SCOPED:
        return "account"
    return "trip"


def _names(guards: set[Any]) -> list[str]:
    return sorted(guard.__name__ for guard in guards)


def _violation(method: str, route: Any) -> str | None:
    """
    Why ``method`` on ``route`` breaks its class's rule, or ``None`` if it doesn't.

    One function for the live audit and for the self-test that feeds it
    miswired routes, so the rules the self-test proves are the rules applied.
    """
    label = f"{method} {route.path} (`{route.name}`)"
    guards = _declared_guards(route)
    route_class = _route_class(route)

    if route_class == "anonymous":
        if guards:
            return f"{label} is anonymous by design but declares {_names(guards)}"
        return None

    if method not in READ_METHODS | UNSAFE_METHODS:
        return (
            f"{label} uses a verb with no agreed guard — add it to READ_METHODS or "
            "UNSAFE_METHODS with a decision, do not leave it unaudited"
        )

    if route_class == "account":
        if not route.path.startswith("/api/v2/"):
            return f"{label} is account-scoped but sits outside /api/v2"
        if guards != {require_session}:
            return f"{label} is account-scoped and must declare exactly `require_session`; " + (
                f"it declares {_names(guards) or 'no guard'}"
            )
        return None

    if not route.path.startswith(TRIP_PATH_PREFIXES):
        return (
            f"{label} is neither under /api/trips or /api/v2/trips nor exempt by name — "
            "give it a trip guard under a trip path, or a reasoned entry in "
            "ANONYMOUS_BY_DESIGN or ACCOUNT_SCOPED"
        )
    if method in READ_METHODS:
        if guards != {TRIP_READ_GUARD}:
            return (
                f"{label} is a trip-scoped read and must declare exactly "
                f"`{TRIP_READ_GUARD.__name__}`; it declares {_names(guards) or 'no trip guard'}"
            )
        return None

    if len(guards) != 1 or not guards <= TRIP_UNSAFE_GUARDS:
        return (
            f"{label} is a trip-scoped write and must declare exactly one of "
            f"{_names(set(TRIP_UNSAFE_GUARDS))}; it declares {_names(guards) or 'no trip guard'}"
        )
    return None


def _api_routes() -> list[tuple[str, Any]]:
    """Every (method, route) pair registered under /api, one row per method."""
    return [
        (method, route)
        for route in _walk(app.routes)
        if route.path.startswith("/api")
        for method in sorted(route.methods or ())
    ]


AUDITED = _api_routes()


def test_audit_is_not_vacuous() -> None:
    """
    The enumeration actually found routes — the one way this file can lie.

    An audit that walks zero routes passes every parametrised case below by
    having none, and is indistinguishable from a clean bill of health. So assert
    the shape of what was found: every route class is populated, and trip-scoped
    routes cover every agreed method. If FastAPI changes its route tree again,
    this is the test that goes red instead of the suite going quietly green.
    """
    assert AUDITED, "route enumeration found nothing — the walk is broken, not the app"

    classes = {_route_class(route) for _, route in AUDITED}
    assert classes == {"anonymous", "account", "trip"}, (
        f"route classes found: {sorted(classes)} — a whole class has no routes, so the walk "
        "lost part of the tree or the allowlists no longer name live routes"
    )

    # Equality, both directions on purpose: an agreed method dropping to zero
    # trip routes is a walk that lost part of the tree, and a method with no
    # agreed class is a decision nobody made. The message names which side
    # failed, so a new verb does not read as "a method is missing" (t-route-
    # audit-new-verb-double-failure) — its per-route case below fails with the
    # actionable text.
    agreed = READ_METHODS | UNSAFE_METHODS
    methods = {method for method, route in AUDITED if _route_class(route) == "trip"}
    missing = sorted(agreed - methods)
    unagreed = sorted(methods - agreed)
    assert methods == agreed, (
        f"trip-scoped methods {sorted(methods)} differ from the agreed set {sorted(agreed)}: "
        f"no route found for {missing or 'none'} (the walk lost them, or the routes are gone); "
        f"routes with no agreed guard: {unagreed or 'none'} (see the per-route failure)"
    )


def test_every_allowlisted_name_is_one_live_path() -> None:
    """
    Each name in ``ANONYMOUS_BY_DESIGN`` and ``ACCOUNT_SCOPED`` matches routes on one path.

    None: the entry is stale, and would silently exempt whatever route next
    takes that name. More than one path: a same-named handler elsewhere is
    riding on an exemption made for a different route.
    """
    paths_by_name: dict[str, set[str]] = defaultdict(set)
    for _, route in AUDITED:
        paths_by_name[route.name].add(route.path)

    assert not ANONYMOUS_BY_DESIGN & ACCOUNT_SCOPED, "a route name is in both allowlists"
    wrong = {
        name: sorted(paths_by_name.get(name, ()))
        for name in sorted(ANONYMOUS_BY_DESIGN | ACCOUNT_SCOPED)
        if len(paths_by_name.get(name, ())) != 1
    }
    assert not wrong, f"allowlisted names that don't match exactly one /api path: {wrong}"


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
        "register them as APIRoutes with a guard, or move them outside /api"
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


def test_every_guarded_route_is_under_trips_or_v2() -> None:
    """Anything under /api outside /api/trips and /api/v2 is exempt by name, or unguardable."""
    stray = sorted(
        {
            route.path
            for _, route in AUDITED
            if route.name not in {"health", "unknown_api_path"}
            and not route.path.startswith(("/api/trips/", "/api/v2/"))
        }
    )
    assert not stray, (
        f"/api routes outside /api/trips and /api/v2 and not exempt by name: {stray} — "
        "either they need a guard under one of those prefixes or a reasoned exemption"
    )


def test_schema_excluded_head_routes_are_audited() -> None:
    """
    The HEAD siblings are in scope, not filtered out.

    HEAD is registered as a second route with ``include_in_schema=False`` so the
    OpenAPI document does not grow a duplicate operation (see
    ``test_head_method.py``). That flag makes them invisible to anything derived
    from ``app.openapi()`` — but they run the same handler with the same guard,
    so an unguarded HEAD leaks exactly what an unguarded GET does.
    """
    head = [route for method, route in AUDITED if method == "HEAD" and route.methods == {"HEAD"}]
    assert head, "no HEAD routes audited — they are being filtered out somewhere"
    assert all(not route.include_in_schema for route in head)
    assert any(_route_class(route) == "account" for route in head), (
        "the account-scoped HEAD sibling (/api/v2/auth/me) is not audited"
    )


@pytest.mark.parametrize(
    ("method", "route"),
    AUDITED,
    ids=[f"{method} {route.path}" for method, route in AUDITED],
)
def test_route_declares_the_guard_its_class_requires(method: str, route: Any) -> None:
    """
    Anonymous routes declare nothing; account routes a session; trip routes a trip guard.

    Asserted as an exact set, so declaring *two* guards fails too: two
    resolutions of one request is an ambiguity about which one enforces, and
    the answer would depend on parameter ordering.
    """
    violation = _violation(method, route)
    assert violation is None, violation


def test_the_rules_reject_miswired_routes() -> None:
    """
    ``_violation`` fails each kind of miswiring — the rules are not vacuous either.

    Real ``APIRoute`` objects on a scratch router, never mounted on the app:
    an account read without its session, a trip write with the read guard, an
    anonymous route with a guard, a trip route outside a trip path, an unagreed
    verb, and a correctly wired pair that must pass.
    """
    scratch = APIRouter()

    async def get_me() -> None:  # the account-scoped name, but no session
        return None

    async def write(guard: Annotated[Any, Depends(require_trip_access)]) -> None:
        return None

    async def signin(user: Annotated[Any, Depends(require_session)]) -> None:
        return None

    async def stray(guard: Annotated[Any, Depends(require_trip_writer)]) -> None:
        return None

    async def read(guard: Annotated[Any, Depends(require_trip_access)]) -> None:
        return None

    scratch.add_api_route("/api/v2/auth/me", get_me, methods=["GET"])
    scratch.add_api_route("/api/trips/{slug}/things", write, methods=["POST"])
    scratch.add_api_route("/api/v2/auth/signin", signin, methods=["POST"])
    scratch.add_api_route("/api/elsewhere/{slug}", stray, methods=["POST"])
    scratch.add_api_route("/api/trips/{slug}/things/{id}", stray, methods=["DELETE"])
    scratch.add_api_route("/api/trips/{slug}/things", read, methods=["GET"])
    scratch.add_api_route("/api/trips/{slug}/things/{id}", stray, methods=["PATCH"])

    # A trip write with only a session: who is asking, but not whether they may
    # write to this trip (t-am-write-gate-legacy).
    async def session_only(user: Annotated[Any, Depends(require_session)]) -> None:
        return None

    # A trip write declaring the membership gate *and* another guard.
    async def doubled(
        guard: Annotated[Any, Depends(require_trip_writer)],
        read: Annotated[Any, Depends(require_trip_access)],
    ) -> None:
        return None

    scratch.add_api_route("/api/trips/{slug}/session-only", session_only, methods=["POST"])
    scratch.add_api_route("/api/trips/{slug}/doubled", doubled, methods=["POST"])
    routes = {(next(iter(r.methods)), r.path): r for r in scratch.routes}

    must_fail = [
        ("GET", "/api/v2/auth/me"),
        ("POST", "/api/trips/{slug}/things"),
        ("POST", "/api/v2/auth/signin"),
        ("POST", "/api/elsewhere/{slug}"),
        ("DELETE", "/api/trips/{slug}/things/{id}"),
        ("POST", "/api/trips/{slug}/session-only"),
        ("POST", "/api/trips/{slug}/doubled"),
    ]
    for key in must_fail:
        assert _violation(key[0], routes[key]) is not None, f"{key} was not rejected"

    for key in [("GET", "/api/trips/{slug}/things"), ("PATCH", "/api/trips/{slug}/things/{id}")]:
        assert _violation(key[0], routes[key]) is None, f"{key} was wrongly rejected"


def test_a_synthetic_app_with_an_unguarded_trip_post_fails_the_audit() -> None:
    """
    An app with one unguarded ``POST`` under ``/api/trips/{slug}`` fails, end to end.

    ``test_the_rules_reject_miswired_routes`` feeds routes straight to
    ``_violation``. This drives the whole audit path the live app goes through
    — mount a router on a ``FastAPI`` app, walk the lazily-included tree, take
    every ``/api`` (method, route) pair — so a walk that silently skipped the
    route would fail here too. A guarded sibling is the control: the audit must
    single out the unguarded write, not reject everything.
    """
    from fastapi import FastAPI

    synthetic = FastAPI()
    router = APIRouter(prefix="/api/trips/{slug}")

    async def unguarded(slug: str) -> None:
        return None

    async def guarded(guard: Annotated[Any, Depends(require_trip_writer)]) -> None:
        return None

    router.add_api_route("/leaky", unguarded, methods=["POST"])
    router.add_api_route("/fine", guarded, methods=["POST"])
    synthetic.include_router(router)

    pairs = [
        (method, route)
        for route in _walk(synthetic.routes)
        if route.path.startswith("/api")
        for method in sorted(route.methods or ())
    ]
    violations = {route.path: _violation(method, route) for method, route in pairs}

    assert set(violations) == {"/api/trips/{slug}/leaky", "/api/trips/{slug}/fine"}, (
        f"the walk did not find both synthetic routes: {sorted(violations)}"
    )
    assert violations["/api/trips/{slug}/leaky"] is not None, "an unguarded trip POST passed"
    assert violations["/api/trips/{slug}/fine"] is None, violations["/api/trips/{slug}/fine"]


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
