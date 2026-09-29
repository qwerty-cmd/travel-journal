# Single image: FastAPI backend + built SPA served as static files.
# Same image runs locally (docker-compose) and on Azure Container Apps (or any
# other container host) unchanged — see spec Section 4, "Portability principle".

# --- Stage 1: build the frontend (Vite/TanStack SPA) ---
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: python runtime ---
FROM python:3.12-slim AS runtime
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY backend/pyproject.toml backend/uv.lock* ./backend/
RUN cd backend && uv sync --frozen --no-dev

COPY backend/ ./backend/
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

ENV STATIC_FILES_DIR=/app/frontend/dist
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
# Liveness only: /api/health touches neither Postgres nor object storage, so a
# failing check means the process is down, not that a dependency is. Python
# rather than curl because python:3.12-slim ships no curl; urlopen raises (and
# the process exits non-zero) on connection errors and non-2xx statuses.
# docker-compose.yml overrides the timing for local dev. See t-api-healthcheck-wiring.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/app/backend/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"]
# --log-config: uvicorn's default logging plus a filter that redacts the trip
# slug (the link credential) from request-path log lines, since Container Apps
# ships stdout/stderr to Log Analytics. Path is relative to WORKDIR /app; the
# filter module it names is importable because --app-dir puts backend/ on
# sys.path before uvicorn applies the config. See t-access-log-slug-exposure.
CMD ["backend/.venv/bin/uvicorn", "app.main:app", "--app-dir", "backend", "--log-config", "backend/log_config/uvicorn.json", "--host", "0.0.0.0", "--port", "8000"]
