"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum values — never bare strings
#  - Read input_context via state.get("input_context", {}) — read-only
#
# SVC-C2-002 — PreProcessNode.
#
# The pre_process backbone slot. This node owns the caller-data contract and is
# the single trust boundary of the template: it declares
# required_trust_level = VERIFIED_EXTERNAL, so an unauthenticated caller is
# refused here, before any domain node runs.
#
# It validates every caller-supplied field against explicit bounds, refuses
# prompt-injection payloads itself rather than relying on the platform gate
# being active, and publishes only validated values onward.
#
# Caller-data contract (all fields optional; each is bounded):
#   document_text       the contract to review (str, 1..MAX_DOCUMENT_CHARS)
#   contract_reference  label echoed into the report; inert [a-z0-9_]{1,32}
#   risk_threshold      severity floor for rendered risk flags (number, 0..1)
#   max_clauses         cap on clause blocks parsed (integer, 1..2000)
#
# When input_context carries no document, `user_input` is accepted as the
# document instead, so a plain text invocation still works.

import json
import logging
import re
import unicodedata
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

MIN_DOCUMENT_CHARS = 10
MAX_DOCUMENT_CHARS = 200_000

_MAX_CLAUSES_MIN, _MAX_CLAUSES_MAX = 1, 2000
_RISK_THRESHOLD_MIN, _RISK_THRESHOLD_MAX = 0.0, 1.0

# Caller strings that render into the report are locked to this inert alphabet:
# free text on that channel would be caller-controlled output injection.
_INERT_IDENTIFIER_RE = re.compile(r"\A[a-z0-9_]{1,32}\Z")

# Field names are caller data too. A name that does not match this is reported
# positionally rather than echoed.
_SAFE_FIELD_NAME_RE = re.compile(r"\A[A-Za-z0-9_.\-]{1,40}\Z")

# Recognised caller fields. An undeclared caller field is refused rather than
# ignored: ignoring is not stripping, and the key stays on input_context, where
# the platform's output gate scans it and fails the run at the first node.
_CALLER_CONTEXT_FIELDS = frozenset({"document_text", "contract_reference", "risk_threshold", "max_clauses"})

# Fields the hosting runtime puts on the context channel itself. They are not
# part of the caller contract and this agent reads none of them, but refusing
# them would refuse every invocation served that way — the caller cannot remove
# a field it never added. They are accepted and ignored, which is sound exactly
# because no constraint is attached to them: nothing is promised about a field
# this agent does not read, so there is nothing for the caller to be misled
# about. For the same reason they are left out of the screens below: a value
# that is never read cannot reach a prompt or a report, so screening it would
# only refuse ordinary requests without removing any reachable path.
_RUNTIME_CONTEXT_FIELDS = frozenset({"conversation_history"})

_ACCEPTED_CONTEXT_FIELDS = _CALLER_CONTEXT_FIELDS | _RUNTIME_CONTEXT_FIELDS

# ---------------------------------------------------------------------------
# Injection screening
# ---------------------------------------------------------------------------
#
# Two families, both screened. Directive phrases are matched with both ends
# anchored to word boundaries so ordinary contract language ("the parties shall
# act as a single entity") does not trip them. Chat-template control tokens are
# matched structurally, because an attack does not have to use a phrase at all:
# `<|im_start|>system ...` carries no directive wording a phrase list would see.

_INJECTION_PHRASES: List[Tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+)?"
            r"(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
            r"(?:instruction|instructions|prompt|prompts|rule|rules)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "persona_override",
        re.compile(
            r"\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:dan|jailbreak|unrestricted|developer\s+mode)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_probe",
        re.compile(
            r"\b(?:reveal|print|show|repeat|output)\s+(?:me\s+)?(?:your|the)\s+"
            r"(?:system\s+prompt|initial\s+instructions|hidden\s+instructions)\b",
            re.IGNORECASE,
        ),
    ),
]

_CONTROL_TOKENS: List[Tuple[str, re.Pattern[str]]] = [
    ("chat_template_token", re.compile(r"<\|[^|>]{0,64}\|>")),
    ("instruction_tag", re.compile(r"\[/?INST\]", re.IGNORECASE)),
    ("system_tag", re.compile(r"<</?SYS>>", re.IGNORECASE)),
]

# Characters removed before the second screening pass. Stripping markup can
# splice a directive back together out of fragments a raw scan would not match
# ("ig<b>nore all previous instructions"), so the payload is screened BOTH raw —
# which catches control tokens before they are removed — and again afterwards.
_MARKUP_RE = re.compile(r"</?[A-Za-z][^>]{0,120}>")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_markup(text: str) -> str:
    """Remove HTML-ish markup and zero-width characters, then collapse spacing."""
    stripped = _MARKUP_RE.sub("", text)
    stripped = stripped.replace("​", "").replace("‌", "").replace("‍", "")
    return re.sub(r"[ \t]{2,}", " ", stripped)


def _screen_text(text: str) -> Optional[str]:
    """Return the name of the first injection family found, or None if clean.

    The raw form and the markup-stripped form are both screened: control tokens
    only exist in the raw form, spliced directives only in the stripped one.
    """
    for candidate in (text, _strip_markup(text)):
        for name, pattern in _CONTROL_TOKENS:
            if pattern.search(candidate):
                return name
        for name, pattern in _INJECTION_PHRASES:
            if pattern.search(candidate):
                return name
    return None


def _screen_structure(value: Any, path: str) -> Optional[Tuple[str, str]]:
    """Depth-first injection screen over a parsed payload, keys included.

    Returns (family, location) for the first finding, or None. Locations are
    field paths only — a matched value is never returned, because it may itself
    carry a credential shape the platform's output gate would then refuse.
    """
    if isinstance(value, str):
        found = _screen_text(value)
        return (found, path) if found else None
    if isinstance(value, dict):
        for key in value:
            key_text = key if isinstance(key, str) else str(key)
            found = _screen_text(key_text)
            if found:
                return (found, f"{path}.<key #{list(value).index(key) + 1}>")
            nested = _screen_structure(value[key], f"{path}.{_safe_name(key_text)}")
            if nested:
                return nested
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            nested = _screen_structure(item, f"{path}[{index}]")
            if nested:
                return nested
    return None


def _safe_name(name: str) -> str:
    """Render a caller-supplied field name, or mask it when it is not inert."""
    return name if _SAFE_FIELD_NAME_RE.match(name) else "<masked>"


# ---------------------------------------------------------------------------
# Numeric contract
# ---------------------------------------------------------------------------


def _finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse one caller number: real, finite, within [lo, hi]; else None.

    Bools are rejected explicitly (bool is a subclass of int). Strings are
    parsed, so "NaN" and "Infinity" — which float() accepts and which arrive
    verbatim through JSON — are rejected here rather than silently comparing
    False against every later bound and failing open.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except (TypeError, ValueError):
            return None
    elif isinstance(value, (int, float)):
        parsed = float(value)
    else:
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    if not lo <= parsed <= hi:
        return None
    return parsed


def _reject(message: str, code: str = "INVALID_REQUEST") -> Dict[str, Any]:
    """Build the refusal delta. `message` names fields, never their values.

    `formatted_output` is set to a TRUTHY notice for two reasons. It is what the
    caller actually receives — the platform resolves an agent's output as
    `formatted_output or result`, so a refusal that sets neither returns an empty
    envelope with no reason in it. And because that resolution has no status
    check, a falsy value here would fall through to whatever `result` happens to
    hold, which is exactly the fallback a refusal must not re-arm.

    The message is safe to surface: every caller of this helper passes a field
    name and a bound, never a caller-supplied value.

    Two ways to stop, and the caller can act on only one of them. A value the
    caller can correct completes the run carrying ``code``, so the reason
    reaches the caller and a corrected request can be sent on the same
    conversation. Content the agent refuses outright passes ``code=""`` and
    terminates, so a refusal is never presented as something a reworded request
    would get past.

    The branch is chosen by the call site through ``code``, never by reading
    *message*: the two injection screens pass ``code=""``.
    """
    if code:
        # A value the caller can correct: the run COMPLETES carrying the
        # reason so the request can be sent again on the same conversation.
        emit_progress(INPUT_REJECTED)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": code,
            "error_log": [f"PreProcessNode: {message}"],
            "document_text": None,
            "validated_input": None,
            "result": None,
            "formatted_output": f"The request was not accepted. {message[0].upper()}{message[1:]}.",
        }
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [f"PreProcessNode: {message}"],
        "document_text": None,
        "validated_input": None,
        "result": None,
        "formatted_output": f"The request was not accepted. {message[0].upper()}{message[1:]}.",
    }


class PreProcessNode(FunctionNode):
    """Caller-contract gate for the contract review pipeline.

    Validates the caller-supplied document and parameters, screens them for
    prompt-injection payloads, and publishes only validated values. This node
    declares the template's trust requirement, so an unauthenticated caller is
    refused before any domain node runs.

    Input state keys:
        user_input:       contract text, used when input_context carries none
        input_context:    the structured caller-data channel
        runtime_settings: JSON-encoded declared settings (bounds floor)

    Output state keys (partial dict):
        document_text:      the validated contract document
        contract_reference: validated inert label, or ""
        runtime_settings:   JSON-encoded settings after caller overrides
        validated_input:    framework-visible copy of the document
        status / error_log
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        raw_context = state.get("input_context") or {}
        if not isinstance(raw_context, dict):
            return _reject("input_context must be a mapping")

        # Settings floor: what config/config.yaml declared, already validated by
        # the outer graph. A caller may narrow within the same bounds.
        settings = self._declared_settings(state)

        # --- Injection screen over the whole parsed payload --------------
        # Runs BEFORE the unknown-field check: a directive written into an
        # undeclared key matches both rules, and only one of them is the real
        # reason. Checked the other way round it reads as "a field this agent
        # does not read" - a correctable value - and the refusal is never
        # reported as one.
        caller_context = {name: value for name, value in raw_context.items() if name not in _RUNTIME_CONTEXT_FIELDS}

        finding = _screen_structure(caller_context, "input_context")
        if finding:
            family, location = finding
            # Terminal: a refusal, not a correctable value. Rewording the
            # request must not be presented as a route past it.
            return _reject(f"refused: {family} pattern at {location}", code="")

        # --- Unknown fields are refused, not ignored ---------------------
        unknown = [name for name in raw_context if name not in _ACCEPTED_CONTEXT_FIELDS]
        if unknown:
            named = ", ".join(sorted(_safe_name(str(name)) for name in unknown))
            return _reject(f"input_context carries unsupported field(s): {named}")

        # --- Document ----------------------------------------------------
        document = raw_context.get("document_text")
        source_field = "input_context.document_text"
        if document is None:
            document = state.get("user_input")
            source_field = "user_input"
        if not isinstance(document, str):
            return _reject(f"{source_field} must be a string")

        document = _CONTROL_CHAR_RE.sub("", unicodedata.normalize("NFKC", document)).strip()
        if not document:
            # Reported apart from the minimum-length case below: a caller who
            # sent nothing needs to be told to send the document, not that what
            # they sent was too short.
            return _reject(f"{source_field} carries no document", code="EMPTY_INPUT")
        if len(document) < MIN_DOCUMENT_CHARS:
            return _reject(f"{source_field} is shorter than the {MIN_DOCUMENT_CHARS}-character minimum")
        if len(document) > MAX_DOCUMENT_CHARS:
            return _reject(
                f"{source_field} exceeds the {MAX_DOCUMENT_CHARS}-character maximum",
                code="QUESTION_TOO_LONG",
            )

        document_family = _screen_text(document)
        if document_family:
            # Terminal for the same reason as the structured-channel screen: a
            # directive in the document is the same refusal.
            return _reject(f"refused: {document_family} pattern at {source_field}", code="")

        # --- Caller label ------------------------------------------------
        reference = raw_context.get("contract_reference", "")
        if reference != "":
            if not isinstance(reference, str) or not _INERT_IDENTIFIER_RE.match(reference):
                return _reject("input_context.contract_reference must match [a-z0-9_] and be 1-32 characters")

        # --- Caller numerics ---------------------------------------------
        if "risk_threshold" in raw_context:
            threshold = _finite_in_range(raw_context["risk_threshold"], _RISK_THRESHOLD_MIN, _RISK_THRESHOLD_MAX)
            if threshold is None:
                return _reject(
                    "input_context.risk_threshold must be a finite number between "
                    f"{_RISK_THRESHOLD_MIN} and {_RISK_THRESHOLD_MAX}"
                )
            settings["risk_threshold"] = threshold

        if "max_clauses" in raw_context:
            cap = _finite_in_range(raw_context["max_clauses"], _MAX_CLAUSES_MIN, _MAX_CLAUSES_MAX)
            if cap is None or cap != int(cap):
                return _reject(
                    "input_context.max_clauses must be a finite whole number between "
                    f"{_MAX_CLAUSES_MIN} and {_MAX_CLAUSES_MAX}"
                )
            settings["max_clauses"] = int(cap)

        emit_trace_event(
            "caller_contract_validated",
            {
                "document_chars": len(document),
                "reference_supplied": bool(reference),
                "risk_threshold": settings["risk_threshold"],
                "max_clauses": settings["max_clauses"],
            },
            state,
        )
        logger.info(
            "PreProcessNode: caller contract validated — %d document characters, " "risk_threshold=%.3f max_clauses=%d",
            len(document),
            settings["risk_threshold"],
            settings["max_clauses"],
        )

        return {
            "document_text": document,
            "contract_reference": reference if isinstance(reference, str) else "",
            "runtime_settings": json.dumps(settings),
            "validated_input": document,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _declared_settings(state: AgentState) -> Dict[str, Any]:
        """Read the declared settings the outer graph seeded into state.

        Returns the built-in floor when the field is absent or unreadable, so a
        node-level call still runs on a complete settings mapping.
        """
        floor: Dict[str, Any] = {"max_clauses": 200, "risk_threshold": 0.5}
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
