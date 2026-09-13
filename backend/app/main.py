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


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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

    A full match beats an earlier partial one in Starlette's router, so a
    catch-all that accepts every verb would swallow the 405 a wrong verb on a
    real endpoint should produce — turning `DELETE /api/trips/{slug}/stops` into
    a 404. Decision-log entry 6 is explicit that collapsing 405 into 404 is a
    contract lie, not a cosmetic one: the offline queue reads "this resource
    doesn't exist" where the truth is "this method never will be accepted". So
    the partial match the router would have used is reconstructed here.

    Only /api routes are considered, and this route is skipped by path: the SPA
    fallback is GET-only and partial-matches every non-GET request, which would
    otherwise report `Allow: GET` for a path that does not exist at all.
    """
    allowed: set[str] = set()
    for route in request.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if not methods or path == UNKNOWN_API_PATH or not path.startswith(API_PREFIX):
            continue
        match, _ = route.matches(request.scope)
        if match is Match.PARTIAL:
            allowed |= set(methods)
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

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(static_dir / "index.html")
