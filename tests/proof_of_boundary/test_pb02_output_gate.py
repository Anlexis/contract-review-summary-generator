# PB-02 — the output boundary (MANDATORY).
#
# Two things are proved here, and they are different things.
#
#   1. REFUSAL. The gate refuses a report carrying content that may not be
#      released, and its refusal set is the platform's own, widened by this
#      template's domain patterns — never narrower. A narrower set is not merely
#      a missed catch: the platform's own scan then refuses the node's result
#      one layer later, and that refusal DISCARDS the node's entire state update,
#      including the clearing below. A detector gap is a containment bypass.
#
#   2. CONTAINMENT. The platform resolves an agent's output as
#      `formatted_output or result`, with no status check. Returning an error
#      status is therefore not a refusal by itself, and a FALSY notice re-arms
#      the very fallback the refusal exists to prevent. The gate must clear the
#      report fields AND write a truthy notice.
#
# Note on assertion style: a partial state update is MERGED, so omitting a key
# leaves its previous value in place. `not result.get(field)` would therefore
# pass against a gate that clears nothing at all. Every clearing assertion below
# checks PRESENCE and emptiness — the key must be in the returned update, and
# its value must be the cleared one.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.security.credential_detector import _PATTERNS, detect_credentials
from src.nodes.post_process_node import (
    REFUSAL_NOTICE,
    PostProcessNode,
    _INERT_REFUSAL_FIELDS,
    _OUTPUT_BEARING_FIELDS,
    scan_for_disallowed_content,
)

# Simulated values, one per pattern family the platform detector knows. None is
# a real credential; each is only ever compared against a regular expression.
PLATFORM_SHAPES = {
    "stripe_key": "sk_live_" + "51H8xQ2eZvKYlo2C0abcdefgh",
    "openai_key": "sk-abcdefghijklmnopqrstuvwxyz012345",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fw",
    "aws_key": "AKIAIOSFODNN7EXAMPLE",
    "bearer_token": "Bearer abcdefghijklmnopqrstuvwxyz",
    "conn_string": "postgresql://contracts-db.example.test:5432/reviews",
}

# Families the platform detector does NOT cover, which this template adds.
DOMAIN_SHAPES = {
    "credential_assignment": "password = Sup3rSecretVaultPassphrase",
    "private_key_block": "-----BEGIN RSA PRIVATE KEY-----",
}

_REPORT = """\
## Parties

- **Company A Inc.** — Service Provider

## Key Terms

- **Payment** (clause_001): monthly at USD 10,000

## Obligations

- **Service Provider** (clause_001): deliver consulting services

## Risk Flags

- No risk indicators at or above the configured severity floor.

## Renewal / Termination Dates

- **Termination** (clause_002): Notice period: 30 days.

## Recommended Actions

1. File the summary with the contract record.
"""


def report_with(secret: str) -> str:
    return _REPORT + f"\nOperations note: {secret}\n"


class TestRefusalSetMatchesThePlatform:
    def test_every_platform_pattern_family_is_covered(self):
        """The gate's refusal set is a superset of the platform's, family by family."""
        covered = {name for name, _ in _PATTERNS}
        assert covered == set(
            PLATFORM_SHAPES
        ), "the platform's pattern families changed; update this test and re-check the gate"

    @pytest.mark.parametrize("family", sorted(PLATFORM_SHAPES))
    def test_a_platform_shape_is_refused(self, family):
        assert scan_for_disallowed_content(report_with(PLATFORM_SHAPES[family])) is not None

    @pytest.mark.parametrize("family", sorted(DOMAIN_SHAPES))
    def test_a_domain_shape_is_refused(self, family):
        """These are the families the platform detector does not see."""
        secret = DOMAIN_SHAPES[family]
        assert detect_credentials(secret) == []
        assert scan_for_disallowed_content(report_with(secret)) == family

    def test_a_clean_report_is_released(self):
        assert scan_for_disallowed_content(_REPORT) is None


class TestContainmentOnRefusal:
    @pytest.mark.parametrize("secret", sorted(PLATFORM_SHAPES.values()) + sorted(DOMAIN_SHAPES.values()))
    def test_every_report_field_is_cleared(self, secret):
        result = PostProcessNode().execute({"result": report_with(secret)})
        assert result["status"] == AgentStatus.ERROR.value
        for field in _OUTPUT_BEARING_FIELDS:
            assert field in result, f"{field} must be present in the update, not omitted"
            assert result[field] is None, f"{field} must be cleared, not left as it was"

    def test_the_refusal_notice_is_truthy(self):
        """A falsy notice re-activates the platform's fallback to `result`."""
        result = PostProcessNode().execute({"result": report_with(PLATFORM_SHAPES["aws_key"])})
        assert result["formatted_output"] == REFUSAL_NOTICE
        assert bool(result["formatted_output"]) is True

    def test_the_refusal_carries_no_report_text(self):
        marker = "Northwind Escrow Holdings"
        result = PostProcessNode().execute(
            {"result": report_with(PLATFORM_SHAPES["aws_key"]).replace("Company A Inc.", marker)}
        )
        assert marker not in repr(result)

    def test_the_refusal_names_the_field_not_the_value(self):
        secret = PLATFORM_SHAPES["aws_key"]
        result = PostProcessNode().execute({"result": report_with(secret)})
        joined = " ".join(result["error_log"])
        assert "result" in joined
        assert secret not in joined

    def test_the_refusal_update_matches_its_declared_inventory(self):
        """A field added to the state later must be added to the inventory deliberately."""
        result = PostProcessNode().execute({"result": report_with(PLATFORM_SHAPES["aws_key"])})
        assert set(result) == set(_OUTPUT_BEARING_FIELDS) | set(_INERT_REFUSAL_FIELDS)

    def test_an_empty_report_is_refused_with_a_truthy_notice(self):
        result = PostProcessNode().execute({"result": ""})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] is None
        assert bool(result["formatted_output"]) is True


class TestCleanPathControl:
    """A gate that refuses everything must not be able to pass PB-02."""

    def test_a_clean_report_is_returned_unchanged_apart_from_the_disclaimer(self):
        result = PostProcessNode().execute({"result": _REPORT})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _REPORT.strip() in result["result"]
        assert result["formatted_output"] is None

    def test_contract_text_that_merely_mentions_credentials_is_released(self):
        wording = _REPORT + "\nThe Client shall keep all passwords and access tokens confidential.\n"
        result = PostProcessNode().execute({"result": wording})
        assert result["status"] == AgentStatus.SUCCESS.value
