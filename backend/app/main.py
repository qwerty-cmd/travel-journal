from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import api_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title="Bike Trip Journal API")
app.include_router(api_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
