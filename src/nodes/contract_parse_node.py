"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum values — never bare strings
#
# SVC-C2-002 — ContractParseNode.
#
# Inner domain node 1: normalizes the validated contract document and splits it
# into clause blocks.
#
# Input:  state["document_text"] — the document, bridged in from the outer graph
#         state["runtime_settings"] — JSON: {"max_clauses": int, ...}
# Output: state["contract_text"]  — normalized document text
#         state["parsed_clauses"] — clause blocks
#
# The node accepts text only. Document conversion (PDF, DOCX, scanned pages)
# belongs upstream of the agent: resolving a caller-supplied path here would
# turn the request channel into a local-filesystem probe, and the answer would
# depend on optional extraction libraries that are not part of this template's
# dependency set.

import json
import logging
import re
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CLAUSES = 200

# Clause headings: "Article 1.", "Section 2:", "CLAUSE III —", "第3条".
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?m)^(?:(?:Article|Section|Clause|ARTICLE|SECTION|CLAUSE)\s+[\dIVXivx]+[.\s:\u2014)]"
    r"|\u7b2c\s*[\d\uff10-\uff19]+\s*\u6761)"
)

# Below this length a heading match is a stray reference rather than a clause.
_MIN_CLAUSE_LENGTH = 20

# Unicode spacing variants normalized to a plain space.
_UNICODE_SPACE_RE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")


def _normalize_text(text: str) -> str:
    """Normalize whitespace in the extracted contract text.

    Collapses Unicode spacing variants, strips trailing whitespace per line and
    reduces runs of three or more blank lines to two. Line structure is
    otherwise preserved, because clause headings are matched per line.
    """
    text = _UNICODE_SPACE_RE.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_into_clauses(text: str, max_clauses: int) -> List[Dict[str, Any]]:
    """Split normalized contract text into clause blocks.

    Uses heading patterns as boundaries and falls back to a single block when the
    document carries none. At most `max_clauses` blocks are returned; the cap is
    a declared runtime parameter, so a very long document produces a bounded
    amount of work and a bounded report.
    """
    matches = list(_CLAUSE_BOUNDARY_RE.finditer(text))

    if not matches:
        return [{"clause_id": "clause_001", "clause_type": "general", "raw_text": text.strip()}]

    clauses: List[Dict[str, Any]] = []
    for index, match in enumerate(matches):
        if len(clauses) >= max_clauses:
            break
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[start:end].strip()
        if len(raw) < _MIN_CLAUSE_LENGTH:
            continue
        heading = match.group(0).strip().rstrip(":\u2014)").strip()
        clauses.append(
            {
                "clause_id": f"clause_{len(clauses) + 1:03d}",
                "clause_type": heading.split()[0].lower() if heading.split() else "general",
                "raw_text": raw,
            }
        )

    if not clauses:
        return [{"clause_id": "clause_001", "clause_type": "general", "raw_text": text.strip()}]
    return clauses


def read_settings(state: AgentState) -> Dict[str, Any]:
    """Decode the runtime settings the graph republished into inner state.

    Returns the built-in floor when the field is absent or unreadable, so a
    direct node call still runs on a complete settings mapping.
    """
    floor: Dict[str, Any] = {"max_clauses": _DEFAULT_MAX_CLAUSES, "risk_threshold": 0.5}
    raw = state.get("runtime_settings")
    if not isinstance(raw, str) or not raw:
        return floor
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return floor
    if not isinstance(loaded, dict):
        return floor
    floor.update({key: loaded[key] for key in ("max_clauses", "risk_threshold") if key in loaded})
    return floor


class ContractParseNode(FunctionNode):
    """Normalize the contract document and split it into clause blocks.

    Inner domain node 1, registered as "contract_parse". The document arrives on
    the context bridge as `document_text`; `user_input` is accepted as a fallback
    so the node also works when called directly.

    Input state keys:
        document_text:    validated contract document
        runtime_settings: JSON-encoded settings ({"max_clauses": int})

    Output state keys (partial dict):
        contract_text:  normalized document text
        parsed_clauses: list of clause block dicts
        status / error_log
    """

    # The trust decision is taken once, at the caller-contract gate in the
    # pre_process slot. The caller's level travels unchanged into the inner
    # graph, so requiring a higher level here could never be satisfied — it
    # would refuse every request rather than add a check.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        document = state.get("document_text") or state.get("user_input") or ""
        if not isinstance(document, str) or not document.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "ContractParseNode: no contract document reached this node "
                    "(document_text and user_input are both empty)"
                ],
            }

        settings = read_settings(state)
        max_clauses = int(settings.get("max_clauses", _DEFAULT_MAX_CLAUSES))

        normalized_text = _normalize_text(document)
        if not normalized_text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ContractParseNode: the contract document is empty after normalization"],
            }

        parsed_clauses = _split_into_clauses(normalized_text, max_clauses)

        logger.info(
            "ContractParseNode: parsed contract — %d characters, %d clause blocks (cap %d)",
            len(normalized_text),
            len(parsed_clauses),
            max_clauses,
        )
        emit_trace_event(
            "contract_parsed",
            {
                "text_len": len(normalized_text),
                "clause_count": len(parsed_clauses),
                "max_clauses": max_clauses,
            },
            state,
        )

        return {
            "contract_text": normalized_text,
            "parsed_clauses": parsed_clauses,
            "status": AgentStatus.SUCCESS.value,
        }
