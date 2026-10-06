# PB-04 — import isolation.
#
# A template builds on the framework's public modules (framework.*, shared.*)
# and never reaches past them into the platform's own internals. Those internals
# carry no compatibility promise, so an import of them turns a platform upgrade
# into a broken agent.

import ast
import os
import pytest


PROHIBITED_IMPORTS = [
    "agenticstar",
    "platform",  # the platform's internal package — not Python's stdlib module
]

# Python's own stdlib `platform` module is a different thing and is allowed.
STDLIB_PLATFORM_ALLOWED = True


def _scan_imports(filepath: str) -> list[str]:
    """Scan a Python file for prohibited imports using AST."""
    with open(filepath, "r") as f:
        tree = ast.parse(f.read(), filename=filepath)

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for prohibited in PROHIBITED_IMPORTS:
                    if alias.name == prohibited or alias.name.startswith(f"{prohibited}."):
                        if prohibited == "platform" and STDLIB_PLATFORM_ALLOWED:
                            # Skip Python stdlib platform
                            if alias.name == "platform":
                                continue
                        violations.append(f"{filepath}:{node.lineno} — import {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for prohibited in PROHIBITED_IMPORTS:
                    if node.module == prohibited or node.module.startswith(f"{prohibited}."):
                        if prohibited == "platform" and STDLIB_PLATFORM_ALLOWED:
                            if node.module == "platform":
                                continue
                        violations.append(f"{filepath}:{node.lineno} — from {node.module} import ...")

    return violations


def _find_python_files(directory: str) -> list[str]:
    """Find all .py files under the given directory."""
    py_files = []
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))
    return py_files


class TestImportIsolation:
    """PB-04: this template must not import the platform's internal package."""

    def test_no_prohibited_imports_in_src(self):
        """All source files under src/ must not import agenticstar or platform."""
        src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")
        if not os.path.exists(src_dir):
            pytest.skip("src/ directory not found")

        violations = []
        for filepath in _find_python_files(src_dir):
            violations.extend(_scan_imports(filepath))

        assert violations == [], "Import Isolation violations found:\n" + "\n".join(violations)
