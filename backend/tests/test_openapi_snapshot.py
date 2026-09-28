"""
`frontend/openapi.json` is the committed input Kubb generates the frontend client
from. If it drifts from what the backend actually serves, the generated types lie.
"""

import json
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"

REGENERATE = (
    "frontend/openapi.json is stale. Regenerate it and the client:\n"
    '  cd backend && uv run python -c "import json,pathlib; from app.main import app; '
    "pathlib.Path('../frontend/openapi.json').write_text(json.dumps(app.openapi(), indent=2)+'\\n', "
    "encoding='utf-8', newline='\\n')\"\n"
    "  cd ../frontend && npx kubb generate"
)


def test_openapi_snapshot_matches_app() -> None:
    from app.main import app

    assert app.openapi() == json.loads(SNAPSHOT.read_text(encoding="utf-8")), REGENERATE
