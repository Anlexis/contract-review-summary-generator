# PB-06 — the node execution order, and the trust boundary.
#
# Every node runs behind a wrapper the template cannot override. The wrapper
# checks the caller's trust level, emits a start event, runs the input gate,
# then execute(), then the output gate, then a completion event. Two properties
# follow, and both are checked here:
#
#   * a caller below a node's declared trust level never reaches execute();
#   * every node in this template goes through the whole sequence, in order.
#
# The template's single trust boundary is the caller-contract gate in the
# pre_process slot. The workflow nodes deliberately declare the permissive level:
# the caller's level travels into the inner graph unchanged, so a stricter
# declaration there could never be satisfied — it would refuse every request
# rather than add a check. That is asserted below so the arrangement cannot be
# changed silently.

import importlib
import inspect
import pkgutil
from typing import ClassVar

import pytest

from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel
from src.nodes.pre_process_node import PreProcessNode


class _PrivilegedFixtureNode(BaseNode):
    """A privileged node used to prove the negative trust-gate path."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _lower_trust_level(required: TrustLevel) -> TrustLevel:
    """Return a level below `required`; fail loudly if the platform adds one."""
    below = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return below[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _discover_node_classes() -> list:
    """Import every module under src/nodes/ and collect the concrete node classes."""
    try:
        package = importlib.import_module("src.nodes")
    except ImportError as exc:  # pragma: no cover - a broken install, not a behaviour
        pytest.fail(f"src.nodes could not be imported: {exc}")

    discovered = []
    for _, module_name, _ in pkgutil.walk_packages(package.__path__, prefix="src.nodes."):
        module = importlib.import_module(module_name)
        for attribute in vars(module).values():
            if (
                isinstance(attribute, type)
                and issubclass(attribute, BaseNode)
                and attribute is not BaseNode
                and attribute.__module__ == module_name
                and not inspect.isabstract(attribute)
            ):
                discovered.append(attribute)
    return discovered


class TestTrustBoundary:
    def test_an_under_privileged_caller_never_reaches_execute(self, monkeypatch):
        import framework.nodes.base_node as base_node_module

        events: list = []
        execute_calls: list = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedFixtureNode.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedFixtureNode, "execute", spy_execute)

        result = _PrivilegedFixtureNode()(
            {
                "caller_trust_level": _lower_trust_level(_PrivilegedFixtureNode.required_trust_level).value,
                "correlation_id": "pb06-trust-denial",
            }
        )

        assert result["status"] == "error"
        assert "trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls

    def test_the_caller_contract_gate_is_the_template_trust_boundary(self):
        assert PreProcessNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_only_the_caller_contract_gate_raises_the_bar(self):
        """A stricter workflow node could never be satisfied — see the module note."""
        stricter = [
            node.__name__
            for node in _discover_node_classes()
            if node is not PreProcessNode and node.required_trust_level is not TrustLevel.ANONYMOUS
        ]
        assert stricter == [], (
            "these nodes declare a trust level the caller can never hold, so they would "
            f"refuse every request: {stricter}"
        )


class TestInvokeOrder:
    def test_every_node_runs_the_full_sequence(self, monkeypatch):
        node_classes = _discover_node_classes()
        assert node_classes, "src/nodes/ must contain at least one concrete node"

        import framework.nodes.base_node as base_node_module

        failures: list = []
        for node_class in node_classes:
            order: list = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "input_gate"),
                ("execute", "execute"),
                ("_security_gate_output", "output_gate"),
            ):
                original = getattr(node_class, method_name)

                def spy(self, argument, _o=order, _label=label, _original=original):
                    _o.append(_label)
                    return _original(self, argument)

                monkeypatch.setattr(node_class, method_name, spy)

            node_class()(
                {
                    "caller_trust_level": node_class.required_trust_level.value,
                    "correlation_id": "pb06-invoke-order",
                }
            )

            expected = [
                "event:node_start",
                "input_gate",
                "execute",
                "output_gate",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(f"{node_class.__name__}: expected {expected}, got {order}")

        assert not failures, "\n".join(failures)
