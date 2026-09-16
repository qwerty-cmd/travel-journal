---
name: add-endpoint
description: The repeatable recipe for turning one API-contract entry into a working, tested, documented endpoint. Use this whenever the dev/test-writer/qa/docs agents are working through one of the endpoints in spec Section 5.
---

# Add endpoint

One API-contract entry (spec Section 5) becomes one endpoint via this sequence. Every step is a small, checkable unit — don't skip ahead if an earlier one isn't actually done.

1. **Pydantic model** (`backend/app/models/`) — request and response shapes exactly as defined in the API contract. Every field gets a real `description=` (this feeds the OpenAPI spec, which feeds Kubb's generated frontend types — spec Section 5).
2. **Repository function** (`backend/app/data/repositories/`) — the Postgres query/mutation this endpoint needs. Never call SQLAlchemy from the route handler directly.
3. **Storage call, if the endpoint touches photos** (`backend/app/storage/`) — via the S3-compatible client module, never boto3 directly from the route.
4. **Route handler** (`backend/app/api/routes/`) — wires the above together. Enforces the access-control rule from spec Section 4 (403 on a viewer slug for any write) via the shared dependency in `backend/app/core/security.py`.
   - **A `GET` route also gets a `HEAD` sibling**: a second registration of the *same handler* — `add_api_route(..., methods=["HEAD"], include_in_schema=False)` — not `methods=["GET", "HEAD"]` on one route. FastAPI's `get_openapi_path` loops over `route.methods` with no HEAD exclusion while `operation_id` is per-route, so one route carrying both verbs emits a second `head:` operation sharing the GET's `operationId`, and step 8 then generates a duplicate hook from it. `include_in_schema=False` is the part doing the work: it keeps the document to the one operation the contract describes. Reusing the handler object is what keeps the access dependency in sync across the pair. See decision-log Entry 11.
5. **Test → QA → Docs** — `dev` chains the rest of the pipeline automatically: `test-writer` (contract-first for access control/data integrity, implementation-following otherwise) → `qa` (re-derives criteria from contract, verifies directly) → `docs` (Context/How it works/Related APIs, from what was built). Dev returns the combined result to the orchestrator.
8. **Frontend hook regeneration** — once the backend endpoint exists and its OpenAPI description is complete, regenerate the Kubb client (`frontend/src/api/`) so the frontend gets the new typed hook automatically — never hand-write a matching fetch call.

Stop and flag it if any step reveals the API contract itself needs to change — don't quietly improvise around a contract gap; that's a Session 1 problem, not something to patch over mid-task.
