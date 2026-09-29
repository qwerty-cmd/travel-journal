"""
No module under ``app/`` raises a bare ``HTTPException`` with a 409.

Task ``t-bare-409-envelope-bypass``. ``_STATUS_TO_CODE`` maps 409 to CONFLICT by
*status*, so ``HTTPException(409, detail=...)`` renders a well-formed CONFLICT
envelope with its ``detail`` as the message -- without ever passing through
``ApiError.conflict``, where the contract's "no value from the conflicting
record" rule is held (``docs/api-contract.md``, the ``CONFLICT`` leak boundary).
The envelope looks correct, so no response-shape assertion can catch it.

The contract puts that boundary at the raise site, not in the global handler, so
this guard is at the raise site too: a static walk of every ``HTTPException``
call in ``app/``. A status the walk cannot resolve to a literal (a variable, a
computed value) fails as well -- an unresolvable status is exactly how a 409
would slip past a literal-only check.

The mapping row itself stays (decision-log Entry 14); removing it would also make
``ApiError.conflict`` unconstructible.
"""

from __future__ import annotations

import ast
from http import HTTPStatus
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"

_HTTP_EXCEPTION_MODULES = {"fastapi", "fastapi.exceptions", "starlette.exceptions"}


class _Unresolved:
    """Sentinel: the status expression is not a statically known integer."""


UNRESOLVED = _Unresolved()


def _exception_names(tree: ast.Module) -> set[str]:
    """Local names bound to ``HTTPException`` by ``from X import HTTPException [as Y]``."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _HTTP_EXCEPTION_MODULES:
            for alias in node.names:
                if alias.name == "HTTPException":
                    names.add(alias.asname or alias.name)
    return names


def _is_http_exception_call(call: ast.Call, names: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in names
    # `fastapi.HTTPException(...)`, `exceptions.HTTPException(...)`
    return isinstance(func, ast.Attribute) and func.attr == "HTTPException"


def _resolve_status(node: ast.expr) -> int | _Unresolved:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute):
        # HTTPStatus.CONFLICT
        if node.attr in HTTPStatus.__members__:
            return HTTPStatus[node.attr].value
        # fastapi/starlette `status.HTTP_409_CONFLICT`
        if node.attr.startswith("HTTP_"):
            digits = node.attr.split("_")[1]
            if digits.isdigit():
                return int(digits)
    return UNRESOLVED


def _status_argument(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "status_code":
            return keyword.value
    return call.args[0] if call.args else None


def violations_in_source(source: str, filename: str = "<source>") -> list[str]:
    """Every HTTPException call in ``source`` whose status is 409 or unresolvable."""
    tree = ast.parse(source, filename=filename)
    names = _exception_names(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_http_exception_call(node, names)):
            continue
        argument = _status_argument(node)
        status = UNRESOLVED if argument is None else _resolve_status(argument)
        if status is UNRESOLVED:
            found.append(f"{filename}:{node.lineno}: HTTPException with an unresolvable status")
        elif status == HTTPStatus.CONFLICT:
            found.append(f"{filename}:{node.lineno}: bare HTTPException(409)")
    return found


def http_exception_calls_in_app() -> int:
    """How many HTTPException calls the walk sees -- the non-vacuity check."""
    count = 0
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _exception_names(tree)
        count += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_http_exception_call(node, names)
        )
    return count


def test_no_bare_409_is_raised_anywhere_in_app() -> None:
    """A 409 is raised only through ``ApiError.conflict``, never as an HTTPException."""
    found = []
    for path in sorted(APP_DIR.rglob("*.py")):
        found += violations_in_source(
            path.read_text(encoding="utf-8"), str(path.relative_to(APP_DIR.parent))
        )
    assert found == [], (
        "Raise conflicts through ApiError.conflict(...) -- a bare HTTPException(409) "
        "skips the contract's CONFLICT message leak boundary:\n" + "\n".join(found)
    )


def test_audit_is_not_vacuous() -> None:
    """The walk finds the one known raise (main.py's 405), so a pass means something."""
    assert http_exception_calls_in_app() >= 1


@pytest.mark.parametrize(
    "source",
    [
        "from fastapi import HTTPException\nraise HTTPException(409)",
        "from fastapi import HTTPException\nraise HTTPException(status_code=409, detail='x')",
        "from fastapi import HTTPException\nraise HTTPException(HTTPStatus.CONFLICT)",
        (
            "from fastapi import HTTPException, status\n"
            "raise HTTPException(status.HTTP_409_CONFLICT)"
        ),
        "from starlette.exceptions import HTTPException as SE\nraise SE(409)",
        "import fastapi\nraise fastapi.HTTPException(409)",
        "from fastapi import HTTPException\ncode = 409\nraise HTTPException(code)",
        "from fastapi import HTTPException\nraise HTTPException()",
    ],
)
def test_detector_flags_every_spelling_of_a_409(source: str) -> None:
    """Literal, keyword, enum, starlette constant, alias, module attribute, variable."""
    assert len(violations_in_source(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from fastapi import HTTPException\nraise HTTPException(405)",
        (
            "from fastapi import HTTPException\n"
            "raise HTTPException(status_code=HTTPStatus.METHOD_NOT_ALLOWED)"
        ),
        (
            "from fastapi import HTTPException, status\n"
            "raise HTTPException(status.HTTP_404_NOT_FOUND)"
        ),
        "raise ApiError.conflict('This id is already in use.')",
    ],
)
def test_detector_passes_non_409_raises(source: str) -> None:
    """A non-409 HTTPException, and a conflict raised the sanctioned way, are clean."""
    assert violations_in_source(source) == []
