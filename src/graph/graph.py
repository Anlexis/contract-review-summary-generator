"""AgentCore Platform v1.0"""

# SVC-C2-002 — outer graph (AgentBaseGraph; two-layer nested architecture).
#
# Outer backbone (fixed — add_edges() is NOT overridden):
#     START -> initialize -> pre_process -> main -> post_process -> finalize -> END
#                                            |
#                                            +-- RETRY --> pre_process (max_retry)
#
# The `main` slot is a GraphNode subclass (ContractReviewGraphNode) that
# delegates the whole domain workflow to DomainWorkflowGraph (inner BaseGraph),
# so domain complexity never reaches the backbone.
#
# Runtime configuration: the registry loads config/config.yaml and passes it as
# Graph(config=...); the standalone entry point does the same through
# _runtime_config(). Every declared value is validated once in
# declared_settings() and then travels to its consumer — the backbone reads
# max_retry itself, the pre_process node reads the caller-contract bounds from
# the initial state, and the inner pipeline reads them from the forwarded
# parent config. There is no second copy of the defaults elsewhere on disk.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#   src/graph/context_bridge.py        <- outer -> inner hand-off

import json
import logging
import math
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

logger = logging.getLogger(__name__)

# src/graph/graph.py -> parents[2] is the repository root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# The pipeline's floor, used only for keys config/config.yaml does not declare
# or declares out of contract. A value present in the file always wins, which is
# what makes the declaration observable end to end.
_BUILTIN_SETTINGS: Dict[str, Any] = {
    "max_clauses": 200,
    "risk_threshold": 0.5,
}

# Bounds every declared setting is checked against.
_MAX_CLAUSES_MIN, _MAX_CLAUSES_MAX = 1, 2000
_RISK_THRESHOLD_MIN, _RISK_THRESHOLD_MAX = 0.0, 1.0


def _runtime_config() -> Dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the file the registry loads and passes as Graph(config=...); the
    standalone entry point reads it here so both deployments run on the same
    declaration. Returns an empty mapping — never raises — when the file is
    absent, unreadable, not valid YAML or not a mapping; the graph then runs on
    its built-in floor. PyYAML is imported lazily because it is a framework
    runtime dependency rather than a module-load coupling of this template.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast(Dict[str, Any], loaded)


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate one declared numeric setting: a real number, finite, within [lo, hi].

    Bools, strings, other non-numerics, NaN, Infinity and out-of-range values all
    return None and the consumer keeps its built-in floor. Rejecting non-finite
    values matters even for a declared setting: NaN compares False against every
    bound, so a NaN threshold would silently disable the filtering it configures
    rather than fail.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def declared_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and flatten the declared runtime settings for the pipeline.

    `config` is what the graph was constructed with — the contents of
    config/config.yaml. Each value is checked for type, finiteness and range; an
    absent or out-of-contract key falls back to the built-in floor rather than
    propagating a value no consumer could use.
    """
    settings: Dict[str, Any] = dict(_BUILTIN_SETTINGS)

    parser = config.get("parser")
    parser = parser if isinstance(parser, dict) else {}
    max_clauses = _config_number(parser.get("max_clauses"), _MAX_CLAUSES_MIN, _MAX_CLAUSES_MAX)
    if max_clauses is not None and max_clauses == int(max_clauses):
        settings["max_clauses"] = int(max_clauses)

    review = config.get("review")
    review = review if isinstance(review, dict) else {}
    threshold = _config_number(review.get("risk_threshold"), _RISK_THRESHOLD_MIN, _RISK_THRESHOLD_MAX)
    if threshold is not None:
        settings["risk_threshold"] = threshold

    return settings


class ContractReviewGraphNode(GraphNode):
    """GraphNode wrapper for the inner DomainWorkflowGraph.

    Assigned to the `main` backbone slot. Delegates the multi-step domain
    workflow (parse -> extract -> summarize -> disclaimer gate) to the inner
    graph, forwards the validated runtime settings to it, and bridges the
    validated document across the graph boundary.
    """

    # "handle": an inner failure is turned into a contained refusal by
    # on_subgraph_error() below, rather than re-raised. Re-raising is what the
    # framework default does, and it loses containment: the node wrapper turns
    # the exception into a bare error update that clears nothing and carries a
    # traceback, and the caller receives an empty envelope with no reason in it.
    error_strategy: ClassVar[str] = "handle"

    # Human-in-the-loop interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        """Bind the validated runtime settings forwarded by the outer graph."""
        super().__init__()
        self._settings: Dict[str, Any] = dict(settings or _BUILTIN_SETTINGS)

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the validated runtime settings to the inner graph.

        The values come from the outer graph's own config — config/config.yaml
        as loaded by the registry, or by the standalone entry point through
        _runtime_config() — validated once in declared_settings(). The inner
        graph republishes them into inner state so the domain nodes read a live
        declaration rather than a dead one.
        """
        return {"configurable": {"settings": dict(self._settings)}}

    def get_subgraph(self) -> Any:
        """Instantiate the inner domain workflow graph.

        The inner graph receives the validated settings through the BaseGraph
        constructor; its domain NODES still take no constructor arguments and
        read their parameters from state.

        Imported inside the method to keep module load order independent of the
        inner graph, matching the nested-graph sample under src/examples/.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Hand the inner graph the validated document, and bridge the request.

        GraphNode.extract_input() returns a STRING that the framework writes into
        the inner state under `user_input`. Nothing else from the outer state
        crosses the boundary, so this is the last hook that can carry the
        validated document and the runtime settings to the inner side; both are
        stashed on the context bridge here and picked up by
        DomainWorkflowGraph._extra_initial_state().
        """
        document = cast(str, state.get("document_text") or state.get("validated_input") or "")
        set_caller_input_context(
            {
                "document_text": document,
                "contract_reference": state.get("contract_reference") or "",
                "runtime_settings": state.get("runtime_settings") or json.dumps(self._settings),
            }
        )
        return document or cast(str, state.get("user_input") or "")

    def on_subgraph_error(self, state: AgentState, error: Exception) -> Dict[str, Any]:
        """Turn an inner-graph failure into a contained, actionable refusal.

        Reached whenever the inner graph ends in an error status or raises —
        which includes the platform refusing one of the inner nodes' own results.

        Two things matter here. The report fields are CLEARED rather than left
        as they were: the platform resolves output as `formatted_output or
        result`, with no status check, so a summary left in `result` would be
        delivered inside the error envelope. And the reason is SUMMARISED: the
        inner error text can carry a traceback and absolute source paths, so it
        is replaced by a fixed sentence rather than forwarded.
        """
        logger.warning("ContractReviewGraphNode: inner workflow failed (%s)", type(error).__name__)
        return {
            "summary_final": None,
            "result": None,
            "review_metadata": {},
            "formatted_output": (
                "The contract review summary could not be produced for this request "
                "and no partial summary is included. Check the submitted document "
                "and try again."
            ),
            "status": AgentStatus.ERROR.value,
            "error_log": ["ContractReviewGraphNode: the contract review workflow did not complete"],
        }

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner graph's sub_result back into the outer state delta.

        `sub_result` is the dict shaped by DomainWorkflowGraph.get_output().
        Returns ONLY the keys this node changes — never the full state.

        The inner summary is published as `result` because the output gate in the
        post_process slot reads that field. On any non-success inner status the
        summary is NOT published: the un-gated text must not reach a state field
        the framework's output resolution can fall back to.
        """
        # Outer reason wins: a reason settled before the inner run is the real
        # one, and a plain sub_result.get() would erase it.
        marker = state.get("error_code") or sub_result.get("error_code", "")
        inner_status = sub_result.get("status")
        if inner_status != AgentStatus.SUCCESS.value:
            return {
                "error_code": marker,
                "summary_final": None,
                "result": None,
                "status": AgentStatus.ERROR.value,
                "review_metadata": {},
            }
        return {
            "error_code": marker,
            "summary_final": sub_result.get("summary_final"),
            "result": sub_result.get("summary_final"),
            "status": inner_status,
            "review_metadata": sub_result.get("review_metadata", {}),
        }


class SvcC2002Agent(AgentBaseGraph):
    """Outer graph — Contract Review Summary Generator.

    Base class: AgentBaseGraph (direct framework inheritance). Domain complexity
    is encapsulated in the inner DomainWorkflowGraph, reached through
    ContractReviewGraphNode in the `main` slot.

    Backbone slots:
        pre_process   PreProcessNode   — the caller-contract gate and the single
                                         trust boundary of this template
        main          ContractReviewGraphNode
        post_process  PostProcessNode  — the output gate

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "svc_c2_002"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill the backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default initialize and finalize nodes.
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ContractReviewGraphNode(settings=declared_settings(self.config))
        self._nodes["post_process"] = PostProcessNode()

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated runtime settings into the outer initial state.

        The node contract takes no config argument and every node is constructed
        without one, so the bounds PreProcessNode enforces travel through state.
        """
        return {"runtime_settings": json.dumps(declared_settings(self.config))}


# Module-level alias: the standalone entry point and the registry module path
# both resolve the agent class through this name.
Graph = SvcC2002Agent
