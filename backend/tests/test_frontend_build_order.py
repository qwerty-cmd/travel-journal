"""
Guard on the *order* of the frontend build script.

``frontend/src/routeTree.gen.ts`` is emitted by the TanStack router vite plugin
during ``vite build``, and it is gitignored. So in a clean checkout — which is
exactly what the Dockerfile's ``frontend-build`` stage is — that file does not
exist until vite has run. A build script that type-checks first therefore fails
on every fresh clone and every image build, while passing on any developer
machine that happens to have a stale generated file lying around.

That regression already shipped once (``tsc -b && vite build``). The real
acceptance test is ``docker build``, but that is far too expensive to put in
this suite and would drag Docker and the npm toolchain into a backend pytest
run. This is the cheap stand-in: a pure string check on the committed script,
so a future edit cannot silently put tsc back in front of vite.

No runtime behaviour is covered here — there is none to cover.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"


def _build_script() -> str:
    scripts = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]
    return scripts["build"]


def test_vite_build_runs_before_typecheck() -> None:
    """vite must generate the route tree before tsc looks at the tree."""
    script = _build_script()
    assert "vite build" in script, script
    assert "tsc" in script, script
    assert script.index("vite build") < script.index("tsc"), (
        f"build script type-checks before vite generates routeTree.gen.ts: {script!r}"
    )


def test_typecheck_does_not_emit() -> None:
    """
    tsc's only remaining job is type-checking — vite already produced the
    output. ``-b`` would make it a second, build-info-managing emitter.
    """
    script = _build_script()
    assert "--noEmit" in script, script
    assert "tsc -b" not in script, script
