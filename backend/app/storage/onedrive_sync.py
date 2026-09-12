# Background job: periodically pushes newly-uploaded photos from S3-compatible
# storage (source of truth) to OneDrive via Microsoft Graph (archive only —
# spec Section 4 "Storage"). Runs on a timer, never blocks the upload path.
# On failure: retry with backoff; if it keeps failing, the photo just stays
# pending-archive and tries again next run — never lost, never blocking.
#
# Implemented in Week 2 once the Photo repository and API contract exist.
# Refresh token comes from GRAPH_REFRESH_TOKEN (container secret / env var,
# never committed) — see DevOps agent scope in spec Section 11.
