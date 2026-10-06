"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum values — never bare strings
#
# SVC-C2-002 — DisclaimerQualityGateNode.
#
# Inner domain node 4 (final): enforces the report's two structural invariants
# before the summary may leave the inner graph.
#
#   1. All six required sections are present.
#   2. The mandatory legal disclaimer is prepended, unconditionally.
#
# Input:  state["summary_raw"] (SummarizeNode), state["extracted_sections"]
# Output: state["summary_final"], state["review_metadata"]

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# The disclaimer every report carries. The output gate in the post_process slot
# checks for it again, so the two copies are deliberately identical.
MANDATORY_DISCLAIMER = (
    "This summary is for internal review purposes only. "
    "Consult qualified legal counsel before relying on any legal analysis."
)

# Canonical heading -> detection pattern. The patterns tolerate capitalization
# and spacing differences; the renderer emits the canonical form.
_SECTION_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("## Parties", re.compile(r"^##\s+Parties\s*$", re.IGNORECASE | re.MULTILINE)),
    ("## Key Terms", re.compile(r"^##\s+Key\s+Terms\s*$", re.IGNORECASE | re.MULTILINE)),
    ("## Obligations", re.compile(r"^##\s+Obligations\s*$", re.IGNORECASE | re.MULTILINE)),
    ("## Risk Flags", re.compile(r"^##\s+Risk\s+Flags\s*$", re.IGNORECASE | re.MULTILINE)),
    (
        "## Renewal / Termination Dates",
        re.compile(r"^##\s+Renewal\s*/\s*Termination\s+Dates\s*$", re.IGNORECASE | re.MULTILINE),
    ),
    ("## Recommended Actions", re.compile(r"^##\s+Recommended\s+Actions\s*$", re.IGNORECASE | re.MULTILINE)),
]

REQUIRED_SECTIONS = [heading for heading, _ in _SECTION_PATTERNS]


def missing_sections(text: str) -> List[str]:
    """Return the required headings that are absent from `text`."""
    return [heading for heading, pattern in _SECTION_PATTERNS if not pattern.search(text)]


def _review_metadata(sections: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize the extraction as counts for the report's audit record."""
    sections = sections if isinstance(sections, dict) else {}
    counted = [
        len(sections.get(key) or [])
        for key in ("parties", "key_terms", "obligations", "risk_flags", "renewal_termination_dates")
    ]
    dates = sections.get("renewal_termination_dates") or []
    return {
        "clause_count": sum(counted),
        "risk_flag_count": len(sections.get("risk_flags") or []),
        "renewal_dates_found": len([d for d in dates if isinstance(d, dict) and d.get("date")]),
        "risk_flags_suppressed": int(sections.get("risk_flags_suppressed") or 0),
    }


class DisclaimerQualityGateNode(FunctionNode):
    """Enforce the report's structural invariants and attach the disclaimer.

    Inner domain node 4, registered as "disclaimer_quality_gate".

    Input state keys:
        summary_raw:        the rendered report (SummarizeNode)
        extracted_sections: used for the review metadata counts

    Output state keys (partial dict):
        summary_final:   the report with the disclaimer prepended
        review_metadata: counts describing the extraction
        status / error_log
    """

    # See ContractParseNode — the trust decision is taken at the caller-contract
    # gate; the caller's level travels unchanged into the inner graph.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        summary_raw = state.get("summary_raw") or ""
        if not isinstance(summary_raw, str) or not summary_raw.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DisclaimerQualityGateNode: no report reached this node (summary_raw is empty)"],
                "summary_final": None,
            }

        absent = missing_sections(summary_raw)
        if absent:
            logger.error("DisclaimerQualityGateNode: report incomplete — missing %s", absent)
            emit_trace_event(
                "quality_gate_blocked",
                {"missing_section_count": len(absent), "missing_sections": absent},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "DisclaimerQualityGateNode: the report is missing required section(s): "
                    f"{absent}. All of {REQUIRED_SECTIONS} are required."
                ],
                "summary_final": None,
            }

        # The disclaimer is unconditional. It is not configurable: a review
        # summary released without it would misrepresent what the output is.
        summary_final = f"{MANDATORY_DISCLAIMER}\n\n{summary_raw.strip()}"
        metadata = _review_metadata(state.get("extracted_sections"))

        logger.info(
            "DisclaimerQualityGateNode: report accepted — clause_count=%d risk_flag_count=%d "
            "renewal_dates_found=%d characters=%d",
            metadata["clause_count"],
            metadata["risk_flag_count"],
            metadata["renewal_dates_found"],
            len(summary_final),
        )
        emit_trace_event(
            "quality_gate_passed",
            {"summary_len": len(summary_final), **metadata},
            state,
        )

        return {
            "summary_final": summary_final,
            "review_metadata": metadata,
            "status": AgentStatus.SUCCESS.value,
        }
