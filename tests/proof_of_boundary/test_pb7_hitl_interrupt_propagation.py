# PB-07 — human-in-the-loop interrupt propagation.
#
# This template does not pause for human review: the review report is produced
# in one pass from the submitted document, and there is no decision inside the
# workflow a person has to make before it can continue.
#
# "Not applicable" is a claim, though, and a test file that only skips proves
# nothing about whether the claim is still true. So instead of skipping, this
# module PINS the claim from both ends: no node calls interrupt(), and the
# runtime configuration does not enable the checkpointing that resuming would
# require. If either changes, these tests fail and say what to add — the two
# propagation cases, one for the interrupt reaching the engine and one for the
# guard that stops it when the caller does not allow a pause.

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"

_FOLLOW_UP = (
    "This template has become a human-in-the-loop agent. Add the two "
    "propagation cases to this file: the interrupt must reach the engine "
    "without the status being set to error, and it must be skipped when the "
    "caller does not allow a pause."
)


def _modules_calling_interrupt() -> list[str]:
    """Return the source files that call interrupt(), by relative path."""
    calling: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            if name.endswith("interrupt"):
                calling.append(str(path.relative_to(_REPO_ROOT)))
                break
    return calling


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml enables human-in-the-loop pauses."""
    if not _CONFIG_PATH.exists():
        pytest.fail(f"{_CONFIG_PATH.name} is missing; the runtime contract cannot be checked")
    import yaml

    loaded = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        pytest.fail(f"{_CONFIG_PATH.name} does not parse as a mapping")
    hitl = loaded.get("hitl") or {}
    return bool(isinstance(hitl, dict) and hitl.get("enabled", False))


class TestHumanReviewIsNotPartOfThisWorkflow:
    def test_no_node_pauses_for_human_review(self):
        calling = _modules_calling_interrupt()
        assert calling == [], f"{calling} now pause for human review. {_FOLLOW_UP}"

    def test_the_runtime_configuration_does_not_enable_pauses(self):
        assert not _hitl_enabled(), _FOLLOW_UP

    def test_the_two_claims_agree(self):
        """A pause needs both a caller and a checkpoint; neither may appear alone."""
        assert bool(_modules_calling_interrupt()) == _hitl_enabled(), (
            "a node that pauses without checkpointing enabled cannot be resumed, and "
            "checkpointing enabled with no node that pauses is a dead declaration"
        )
