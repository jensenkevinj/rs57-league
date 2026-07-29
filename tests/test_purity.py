"""The engines are pure, and this is what keeps them that way.

``keeper_rules`` and ``stats`` take everything as arguments. No network, no filesystem, no
Flask, no ESPN. The rule is easy to state and easy to break by accident — one convenience
import of ``json`` to load a fixture, one ``Path`` to find a data file, and the tested core
quietly becomes something that needs a working directory to run.

Checked by reading the imports rather than by trusting a convention, because a convention
holds right up until the session that has not read ``CLAUDE.md``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PURE_MODULES = ("keeper_rules", "stats")

FORBIDDEN = {
    # I/O and the environment
    "json", "os", "sys", "io", "pathlib", "shutil", "tempfile", "subprocess", "socket",
    "urllib", "urllib3", "http", "requests", "sqlite3", "csv", "pickle", "logging",
    # frameworks and the impure half of this package
    "flask", "jinja2", "argparse",
}

SOURCE = Path(__file__).resolve().parent.parent / "rs57"


def imported_modules(path: Path) -> set[str]:
    """Every top-level module name ``path`` imports, however it spells the import."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            # A relative import has no module of its own to name; it is in-package by
            # definition, and the rs57 check below covers where it can point.
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("module", PURE_MODULES)
def test_no_io_imports(module: str):
    offenders = imported_modules(SOURCE / f"{module}.py") & FORBIDDEN
    assert not offenders, (
        f"{module}.py imports {sorted(offenders)} — it is meant to be pure. Everything it "
        f"needs arrives as an argument; the I/O belongs in espn.py or a sync module."
    )


@pytest.mark.parametrize("module", PURE_MODULES)
def test_no_dependency_on_the_impure_half(module: str):
    """The dependency only ever points one way: sync -> stats -> keeper_rules -> models."""
    source = (SOURCE / f"{module}.py").read_text(encoding="utf-8")
    for impure in ("rs57.espn", "rs57.sync", "rs57.stats_sync", "rs57.validate"):
        assert f"from {impure}" not in source and f"import {impure}" not in source, (
            f"{module}.py imports {impure} — that inverts the dependency. "
            f"{impure} is allowed to know about {module}, never the other way round."
        )


def test_keeper_rules_does_not_import_stats():
    """``stats`` may borrow ``Severity`` from ``keeper_rules``; the reverse would be a cycle
    and would drag the prize rules into the salary engine."""
    source = (SOURCE / "keeper_rules.py").read_text(encoding="utf-8")
    assert "rs57.stats" not in source


@pytest.mark.parametrize("module", PURE_MODULES)
def test_importable_without_touching_the_filesystem(module: str):
    """No module-level path resolution, no file read at import time."""
    tree = ast.parse((SOURCE / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        assert not isinstance(node, (ast.With, ast.For, ast.While, ast.Try)), (
            f"{module}.py runs a statement at import time; a pure module should only define "
            f"constants, classes and functions"
        )
