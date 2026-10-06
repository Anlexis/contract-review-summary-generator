"""AgentCore Platform v1.0"""

# SVC-C2-002 — DomainWorkflowGraph (inner BaseGraph).
#
# The inner graph of the two-layer nested architecture. It encapsulates the
# contract review workflow:
#
#   START -> contract_parse -> clause_extraction -> summarize
#         -> disclaimer_quality_gate -> END
#
# Instantiated by ContractReviewGraphNode.get_subgraph() (src/graph/graph.py).
# get_output() shapes the sub_result dict that merge_output() consumes there.

import json
from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.clause_extraction_node import ClauseExtractionNode
from src.nodes.contract_parse_node import ContractParseNode
from src.nodes.disclaimer_quality_gate_node import DisclaimerQualityGateNode
from src.nodes.summarize_node import SummarizeNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for SVC-C2-002.

    Inherits BaseGraph directly for a fully custom node topology.

    Pipeline (linear):
        START
          -> contract_parse           (ContractParseNode)
          -> clause_extraction        (ClauseExtractionNode)
          -> summarize                (SummarizeNode)
          -> disclaimer_quality_gate  (DisclaimerQualityGateNode)
          -> END

    Every node is a FunctionNode subclass returning a partial-dict state update.
    initialize and finalize are outer backbone concerns and are not registered
    here.
    """

    # -- Identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "svc_c2_002_contract_review_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict shared by the inner and the outer graph."""
        return State

    # -- Config --------------------------------------------------------------

    def _validate_config(self) -> None:
        """No inner-graph config contract beyond the forwarded settings.

        The settings this graph republishes into inner state were already
        validated by declared_settings() in the outer graph, so there is nothing
        left to reject here. Every value has a built-in floor, which is why an
        empty forwarded config is a valid configuration rather than an error.
        """
        return None

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the inner initial state from the forwarded settings and bridge.

        ContractReviewGraphNode._parent_config() forwards the validated runtime
        settings under config["configurable"]["settings"]; this hook makes that
        block reachable by the domain nodes as the JSON-string state field
        `runtime_settings`.

        The validated document crosses the graph boundary on the context bridge
        (src/graph/context_bridge.py) because the framework forwards no outer
        state field into a subgraph. Passing values through state rather than a
        second execute() argument is what keeps every node at the canonical
        execute(self, state) -> dict signature.
        """
        configurable = (self.config or {}).get("configurable") or {}
        settings = configurable.get("settings") or {}
        bridged = get_caller_input_context()
        return {
            "runtime_settings": bridged.get("runtime_settings") or json.dumps(settings),
            "document_text": bridged.get("document_text") or "",
            "contract_reference": bridged.get("contract_reference") or "",
        }

    # -- Node registration ---------------------------------------------------

    def register_nodes(self) -> None:
        """Register the four domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract. initialize and
        finalize belong to the outer backbone. Every key registered here is
        referenced in add_edges().
        """
        self._nodes["contract_parse"] = ContractParseNode()
        self._nodes["clause_extraction"] = ClauseExtractionNode()
        self._nodes["summarize"] = SummarizeNode()
        self._nodes["disclaimer_quality_gate"] = DisclaimerQualityGateNode()

    # -- Edge wiring ---------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the linear contract review topology.

        The topology is intentionally linear — there is no conditional branching
        between the domain nodes, so add_conditional_edges() is not used.
        """
        self._sg.add_edge(START, "contract_parse")
        self._sg.add_edge("contract_parse", "clause_extraction")
        self._sg.add_edge("clause_extraction", "summarize")
        self._sg.add_edge("summarize", "disclaimer_quality_gate")
        self._sg.add_edge("disclaimer_quality_gate", END)

    # -- Routing -------------------------------------------------------------

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph contract; unused by this linear topology.

        add_conditional_edges() is not called, so this method never runs at
        request time. It returns END on error so that an unexpected call cannot
        re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "disclaimer_quality_gate"

    # -- Output shape --------------------------------------------------------

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the dict returned to the outer graph as `sub_result`.

        Received by ContractReviewGraphNode.merge_output(); the two are designed
        together so the field names cannot drift:

            emitted here          -> summary_final, review_metadata, status
            read by merge_output  -> sub_result.get("summary_final"), .get("status"),
                                     .get("review_metadata")

        The summary is emitted only on a successful run. On any other status the
        field is None, so a partially-built or refused summary cannot reach the
        outer state through this boundary.
        """
        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "summary_final": state.get("summary_final") if succeeded else None,
            "review_metadata": state.get("review_metadata", {}) if succeeded else {},
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
