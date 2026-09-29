"""
Redact the trip slug from uvicorn's request-path log lines.

A trip slug is the credential for a link (decision-log Entry 13), and it sits
in the URL by construction: `/api/trips/<slug>/...` for the API and `/t/<slug>/...`
for the SPA. uvicorn writes that URL on every request (`uvicorn.access`) and on
every WebSocket handshake attempt (`uvicorn.error`, `"WebSocket <path>"`). On
Azure Container Apps stdout/stderr go to Log Analytics — retained and
queryable — so the segment is replaced with `<redacted>` before any handler
formats the record.

Only the slug segment is removed. Method, status, latency, client address, the
rest of the path and the query string are kept: silencing access logs wholesale
was considered and rejected (see `t-access-log-slug-exposure` in
`docs/progress-notes.md`).

Wired exclusively through `log_config/uvicorn.json` (`--log-config`), never
imported by application code.
"""

from __future__ import annotations

import logging
import re

REDACTED = "<redacted>"

# The path must *start* with a slug-bearing prefix; repeated slashes are
# tolerated because the log records what the client sent, not what routing
# normalised, and `//t/<slug>` still carries the credential. The segment ends
# at the next `/`, `?` or `#` — so sub-paths and the query string survive.
# Case-insensitive: routing is case-sensitive, so `/T/<slug>` 404s, but the
# slug the client typed is still a live credential in the log line.
_SLUG_PATH = re.compile(r"^(/+(?:api/+trips|t)/+)[^/?#]+", re.IGNORECASE)


def redact_path(value: str) -> str:
    """`/api/trips/abc/stops?x=1` -> `/api/trips/<redacted>/stops?x=1`; others unchanged."""
    return _SLUG_PATH.sub(lambda m: m.group(1) + REDACTED, value, count=1)


class SlugRedactionFilter(logging.Filter):
    """
    Rewrite any string argument of a record that is a slug-bearing path.

    uvicorn's access record args are `(client_addr, method, full_path,
    http_version, status_code)`; its WebSocket handshake records are
    `(client_addr, full_path[, status])`. Rather than depend on either position,
    every `str` arg is passed through `redact_path`, which only touches values
    that begin with a slug-bearing path — a client address, a method or an HTTP
    version can never match. Arg count and types are preserved, because
    `uvicorn.logging.AccessFormatter` unpacks exactly five args.

    Always returns True: this filter edits records, it never drops them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(redact_path(arg) if isinstance(arg, str) else arg for arg in args)
        return True
