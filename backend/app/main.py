from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match

from app.api.routes import api_router
from app.core.config import get_settings
from app.core.errors import ApiError, register_exception_handlers

settings = get_settings()

app = FastAPI(title="Bike Trip Journal API")
register_exception_handlers(app)
app.include_router(api_router)


@app.get(
    "/api/health",
    description="Intended as the liveness probe for the container host — nothing is wired "
    "to it yet (no `HEALTHCHECK`, no `docker compose` healthcheck on the `api` service). "
    "Answers `200` with a fixed `status: ok` body as soon as the ASGI app is accepting "
    "requests; it touches neither Postgres nor object storage, so it reports that the "
    "process is up, not that the trip data is reachable.",
)
async def health() -> dict[str, str]:
    return {"status": "ok"}


# HEAD is GET without a body (RFC 9110 §9.3.2): a server that registers GET is
# expected to answer HEAD on the same path, and stripping the body is the ASGI
# server's job, not ours. It is a **second registration of the same handler**
# rather than `methods=["GET", "HEAD"]` on the one above, because
# `fastapi.openapi.utils.get_openapi_path` loops `for method in route.methods`
# with no HEAD exclusion while `operation_id` is per-*route*: one route carrying
# both verbs emits a duplicate `head:` operation into the OpenAPI document — and
# a "Duplicate Operation ID" warning — which Kubb would generate a second,
# identical frontend hook from. `include_in_schema=False` is what keeps the
# document to the one operation the contract describes. (Measured on FastAPI
# 0.141.1.)
app.add_api_route("/api/health", health, methods=["HEAD"], include_in_schema=False)


# --------------------------------------------------------------------------
# Unknown /api paths — kept out of the SPA fallback's reach
# --------------------------------------------------------------------------
# The SPA fallback below is a catch-all: `/{full_path:path}` matches *every*
# path, including ones under /api that no route claims. Without this route in
# between, once `frontend/dist` exists a GET for an unknown /api path returns
# `200 text/html` (index.html) instead of a 404 envelope — and the offline
# queue, which reads `code` out of a JSON body to decide retry-vs-never-retry,
# gets HTML with a success status. A guard *inside* the SPA handler would not be
# enough: that route is GET-only, so a POST to an unknown /api path would still
# match its path and produce a method-mismatch 405.
#
# So /api gets its own catch-all, registered after `api_router` (real routes win
# on a full match) and before the SPA fallback (which therefore never sees /api).
#
# None of this is observable in a fresh checkout, which is why it went unnoticed
# until QA (decision-log entry 7b): the SPA fallback is only registered when
# `frontend/dist` exists, and without a built frontend this route just 404s paths
# that would have 404'd anyway. It reads as dead code and isn't. The `spa_client`
# fixture in `tests/test_error_envelope.py` creates that directory rather than
# waiting for the Week 3 frontend build to create it, so the tests that fail if
# this route is deleted do run today — a green local suite is not evidence the
# route is redundant.
API_PREFIX = "/api"
UNKNOWN_API_PATH = "/api/{rest:path}"

# Every method a client can realistically send, because the point of the route is
# that it matches regardless of verb. A GET-only guard would leave every non-GET
# request for an unknown /api path falling through to the SPA fallback, which is
# itself GET-only: it would partial-match on path and the router would report a
# method-mismatch 405 ("wrong verb on a real endpoint") for a path that does not
# exist under any verb.
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]


def _methods_allowed_elsewhere(request: Request) -> set[str]:
    """
    The methods some *real* /api route accepts for this exact path.

    A full match beats an earlier partial one in Starlette's router, so the
    catch-all above — which accepts every verb — swallows the 405 a wrong verb
    on a real endpoint should produce, turning `POST /api/trips/{slug}` into a
    404. **The route this function serves is itself the thing that breaks 405s.**
    That is worth stating plainly, because the obvious fix on reading the
    comment above is "then delete the catch-all", and deleting it does make
    these 405s correct. It is still the wrong trade: without it, once
    `frontend/dist` exists an unknown `GET /api/*` goes back to `200 text/html`
    (entry 7b) — a *success* status carrying an HTML document to a Kubb client
    that will parse it as `ErrorEnvelope`, which is worse than a wrong status
    code. The narrower construction — excluding /api from the SPA fallback
    instead of shadowing it — needs either a hand-assigned `Route.path_regex` or
    middleware that duplicates the routing table, and it would make /api
    correctness depend on whether `frontend/dist` exists: exactly the
    environment-conditional coupling entry 7b forbids. So the catch-all stays
    and the 405 it shadows is reconstructed here instead.

    Decision-log entry 6 is explicit that collapsing 405 into 404 is a contract
    lie, not a cosmetic one: the offline queue reads "this resource doesn't
    exist" where the truth is "this method never will be accepted".

    **Why `route.matches()` and not `route.methods`** (entry 7b is the whole
    story): this FastAPI version represents an included router as a single lazy
    `_IncludedRouter` entry with `path=None`, `methods=None` and no `.routes` —
    so `getattr(route, "methods", None)` sees nothing for *every* route
    registered through `api_router`, and the previous version of this function
    skipped all eight contract endpoints while looking correct against
    `/api/health` (the one route registered directly on `app`). `methods` is not
    a *wrong* attribute, it is a public-looking one that is not universally
    present, and its absence fails silently and toward 404. Walking
    `_IncludedRouter`'s internals instead would reproduce that failure mode one
    layer deeper and regress just as silently on the release that renames the
    wrapper. `matches()` is public and defined on every `BaseRoute`, but it
    reports only PARTIAL/FULL and never the verb set — and `Allow` is a MUST on
    a 405 (RFC 9110 §15.5.6) — so the set is derived by probing each candidate
    verb and keeping the ones that come back FULL.

    Each probe runs against a **copy** of the scope, never the live one, and the
    copy is doing exactly one job: keeping the probe's `method` overwrite off the
    live scope. That one job is not optional — uvicorn reads `scope["method"]`
    back at *response-send* time, both for the access-log line and to decide
    whether a HEAD response is allowed a body, so a request left holding the last
    verb probed would log a method it never used — and would lose its body
    entirely the day `_ALL_METHODS` is reordered to end on HEAD.

    What the copy does **not** protect is `scope["fastapi"]`, FastAPI's own
    bookkeeping dict. By the time this function runs that key is already present
    (Starlette's router iterates `route.matches(scope)` over the *live* scope, and
    `api_router`'s `_IncludedRouter` is registered ahead of the catch-all, so
    `_IncludedRouter.matches` has already done `scope.setdefault("fastapi", {})`
    on it), and `{**request.scope, ...}` is a shallow copy that shares that inner
    dict — QA measured 104 probe writes landing on the live one. They are safe
    anyway because `_IncludedRouter.matches` brackets each write in `try/finally`
    and restores the prior value via `_restore_fastapi_scope_key`, and this loop
    is synchronous: there is no `await` between any write and its restore, so no
    other task can observe the intermediate state and the dict is byte-identical
    afterwards. Those two halves do not stand or fall together, and keeping
    them apart is the point. The FastAPI-*private* `finally` is **covered**:
    `test_probing_leaves_the_live_request_scope_unchanged` seeds the scope with
    a real `fastapi` key and snapshots that inner dict separately, so an
    upgrade that stops restoring the key fails that test rather than quietly
    outdating this paragraph — confirmed by stubbing
    `_restore_fastapi_scope_key` to a no-op, which makes the failure name both
    private writes probing provokes (`included_router`,
    `effective_route_context`). This loop staying synchronous is **not
    covered, and that test structurally cannot cover it**: a post-condition
    assertion cannot see a state that is restored before the call returns. It
    is a code-review invariant instead — there is no `await` in this function
    today, and adding one is exactly what would make the intermediate state
    observable to another task. One half is a regression a test catches for
    you; the other is a rule you have to hold while editing this loop.

    Filtering is by path, tolerating `path=None` rather than treating it as
    missing — a route whose path we cannot read is probed, not skipped, because
    skipping is what the bug was. A *known* non-/api path is skipped: the SPA
    fallback is `/{full_path:path}` and would FULL-match a GET probe for any
    path at all, reporting `Allow: GET` for a path that exists under no verb.
    """
    allowed: set[str] = set()
    for route in request.app.routes:
        path = getattr(route, "path", None)
        if path is not None and (path == UNKNOWN_API_PATH or not path.startswith(API_PREFIX)):
            continue
        for method in _ALL_METHODS:
            match, _ = route.matches({**request.scope, "method": method})
            if match is Match.FULL:
                allowed.add(method)
    return allowed


@app.api_route(UNKNOWN_API_PATH, methods=_ALL_METHODS, include_in_schema=False)
async def unknown_api_path(request: Request) -> Response:
    """
    Any /api path no route claims -> a 404 (or 405) envelope, never index.html.

    `include_in_schema=False`: this is framework-level behaviour, not one of the
    eight contract endpoints. The contract deliberately keeps 404-for-no-such-
    path, 405 and 500 out of the per-endpoint status tables, so it must not
    appear in the OpenAPI spec Kubb generates the client from.
    """
    allowed = _methods_allowed_elsewhere(request)
    if allowed:
        # The path exists, this verb doesn't. `Allow` is a MUST on a 405
        # (RFC 9110 §15.5.6) and is propagated by `http_exception_handler`.
        raise HTTPException(
            status_code=HTTPStatus.METHOD_NOT_ALLOWED,
            detail="This path does not accept that HTTP method.",
            headers={"Allow": ", ".join(sorted(allowed))},
        )
    raise ApiError.not_found("No API endpoint matches this path.")


# Serve the built SPA (spec Section 4, "Frontend hosting" — bundled into the
# same container). Anything under /api is handled above; everything else
# falls through to index.html so TanStack Router's client-side routing works
# on a hard refresh of a deep link.
static_dir = Path(settings.static_files_dir)
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(static_dir / "index.html")
