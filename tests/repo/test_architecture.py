"""Layer rules from docs/architecture.md, checked on every test run.

interfaces → services → etl → db
etl.extract and etl.transform are pure: no database, no services.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.boundaries

SRC = Path(__file__).resolve().parents[2] / "src" / "our_pocket"

# package → our_pocket packages (and third-party modules) it must not import
FORBIDDEN = {
    "etl/extract": ("our_pocket.db", "our_pocket.services", "our_pocket.interfaces", "sqlalchemy"),
    "etl/transform": (
        "our_pocket.db",
        "our_pocket.services",
        "our_pocket.interfaces",
        "sqlalchemy",
    ),
    "etl": ("our_pocket.services", "our_pocket.interfaces"),
    "services": ("our_pocket.interfaces",),
    "db": ("our_pocket.etl", "our_pocket.services", "our_pocket.interfaces"),
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("package, forbidden", FORBIDDEN.items())
def test_layer_rules(package, forbidden):
    violations = [
        f"{path.relative_to(SRC)} imports {name}"
        for path in (SRC / package).rglob("*.py")
        for name in _imports(path)
        if any(name == f or name.startswith(f + ".") for f in forbidden)
    ]
    assert violations == []
