"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic model. LangGraph checkpoints
# use msgpack serialization; Pydantic objects cause silent corruption. Extend
# AgentState with agent-specific fields only. Do NOT add credentials or secrets.
#
# SVC-C2-002 — Contract Review Summary Generator.
# Two-layer nested graph: an outer backbone (AgentBaseGraph) and an inner domain
# workflow (BaseGraph). The fields below cover both layers.
#
# Personal data note: party names, signatory identifiers and employee references
# found in the submitted contract are carried only inside the extracted
# structural fields (parsed_clauses, extracted_sections) and the rendered
# summary. The output gate in post_process blocks credential patterns before
# anything is surfaced to the caller.

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for SVC-C2-002.

    The shared fields (user_input, status, session_id, node_history, error_log,
    input_context, hitl_*, ...) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — written by PreProcessNode and by the main slot's
    # merge_output(). Also seeded into the INNER graph through the context
    # bridge (src/graph/context_bridge.py).
    # ------------------------------------------------------------------

    # The validated contract document. PreProcessNode writes it after the
    # caller contract has been checked; it is the text the pipeline reads.
    document_text: Optional[str]

    # Caller-supplied label echoed into the rendered report. Locked to an inert
    # identifier pattern so caller text cannot inject report structure.
    contract_reference: Optional[str]

    # Validated runtime parameters, JSON-encoded so state stays msgpack-flat.
    # Keys: max_clauses (int), risk_threshold (float).
    runtime_settings: Optional[str]

    # Framework-visible copy of the validated input, kept for compatibility with
    # readers of the pre_process contract.
    validated_input: Optional[str]

    # The summary surfaced to the caller. Written by merge_output() from the
    # inner graph's summary_final, then gated by PostProcessNode.
    result: Optional[str]

    # Set by the output gate when it refuses to release the summary. Truthy on
    # refusal so the framework's `formatted_output or result` resolution cannot
    # fall back to a value the gate meant to withhold.
    formatted_output: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # ContractParseNode output: normalized contract text.
    contract_text: Optional[str]

    # ContractParseNode output: clause blocks.
    # Each entry: {"clause_id": str, "clause_type": str, "raw_text": str}
    parsed_clauses: Optional[List[Dict[str, Any]]]

    # ClauseExtractionNode output. Keys: "parties", "key_terms", "obligations",
    # "risk_flags", "renewal_termination_dates"; each value is a list of dicts.
    extracted_sections: Optional[Dict[str, Any]]

    # SummarizeNode output: rendered summary before the disclaimer gate.
    summary_raw: Optional[str]

    # DisclaimerQualityGateNode output: the validated summary with the mandatory
    # legal disclaimer prepended. All six sections must be present.
    summary_final: Optional[str]

    # DisclaimerQualityGateNode output. Keys: clause_count, risk_flag_count,
    # renewal_dates_found, risk_flags_suppressed.
    review_metadata: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Tracing — framework-managed; do NOT write these from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    error_code: Optional[str]
