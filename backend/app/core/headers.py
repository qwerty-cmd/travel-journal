"""
The baseline security headers, in one place both of their writers import.

`main.py`'s `SecurityHeadersMiddleware` sets them on every response that passes
through the user middleware stack. An unhandled exception's 500 does not:
Starlette's `ServerErrorMiddleware` sits *outside* user middleware, so the
catch-all handler in `core/errors.py` sets them on its own response. Kept here
rather than in `main.py` so `errors.py` can import it without a cycle.
"""

# Set on every response -- API, SPA fallback and /assets alike. Deliberately not
# a full CSP (the SPA's script/style/tile sources would need their own review).
#
# `Referrer-Policy` is explicit rather than left to the browser default because
# the value that matters is the one that keeps `/t/<slug>` -- the trip's access
# credential -- off third-party requests: `strict-origin-when-cross-origin`
# sends only the origin to the OSM tile servers. Not `no-referrer`: OSM's tile
# usage policy wants a Referer. Framing is refused both ways, `X-Frame-Options`
# for older browsers and `frame-ancestors` for current ones.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}
