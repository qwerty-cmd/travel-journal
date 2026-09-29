"""
Startup-time logging configuration for uvicorn (task `t-access-log-slug-exposure`).

Deliberately *outside* `app/`: nothing in the application imports this package.
It is loaded only by uvicorn's `--log-config log_config/uvicorn.json`, which
references `log_config.redaction.SlugRedactionFilter` by dotted path. That is the
decided shape — see the comment block above `_endpoint()` in
`app/core/errors.py` (sink C) for why an app module reconfiguring uvicorn's
loggers at import time was rejected.
"""
