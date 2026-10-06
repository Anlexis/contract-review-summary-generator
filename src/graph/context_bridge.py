"""AgentCore Platform v1.0"""

# src/graph/context_bridge.py — carries the validated contract document and the
# validated runtime parameters across the outer -> inner graph boundary.
#
# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and forwards no other
# outer state field. An inner node that reads state["document_text"] would
# therefore see nothing on a real invocation, however well the outer graph
# populated it. The sanctioned subclass hooks bridge the gap:
#
#   ContractReviewGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       -> set_caller_input_context({...validated document + parameters...})
#   DomainWorkflowGraph._extra_initial_state()     [runs INSIDE subgraph.invoke]
#       -> seeds the inner initial state from get_caller_input_context()
#
# Only values PreProcessNode has already validated are stashed here — the bridge
# is a transport, never a second contract.
#
# A ContextVar keeps the hand-off correct per thread and per task, so concurrent
# invocations inside one process cannot observe each other's document.

from contextvars import ContextVar
from typing import Any, Dict, Optional

_CALLER_INPUT_CONTEXT: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "svc_c2_002_caller_input_context", default=None
)


def set_caller_input_context(payload: Optional[Dict[str, Any]]) -> None:
    """Stash the validated request for the imminent inner-graph invoke."""
    _CALLER_INPUT_CONTEXT.set(dict(payload) if payload else {})


def get_caller_input_context() -> Dict[str, Any]:
    """Read (without consuming) the stashed request; {} when none was set."""
    return _CALLER_INPUT_CONTEXT.get() or {}
