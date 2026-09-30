"""
Every ``/api`` route declares exactly one rate limiter, the one the contract names.

The failure this catches is silent, like the guard audit's
(``test_route_dependency_audit.py``): a new route that forgets its limiter still
answers correctly, and nothing in its own tests notices it can be flooded. A
route that declares the wrong one (``public-read`` on a signin, say) is as quiet.

The rules, from ``docs/api-contract.md`` "Rate limits and lockout":

- ``/api/health``, the ``/api`` catch-all and ``signout``: no limiter;
- signup: ``limit_signup`` (``signup-ip`` and ``signup-global``, one dependency);
- signin, recover, password change, recovery-code rotation: ``limit_signin``;
- ``POST /api/v2/trips``: ``limit_trip_create`` (``trip-create``);
- every other ``GET``/``HEAD``: ``limit_public_read``;
- every other unsafe method: ``limit_writes``.

The limiter must also be the route's **first** dependency, so it answers before
the access gate looks anything up (``app/core/ratelimit.py``, "Order").
"""

from __future__ import annotations

from typing import Any

from app.core.ratelimit import (
    RateLimit,
    limit_public_read,
    limit_signin,
    limit_signup,
    limit_trip_create,
    limit_writes,
)
from app.main import app

UNLIMITED = {"health", "unknown_api_path", "signout"}
BY_NAME = {
    "signup": limit_signup,
    "signin": limit_signin,
    "recover": limit_signin,
    "change_password": limit_signin,
    "rotate_recovery_code": limit_signin,
    "create_trip": limit_trip_create,
}
READ_METHODS = {"GET", "HEAD"}


def _walk(routes: Any) -> list[Any]:
    """Every route with a dependency tree, descending into included routers."""
    found: list[Any] = []
    for route in routes:
        if hasattr(route, "effective_candidates"):
            found.extend(_walk(route.effective_candidates()))
        elif hasattr(route, "dependant"):
            found.append(route)
    return found


AUDITED = [
    (method, route)
    for route in _walk(app.routes)
    if route.path.startswith("/api")
    for method in sorted(route.methods or ())
]


def _limiters(route: Any) -> list[Any]:
    """Every ``RateLimit`` reachable from the route's dependency tree, at any depth."""
    found = []
    stack = list(route.dependant.dependencies)
    while stack:
        dependant = stack.pop()
        if isinstance(dependant.call, RateLimit):
            found.append(dependant.call)
        stack.extend(dependant.dependencies)
    return found


def _expected(method: str, route: Any) -> RateLimit | None:
    if route.name in UNLIMITED:
        return None
    if route.name in BY_NAME:
        return BY_NAME[route.name]
    return limit_public_read if method in READ_METHODS else limit_writes


def test_audit_is_not_vacuous() -> None:
    names = {route.name for _, route in AUDITED}
    assert UNLIMITED <= names and set(BY_NAME) <= names, (
        "an allowlisted route name is no longer live, or the walk lost part of the tree"
    )
    expected = {_expected(method, route) for method, route in AUDITED}
    assert expected == {
        None,
        limit_public_read,
        limit_signin,
        limit_signup,
        limit_trip_create,
        limit_writes,
    }


def test_every_api_route_declares_the_contract_limiter_first() -> None:
    problems = []
    for method, route in AUDITED:
        label = f"{method} {route.path} ({route.name})"
        declared = _limiters(route)
        expected = _expected(method, route)
        if expected is None:
            if declared:
                problems.append(f"{label} must have no limiter; it declares {declared}")
            continue
        if declared != [expected]:
            problems.append(f"{label} must declare exactly {expected!r}; it declares {declared}")
            continue
        if route.dependant.dependencies[0].call is not expected:
            problems.append(f"{label} must declare {expected!r} as its first dependency")
    assert not problems, "\n".join(problems)


def test_every_limited_operation_declares_429_with_retry_after() -> None:
    paths = app.openapi()["paths"]
    missing = []
    for method, route in AUDITED:
        if _expected(method, route) is None or not route.include_in_schema:
            continue
        responses = paths[route.path][method.lower()]["responses"]
        if "Retry-After" not in responses.get("429", {}).get("headers", {}):
            missing.append(f"{method} {route.path}")
    assert not missing, f"no 429 with Retry-After declared on: {missing}"
