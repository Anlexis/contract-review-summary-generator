"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum values — never bare strings
#
# SVC-C2-002 — PostProcessNode.
#
# The post_process backbone slot: the output boundary of the template. It reads
# the review report published by the main slot and decides whether it may be
# released.
#
# Two independent checks, each with its own audit event:
#   1. Credential scan. The platform's own detector is used, not a local copy of
#      it — a narrower local set would let a value through that the platform
#      then refuses one layer later, and that refusal discards this node's
#      entire state update, including the clearing below. Domain patterns are
#      added ON TOP of the platform set, never in place of it.
#   2. Disclaimer invariant. Every released report carries the mandatory legal
#      disclaimer; it is re-attached here if it is somehow absent.
#
# Containment. The platform resolves an agent's output as
# `formatted_output or result`, with no status check, so returning an error
# status is not by itself a refusal: an un-gated report left in `result` would
# still be delivered inside the error envelope. On a violation this node
# therefore CLEARS every field that carries report text and writes a TRUTHY
# refusal notice — a falsy notice would re-activate exactly the fallback the
# refusal exists to prevent.
#
# Violation messages name FIELD PATHS ONLY. Quoting the matched text would put
# the credential back into a returned value, where the platform's output gate
# would refuse it and discard this node's update along with the clearing.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

logger = logging.getLogger(__name__)

# Kept identical to the inner gate's copy; both enforce the same invariant.
MANDATORY_DISCLAIMER = (
    "This summary is for internal review purposes only. "
    "Consult qualified legal counsel before relying on any legal analysis."
)

# Fragment used to detect the disclaimer, case-insensitively.
_DISCLAIMER_FRAGMENT = "internal review purposes only"

# Domain patterns checked IN ADDITION to the platform detector. They widen the
# refusal set; they never replace it.
_EXTRA_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
    ("private_key_block", re.compile(r"-----BEGIN\s+[A-Z ]*PRIVATE KEY-----")),
]

# The refusal notice. Truthy by contract: the platform falls back to `result`
# whenever this value is falsy, which is the leak this notice prevents.
REFUSAL_NOTICE = (
    "The contract review summary was withheld by the output gate because it "
    "contained content that may not be released. No part of the generated "
    "summary is included in this response. Remove credential-like strings from "
    "the source document and submit it again."
)

# Fields that carry report text. Every one is cleared on a refusal. The gate
# asserts that the fields it returns are exactly these plus the inert
# provenance keys below, so a field added to the state later cannot quietly
# start riding out through a refusal.
_OUTPUT_BEARING_FIELDS = ("result", "summary_final")
_INERT_REFUSAL_FIELDS = ("formatted_output", "status", "error_log", "review_metadata")


def scan_for_disallowed_content(content: str) -> Optional[str]:
    """Return the name of the first disallowed pattern found, or None.

    The platform detector runs first so that this gate's refusal set is exactly
    the platform's, widened by the domain patterns — never narrower.
    """
    findings = detect_credentials(content)
    if findings:
        return str(findings[0]["type"])
    for name, pattern in _EXTRA_PATTERNS:
        if pattern.search(content):
            return name
    return None


def _refusal(field_path: str, violation: str) -> Dict[str, Any]:
    """Build the refusal delta: everything text-bearing cleared, notice truthy.

    The delta is CONSTRUCTED from _OUTPUT_BEARING_FIELDS rather than written out
    by hand, so a field added to that inventory is cleared automatically and one
    that is missing from it is visible in a single place. The inventory itself is
    pinned by a test — checking it here would mean raising from inside execute(),
    and a raise is the one thing this function must never do: the platform's node
    wrapper turns an exception into a bare error update and discards the clearing
    along with it.

    `field_path` and `violation` are a state field name and a pattern-family
    name. Neither is caller data and neither carries the matched text.
    """
    delta: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
    delta["formatted_output"] = REFUSAL_NOTICE
    delta["status"] = AgentStatus.ERROR.value
    delta["error_log"] = [
        f"PostProcessNode: output withheld — disallowed content ({violation}) " f"detected in state['{field_path}']"
    ]
    delta["review_metadata"] = {}
    return delta


def _ensure_disclaimer(content: str) -> str:
    """Prepend the mandatory legal disclaimer when it is not already present."""
    if _DISCLAIMER_FRAGMENT.lower() in content.lower():
        return content
    logger.info("PostProcessNode: disclaimer absent from the report — attaching it")
    return f"{MANDATORY_DISCLAIMER}\n\n{content}"


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Output gate for the contract review report.

    Input state keys:
        result: the review report published by the main slot

    Output state keys (partial dict):
        result:           the released report, or None on a refusal
        summary_final:    cleared on a refusal
        formatted_output: the truthy refusal notice, or None on release
        status / error_log / review_metadata
    """

    # The template's trust decision is taken once, at the caller-contract gate.
    # Requiring a higher level HERE would be worse than pointless: a caller below
    # it would be refused at the gate's own trust check, which returns before
    # execute() runs — so the output scan would be skipped for exactly the
    # callers it matters most for.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "formatted_output": message,
                "result": message,
            }
        result = state.get("result") or ""

        if not isinstance(result, str) or not result.strip():
            # Nothing was produced upstream. Say so rather than releasing an
            # empty success: `formatted_output` stays truthy so the platform's
            # `formatted_output or result` resolution cannot reach back into a
            # stale `result` value left in state by an earlier step.
            logger.warning("PostProcessNode: no report reached the output gate")
            emit_trace_event("output_gate_empty", {"released": False}, state)
            return {
                "result": None,
                "summary_final": None,
                "formatted_output": ("No contract review summary was produced for this request."),
                "status": AgentStatus.ERROR.value,
                "error_log": ["PostProcessNode: no report reached the output gate"],
                "review_metadata": {},
            }

        violation = scan_for_disallowed_content(result)
        if violation:
            logger.error("PostProcessNode: output withheld — violation family %s", violation)
            emit_trace_event(
                "output_gate_blocked",
                {"violation": violation, "field": "result", "released": False},
                state,
            )
            return _refusal("result", violation)

        released = _ensure_disclaimer(result)

        logger.info("PostProcessNode: output released — %d characters", len(released))
        emit_trace_event(
            "output_gate_released",
            {"output_len": len(released), "released": True},
            state,
        )

        return {
            "result": released,
            "formatted_output": None,
            "status": AgentStatus.SUCCESS.value,
        }
