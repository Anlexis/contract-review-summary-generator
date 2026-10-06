# PB-03 — the mandatory legal disclaimer is present on every released report.
#
# The disclaimer is what tells a reader the report is not legal advice, so it is
# the template's central output invariant. Two independent places enforce it:
# the quality gate inside the workflow attaches it, and the output gate at the
# boundary re-attaches it if it is ever absent. Both are checked here, and the
# invariant is then checked once more through a real request.

import json


from framework.schemas.agent_status import AgentStatus
from src.nodes.disclaimer_quality_gate_node import (
    MANDATORY_DISCLAIMER,
    DisclaimerQualityGateNode,
)
from src.nodes.post_process_node import PostProcessNode
from tests.conftest import SAMPLE_CONTRACT, invoke

_COMPLETE_REPORT = """\
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


class TestDisclaimerAlwaysPresent:
    def test_the_workflow_gate_attaches_the_disclaimer(self):
        result = DisclaimerQualityGateNode().execute({"summary_raw": _COMPLETE_REPORT})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["summary_final"].startswith(MANDATORY_DISCLAIMER)

    def test_no_runtime_setting_can_switch_the_disclaimer_off(self):
        """The disclaimer is unconditional; nothing a caller or a config declares removes it."""
        for settings in (
            json.dumps({}),
            json.dumps({"risk_threshold": 0.0, "max_clauses": 1}),
            json.dumps({"confidentiality_notice": False}),
        ):
            result = DisclaimerQualityGateNode().execute(
                {"summary_raw": _COMPLETE_REPORT, "runtime_settings": settings}
            )
            assert MANDATORY_DISCLAIMER in result["summary_final"]

    def test_the_output_gate_re_attaches_a_missing_disclaimer(self):
        result = PostProcessNode().execute({"result": _COMPLETE_REPORT})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["result"].startswith(MANDATORY_DISCLAIMER)

    def test_the_output_gate_does_not_duplicate_a_present_disclaimer(self):
        already = f"{MANDATORY_DISCLAIMER}\n\n{_COMPLETE_REPORT}"
        result = PostProcessNode().execute({"result": already})
        assert result["result"].count(MANDATORY_DISCLAIMER) == 1

    def test_a_released_report_carries_the_disclaimer_end_to_end(self, authorized_client):
        body = invoke(authorized_client, SAMPLE_CONTRACT)
        assert body["status"] == AgentStatus.SUCCESS.value
        assert body["output"].startswith(MANDATORY_DISCLAIMER)
