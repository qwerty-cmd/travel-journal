"""
CSRF middleware: the Fetch Metadata / `Origin` check on unsafe `/api` requests.

**Context.** A session is an HttpOnly `__Host-btj_session` cookie (decision-log
Entry 29), so the browser attaches it to any request aimed at us, including one
a third-party page fires. `SameSite=Lax` stops cookies on cross-site
sub-resource and form POSTs in current browsers. This middleware is the first
layer, in front of that: it rejects the unsafe request itself, whatever the
cookie does. It uses no custom header or token, so app versions from before the
upgrade (and their queued writes) still pass. Contract: `docs/api-contract.md`,
"CSRF".

**How it works.** Pure ASGI, like `SecurityHeadersMiddleware`, so a streamed
request or response is never buffered. For every method other than GET, HEAD
and OPTIONS on a path starting with `/api`:

1. If `Sec-Fetch-Site` is present, it must be `same-origin` or `none`. Anything
   else (`cross-site`, `same-site`, or a value we don't recognise) fails.
2. If it is absent (older browsers, non-browser clients), exactly one `Origin`
   must be present, and its host and port must equal the `Host` header. A
   missing `Origin`, `Origin: null` or a mismatch fails.
3. A failure is answered here, before routing, with `403 FORBIDDEN` in the
   standard error envelope (built by `core/errors.py`). The request never
   reaches a handler, so no write happens.

Safe methods are never checked. Unsafe requests outside `/api` aren't checked
either, per the contract. Today every such path hits either the GET-only SPA
fallback or the GET/HEAD-only `/assets` mount, so they can only get a 404/405
and never change state.

`main.py` wires this *inside* `SecurityHeadersMiddleware`, so the 403 carries
the same four security headers as every other response. Starlette's
`add_middleware` puts the most recent addition outermost, so this one is added
first.

**Related APIs.** Every unsafe `/api` route can return this 403. Like 405, it is
middleware-level and is not listed per endpoint. `core/errors.py`
(`ApiError.forbidden`, `envelope_response`); `core/headers.py`
(`SECURITY_HEADERS`, applied by the wrapping middleware). The session cookie
this defends will be issued by `core/sessions.py`, which isn't built yet.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import ApiError, envelope_response

API_PREFIX = "/api"

# RFC 9110 §9.2.1 safe methods. Everything else is checked, including a verb
# the contract doesn't name (TRACE, or anything non-standard): failing closed
# on an unexpected verb can't let a write through.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})

CSRF_REJECTED_MESSAGE = "This request came from another site and was blocked."


def _origin_matches_host(headers: Headers) -> bool:
    """
    True only if there is exactly one `Origin`, it isn't `null`, and its host[:port] equals `Host`.

    **Why the scheme is ignored.** TLS terminates at the ingress (Azure
    Container Apps), so the app sees plain HTTP while the browser sends
    `Origin: https://...`. There is no scheme to compare against that we could
    trust. Host and port are compared exactly as the browser serialises them.
    RFC 6454 §6.2 and RFC 9110 §7.2 both leave out a default port, so a
    same-origin pair spells host[:port] identically. The check doesn't try to
    reconcile `host` with `host:443`, because that would mean guessing the
    scheme it just ignored.

    Matching is an exact comparison against the two serialised forms an origin
    can take, not a URL parse. An `Origin` with a path, userinfo, a different
    port or any other shape simply fails to match. Hostnames are
    case-insensitive (RFC 4343), so both sides are lower-cased first. A
    duplicated `Origin` or `Host` header is ambiguous and fails.
    """
    origins = headers.getlist("origin")
    hosts = headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1:
        return False
    origin = origins[0].strip().lower()
    host = hosts[0].strip().lower()
    if not host or origin == "null":
        return False
    return origin in (f"https://{host}", f"http://{host}")


def is_cross_site(scope: Scope) -> bool:
    """The contract's decision for one unsafe `/api` request: True means reject."""
    headers = Headers(scope=scope)
    fetch_sites = headers.getlist("sec-fetch-site")
    if fetch_sites:
        # Every copy must be acceptable. A repeated header can't smuggle a
        # `cross-site` past us by also saying `same-origin`.
        return not all(value.strip() in ALLOWED_FETCH_SITES for value in fetch_sites)
    return not _origin_matches_host(headers)


class CSRFMiddleware:
    """Rejects cross-site unsafe `/api` requests with a 403 FORBIDDEN envelope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] in SAFE_METHODS
            or not scope["path"].startswith(API_PREFIX)
            or not is_cross_site(scope)
        ):
            await self.app(scope, receive, send)
            return

        error = ApiError.forbidden(CSRF_REJECTED_MESSAGE)
        response = envelope_response(error.status_code, error.code, error.message)
        await response(scope, receive, send)
