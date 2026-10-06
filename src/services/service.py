"""AgentCore Platform v1.0"""

# Service layer: domain rules and lookups used by the nodes.
# No routing, no graph wiring and no credentials belong here.
#
# SVC-C2-002 — the contract risk lexicon.
#
# Which clause language counts as a review risk, and how serious it is, is
# domain knowledge rather than pipeline logic: it is the part a team adapting
# this template is most likely to change, and it is the part that should be
# reviewable on its own. Keeping it here means the extraction node stays a thin
# orchestrator over an explicit, testable rule set.
#
# Each rule carries a severity SCORE in 0..1. The score is compared against the
# risk_threshold declared in config/config.yaml (or narrowed per request by the
# caller), so the same document produces a shorter or longer Risk Flags section
# depending on the configured floor.

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, NamedTuple


class RiskRule(NamedTuple):
    """One contract-risk rule: what to look for, and how serious it is."""

    description: str
    pattern: re.Pattern[str]
    score: float
    recommended_action: str


# Ordered most severe first, so the highest-scoring rule for a clause is the one
# a reader sees first when several match.
RISK_RULES: List[RiskRule] = [
    RiskRule(
        "Unlimited or uncapped liability exposure",
        re.compile(r"\bunlimited\s+liabilit\w+|\bwithout\s+limitation\s+of\s+liabilit\w+", re.IGNORECASE),
        0.95,
        "Negotiate a mutual liability cap before signature.",
    ),
    RiskRule(
        "Automatic renewal without an explicit opt-out",
        re.compile(r"\bautomatically\s+renew\w*|\bauto-?renew\w*", re.IGNORECASE),
        0.85,
        "Diarise the opt-out window and confirm the notice channel.",
    ),
    RiskRule(
        "One-sided termination right",
        re.compile(r"\bsole\s+discretion\b|\bat\s+its\s+own\s+discretion\b", re.IGNORECASE),
        0.80,
        "Request reciprocal termination rights on equivalent notice.",
    ),
    RiskRule(
        "Liability cap benefits one party only",
        re.compile(r"\blimited\s+to\s+the\s+fees\b|\bliabilit\w+\s+is\s+limited\s+to\b", re.IGNORECASE),
        0.70,
        "Make the liability cap mutual, or price the asymmetry.",
    ),
    RiskRule(
        "Indemnity obligation without a reciprocal clause",
        re.compile(r"\bindemnif\w+\b", re.IGNORECASE),
        0.60,
        "Check the indemnity is mutual and bounded.",
    ),
    RiskRule(
        "Exclusivity or non-compete restriction",
        re.compile(r"\bexclusiv\w*\b|\bnon-?compete\b", re.IGNORECASE),
        0.55,
        "Confirm the exclusivity scope and duration are commercially acceptable.",
    ),
    RiskRule(
        "Penalty or liquidated damages provision",
        re.compile(r"\bpenalt\w+\b|\bliquidated\s+damages\b", re.IGNORECASE),
        0.50,
        "Verify the damages formula against realistic delivery risk.",
    ),
    RiskRule(
        "Service levels referenced but not quantified",
        re.compile(r"\bservice\s+levels?\b(?![^.]{0,80}\d)", re.IGNORECASE),
        0.35,
        "Quantify the service levels in an annex before signature.",
    ),
    RiskRule(
        "Governing law or jurisdiction may be unfavourable",
        re.compile(r"\bgoverning\s+law\b|\bjurisdiction\b", re.IGNORECASE),
        0.25,
        "Confirm the governing law is acceptable to your legal team.",
    ),
]

# Severity bands used in the rendered report.
_HIGH_FROM, _MEDIUM_FROM = 0.75, 0.45


def severity_band(score: float) -> str:
    """Map a rule score onto the label the report prints."""
    if score >= _HIGH_FROM:
        return "high"
    if score >= _MEDIUM_FROM:
        return "medium"
    return "low"


def recommended_action(description: str) -> str:
    """Return the follow-up action for a risk description, or an empty string."""
    for rule in RISK_RULES:
        if rule.description == description:
            return rule.recommended_action
    return ""


class Service:
    """Domain rules for contract review."""

    def __init__(self, rules: Iterable[RiskRule] | None = None) -> None:
        self._rules: List[RiskRule] = list(rules) if rules is not None else list(RISK_RULES)

    @property
    def rules(self) -> List[RiskRule]:
        """The rule set this service scores against."""
        return list(self._rules)

    def score_clause(self, clause_text: str) -> List[Dict[str, Any]]:
        """Return every rule that matches `clause_text`, most severe first.

        Each finding carries the rule description, its score and the character
        span that matched, so the caller can build a bounded excerpt without
        re-running the pattern.
        """
        findings: List[Dict[str, Any]] = []
        for rule in self._rules:
            match = rule.pattern.search(clause_text)
            if match:
                findings.append(
                    {
                        "description": rule.description,
                        "score": rule.score,
                        "severity": severity_band(rule.score),
                        "span": (match.start(), match.end()),
                    }
                )
        return findings
