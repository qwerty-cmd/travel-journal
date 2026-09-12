# API client

TanStack Query hooks, one module per entity (trips, stops, photos, bikes, map),
generated against the API contract from spec Section 5 once Session 1 locks it
down. Not implementation-following — hooks are written from the contract, same
as the backend's Pydantic models, so frontend and backend can be built in
parallel against the same source of truth.
