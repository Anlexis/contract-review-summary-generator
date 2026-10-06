"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum values — never bare strings
#
# SVC-C2-002 — SummarizeNode.
#
# Inner domain node 3: renders the review report from the extracted sections.
#
# Input:  state["extracted_sections"] (ClauseExtractionNode)
#         state["contract_reference"] — validated inert caller label
# Output: state["summary_raw"] — the report, with all six required sections
#
# Rendering is deterministic and structural: the report is assembled from the
# extracted data, and every excerpt copied out of the contract is written as
# inline body text. Nothing the document contains can introduce a section
# heading, because headings are emitted only by this renderer.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.service import recommended_action

logger = logging.getLogger(__name__)

# The six headings the report always carries, in order. The quality gate checks
# for exactly these.
REQUIRED_SUMMARY_SECTIONS = [
    "## Parties",
    "## Key Terms",
    "## Obligations",
    "## Risk Flags",
    "## Renewal / Termination Dates",
    "## Recommended Actions",
]

# Human-readable labels for the extracted key-term identifiers.
_TERM_LABELS = {
    "payment": "Payment",
    "duration": "Duration",
    "service_level": "Service Levels",
    "ip_ownership": "Intellectual Property",
    "exclusivity": "Exclusivity",
    "liability": "Liability",
    "confidentiality": "Confidentiality",
    "governing_law": "Governing Law",
}


def _inline(text: Any) -> str:
    """Render one extracted value as safe inline Markdown body text.

    Newlines are collapsed and a leading Markdown block marker is removed, so a
    line taken from the contract renders as prose rather than as report
    structure.
    """
    value = re.sub(r"\s+", " ", str(text)).strip()
    return re.sub(r"^[#>*\-\s]+", "", value)


def _render_parties(parties: List[Dict[str, Any]]) -> str:
    if not parties:
        return "- No party definitions were identified in the submitted document.\n"
    lines = []
    for party in parties:
        role = _inline(party.get("role") or "unspecified role")
        lines.append(f"- **{_inline(party.get('name'))}** — {role}\n")
    return "".join(lines)


def _render_key_terms(terms: List[Dict[str, Any]]) -> str:
    if not terms:
        return "- No commercial terms were identified in the submitted document.\n"
    lines = []
    for term in terms:
        label = _TERM_LABELS.get(str(term.get("term")), str(term.get("term")).replace("_", " ").title())
        lines.append(f"- **{label}** ({_inline(term.get('clause_ref'))}): {_inline(term.get('value'))}\n")
    return "".join(lines)


def _render_obligations(obligations: List[Dict[str, Any]]) -> str:
    if not obligations:
        return "- No obligations were identified in the submitted document.\n"
    lines = []
    for item in obligations:
        deadline = item.get("deadline")
        suffix = f" Deadline: {_inline(deadline)}." if deadline else ""
        lines.append(
            f"- **{_inline(item.get('party'))}** ({_inline(item.get('clause_ref'))}): "
            f"{_inline(item.get('obligation'))}{suffix}\n"
        )
    return "".join(lines)


def _render_risk_flags(flags: List[Dict[str, Any]], suppressed: int) -> str:
    if not flags:
        body = "- No risk indicators at or above the configured severity floor.\n"
    else:
        body = "".join(
            f"- **[{_inline(flag.get('severity'))}]** {_inline(flag.get('description'))} "
            f"({_inline(flag.get('clause_ref'))}): {_inline(flag.get('evidence'))}\n"
            for flag in flags
        )
    if suppressed:
        body += (
            f"\n{suppressed} further indicator(s) scored below the configured severity " "floor and are not listed.\n"
        )
    return body


def _render_dates(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "- No renewal or termination dates were identified in the submitted document.\n"
    lines = []
    for event in events:
        parts = []
        if event.get("date"):
            parts.append(f"Date: {_inline(event.get('date'))}")
        if event.get("notice_period"):
            parts.append(f"Notice period: {_inline(event.get('notice_period'))}")
        detail = ". ".join(parts) if parts else "No date or notice period stated"
        lines.append(
            f"- **{_inline(event.get('event_type')).title()}** ({_inline(event.get('clause_ref'))}): {detail}.\n"
        )
    return "".join(lines)


def _render_actions(sections: Dict[str, Any]) -> str:
    actions: List[str] = []
    for flag in sections.get("risk_flags") or []:
        action = recommended_action(str(flag.get("description")))
        if action and action not in actions:
            actions.append(action)
    if not sections.get("parties"):
        actions.append("Confirm the contracting parties and their defined roles.")
    if not sections.get("renewal_termination_dates"):
        actions.append("Confirm the renewal and termination dates with the contract owner.")
    if not actions:
        actions.append("No follow-up identified; file the summary with the contract record.")
    return "".join(f"{index}. {action}\n" for index, action in enumerate(actions, start=1))


def render_summary(sections: Dict[str, Any], reference: Optional[str] = None) -> str:
    """Assemble the six-section review report from the extracted sections."""
    header = f"Contract reference: `{reference}`\n\n" if reference else ""
    suppressed = int(sections.get("risk_flags_suppressed") or 0)
    return (
        f"{header}"
        f"## Parties\n\n{_render_parties(sections.get('parties') or [])}\n"
        f"## Key Terms\n\n{_render_key_terms(sections.get('key_terms') or [])}\n"
        f"## Obligations\n\n{_render_obligations(sections.get('obligations') or [])}\n"
        f"## Risk Flags\n\n{_render_risk_flags(sections.get('risk_flags') or [], suppressed)}\n"
        f"## Renewal / Termination Dates\n\n{_render_dates(sections.get('renewal_termination_dates') or [])}\n"
        f"## Recommended Actions\n\n{_render_actions(sections)}"
    )


class SummarizeNode(FunctionNode):
    """Render the contract review report from the extracted sections.

    Inner domain node 3, registered as "summarize".

    Input state keys:
        extracted_sections: mapping with the five section keys
        contract_reference: validated inert caller label (optional)

    Output state keys (partial dict):
        summary_raw: the rendered report
        status / error_log
    """

    # See ContractParseNode — the trust decision is taken at the caller-contract
    # gate; the caller's level travels unchanged into the inner graph.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        sections = state.get("extracted_sections")
        if not isinstance(sections, dict) or not sections:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "SummarizeNode: no extracted sections reached this node " "(extracted_sections is empty)"
                ],
            }

        reference = state.get("contract_reference") or ""
        summary = render_summary(sections, str(reference) if reference else None)

        logger.info("SummarizeNode: report rendered — %d characters", len(summary))
        emit_trace_event(
            "summary_rendered",
            {"summary_len": len(summary), "section_count": len(REQUIRED_SUMMARY_SECTIONS)},
            state,
        )

        return {
            "summary_raw": summary,
            "status": AgentStatus.SUCCESS.value,
        }
