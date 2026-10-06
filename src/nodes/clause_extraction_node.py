"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum values — never bare strings
#
# SVC-C2-002 — ClauseExtractionNode.
#
# Inner domain node 2: categorizes the parsed clause blocks into the five
# sections the review report is built from.
#
# Input:  state["parsed_clauses"] / state["contract_text"]
#         state["runtime_settings"] — JSON: {"risk_threshold": float, ...}
# Output: state["extracted_sections"] with the keys
#         parties, key_terms, obligations, risk_flags, renewal_termination_dates
#
# Extraction is deterministic: the same document always yields the same
# sections, which is what makes the report reviewable and the tests meaningful.
# Every extracted item cites the clause it came from, so a reader can go back to
# the source text rather than trusting the summary.

import logging
import re
from typing import Any, ClassVar, Dict, List, NamedTuple, Optional, Set, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.nodes.contract_parse_node import read_settings
from src.services.service import Service

logger = logging.getLogger(__name__)

# Upper bound on any excerpt copied out of the contract and into the report.
MAX_EXCERPT_CHARS = 320

SECTION_KEYS = (
    "parties",
    "key_terms",
    "obligations",
    "risk_flags",
    "renewal_termination_dates",
)

# --- Parties ---------------------------------------------------------------
# A defined party in a contract is a name immediately followed by its quoted
# role: `Company A Inc., a corporation ("Service Provider")`.
_PARTY_RE = re.compile(r"([A-Z][\w&.,'\- ]{2,60}?)\s*(?:,[^(\n]{0,60})?\(\s*[\"“]([^\"”\n]{2,40})[\"”]\s*\)")

# --- Key terms -------------------------------------------------------------
# Each entry: (term, pattern). The first clause matching the pattern supplies
# the excerpt and the clause reference.
_KEY_TERM_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("payment", re.compile(r"\b(?:payment|fee|fees|invoice|remuneration|compensation)\b", re.IGNORECASE)),
    ("duration", re.compile(r"\b(?:term|duration|commences?|expires?|effective\s+date)\b", re.IGNORECASE)),
    ("service_level", re.compile(r"\b(?:service\s+level|SLA|uptime|availability|response\s+time)\b", re.IGNORECASE)),
    (
        "ip_ownership",
        re.compile(r"\b(?:intellectual\s+property|IP\s+ownership|copyright|work\s+product)\b", re.IGNORECASE),
    ),
    ("exclusivity", re.compile(r"\b(?:exclusiv\w*|non-?compete|sole\s+supplier)\b", re.IGNORECASE)),
    ("liability", re.compile(r"\b(?:liabilit\w+|indemnif\w+|limitation\s+of\s+liability)\b", re.IGNORECASE)),
    ("confidentiality", re.compile(r"\b(?:confidential\w*|non-?disclosure|trade\s+secret)\b", re.IGNORECASE)),
    ("governing_law", re.compile(r"\b(?:governing\s+law|jurisdiction|governed\s+by\s+the\s+laws)\b", re.IGNORECASE)),
]

# --- Obligations -----------------------------------------------------------
_OBLIGATION_RE = re.compile(
    r"\b(?:shall|must|agrees?\s+to|undertakes?\s+to|is\s+obligated\s+to|will\s+be\s+responsible\s+for)\b",
    re.IGNORECASE,
)

# --- Dates -----------------------------------------------------------------
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}年\s*\d{1,2}月\s*\d{1,2}日)\b"
)
_NOTICE_RE = re.compile(r"\b(\d{1,3})\s*(?:calendar\s+|business\s+)?(days?|weeks?|months?)\b", re.IGNORECASE)
_RENEWAL_RE = re.compile(r"\brenew\w*\b", re.IGNORECASE)
_TERMINATION_RE = re.compile(r"\bterminat\w*\b|\bexpir\w*\b", re.IGNORECASE)


class _Span(NamedTuple):
    """The character span a rule matched, in the shape _excerpt() expects."""

    start_index: int
    end_index: int

    def start(self) -> int:
        return self.start_index

    def end(self) -> int:
        return self.end_index


def _excerpt(text: str, around: Optional[Any] = None) -> str:
    """Return a bounded, structurally inert excerpt of contract text.

    Collapses the excerpt to a single line and neutralizes a leading Markdown
    heading marker. The report's own section headings are what the quality gate
    checks, so a contract line beginning with `##` must not be able to render as
    one.
    """
    if around is not None:
        start = max(0, around.start() - 80)
        end = min(len(text), around.end() + 200)
        # Snap both edges to a word boundary so an excerpt never opens or closes
        # mid-word; a truncated edge is marked so the reader can see it is one.
        if start > 0:
            space = text.find(" ", start)
            start = space + 1 if 0 <= space < around.start() else start
        if end < len(text):
            space = text.rfind(" ", around.end(), end)
            end = space if space > around.end() else end
        snippet = text[start:end]
        snippet = ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")
    else:
        snippet = text
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if len(snippet) > MAX_EXCERPT_CHARS:
        snippet = snippet[: MAX_EXCERPT_CHARS - 1].rstrip() + "…"
    return re.sub(r"^[#>*\-\s]+", "", snippet)


def _extract_parties(text: str) -> List[Dict[str, Any]]:
    """Identify the contracting parties and the role each is defined as."""
    parties: List[Dict[str, Any]] = []
    seen = set()
    for match in _PARTY_RE.finditer(text):
        name = re.sub(r"\s+", " ", match.group(1)).strip(" ,;")
        role = re.sub(r"\s+", " ", match.group(2)).strip()
        key = (name.lower(), role.lower())
        if not name or key in seen:
            continue
        seen.add(key)
        parties.append({"name": name[:120], "role": role[:60], "signatory": None})
    return parties


def _extract_key_terms(clauses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pick the first clause that defines each commercial term."""
    terms: List[Dict[str, Any]] = []
    for term, pattern in _KEY_TERM_PATTERNS:
        for clause in clauses:
            raw = str(clause.get("raw_text", ""))
            match = pattern.search(raw)
            if match:
                terms.append(
                    {
                        "term": term,
                        "value": _excerpt(raw, match),
                        "clause_ref": str(clause.get("clause_id", "clause_001")),
                    }
                )
                break
    return terms


def _extract_obligations(clauses: List[Dict[str, Any]], parties: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collect the sentences that impose a duty, attributed where possible."""
    role_names = [str(p.get("role", "")) for p in parties if p.get("role")]
    obligations: List[Dict[str, Any]] = []
    for clause in clauses:
        raw = str(clause.get("raw_text", ""))
        for sentence in re.split(r"(?<=[.!?。])\s+", raw):
            if not _OBLIGATION_RE.search(sentence):
                continue
            party = next((role for role in role_names if role.lower() in sentence.lower()), "the parties")
            deadline_match = _DATE_RE.search(sentence) or _NOTICE_RE.search(sentence)
            obligations.append(
                {
                    "party": party,
                    "obligation": _excerpt(sentence),
                    "deadline": deadline_match.group(0) if deadline_match else None,
                    "clause_ref": str(clause.get("clause_id", "clause_001")),
                }
            )
            break
    return obligations


def _extract_risk_flags(
    clauses: List[Dict[str, Any]], threshold: float, service: Service
) -> Tuple[List[Dict[str, Any]], int]:
    """Score each clause against the domain risk rules and apply the floor.

    Returns (rendered_flags, suppressed_count). A finding below the configured
    threshold is counted but not listed, so the report states how much was
    filtered rather than silently dropping it. Each rule contributes at most one
    flag per document, cited to the first clause that triggered it.
    """
    rendered: List[Dict[str, Any]] = []
    suppressed = 0
    seen: Set[str] = set()
    for clause in clauses:
        raw = str(clause.get("raw_text", ""))
        for finding in service.score_clause(raw):
            description = str(finding["description"])
            if description in seen:
                continue
            seen.add(description)
            score = float(finding["score"])
            if score < threshold:
                suppressed += 1
                continue
            start, end = finding["span"]
            rendered.append(
                {
                    "description": description,
                    "severity": str(finding["severity"]),
                    "score": round(score, 3),
                    "evidence": _excerpt(raw, _Span(start, end)),
                    "clause_ref": str(clause.get("clause_id", "clause_001")),
                }
            )
    return rendered, suppressed


def _extract_dates(clauses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collect renewal and termination events with their notice periods."""
    events: List[Dict[str, Any]] = []
    seen = set()
    for clause in clauses:
        raw = str(clause.get("raw_text", ""))
        for sentence in re.split(r"(?<=[.!?。])\s+", raw):
            is_renewal = bool(_RENEWAL_RE.search(sentence))
            is_termination = bool(_TERMINATION_RE.search(sentence))
            if not (is_renewal or is_termination):
                continue
            date_match = _DATE_RE.search(sentence)
            notice_match = _NOTICE_RE.search(sentence)
            if not (date_match or notice_match):
                continue
            event_type = "renewal" if is_renewal else "termination"
            key = (event_type, date_match.group(0) if date_match else "", notice_match.group(0) if notice_match else "")
            if key in seen:
                continue
            seen.add(key)
            events.append(
                {
                    "event_type": event_type,
                    "date": date_match.group(0) if date_match else None,
                    "notice_period": notice_match.group(0) if notice_match else None,
                    "clause_ref": str(clause.get("clause_id", "clause_001")),
                }
            )
    return events


class ClauseExtractionNode(FunctionNode):
    """Categorize the parsed clauses into the five report sections.

    Inner domain node 2, registered as "clause_extraction".

    Input state keys:
        parsed_clauses:   clause blocks (ContractParseNode)
        contract_text:    normalized document text
        runtime_settings: JSON-encoded settings ({"risk_threshold": float})

    Output state keys (partial dict):
        extracted_sections: mapping with the five section keys
        status / error_log
    """

    # See ContractParseNode — the trust decision is taken at the caller-contract
    # gate; the caller's level travels unchanged into the inner graph.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        clauses = state.get("parsed_clauses") or []
        contract_text = state.get("contract_text") or ""
        if not isinstance(clauses, list) or not clauses:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClauseExtractionNode: no clause blocks reached this node " "(parsed_clauses is empty)"],
            }

        threshold = float(read_settings(state).get("risk_threshold", 0.5))

        parties = _extract_parties(str(contract_text))
        key_terms = _extract_key_terms(clauses)
        obligations = _extract_obligations(clauses, parties)
        risk_flags, suppressed = _extract_risk_flags(clauses, threshold, Service())
        dates = _extract_dates(clauses)

        sections: Dict[str, Any] = {
            "parties": parties,
            "key_terms": key_terms,
            "obligations": obligations,
            "risk_flags": risk_flags,
            "renewal_termination_dates": dates,
            "risk_flags_suppressed": suppressed,
        }

        logger.info(
            "ClauseExtractionNode: extracted — parties=%d key_terms=%d obligations=%d "
            "risk_flags=%d (suppressed=%d, threshold=%.3f) dates=%d",
            len(parties),
            len(key_terms),
            len(obligations),
            len(risk_flags),
            suppressed,
            threshold,
            len(dates),
        )
        emit_trace_event(
            "clauses_extracted",
            {
                "clause_count": len(clauses),
                "risk_flag_count": len(risk_flags),
                "risk_flags_suppressed": suppressed,
                "risk_threshold": threshold,
            },
            state,
        )

        return {
            "extracted_sections": sections,
            "status": AgentStatus.SUCCESS.value,
        }
