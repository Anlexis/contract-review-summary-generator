# PB-03b — containment through a real request.
#
# The unit tests in test_pb02_output_gate.py prove what the gate RETURNS. This
# module proves what the CALLER RECEIVES, by driving the deployed entry point
# and reading the response envelope.
#
# Reachability, stated plainly because it decides what each test is worth:
#
#   * The DOMAIN pattern families (a credential assignment, a private-key
#     header) are reachable here. The platform's own scan does not know them, so
#     such a value travels all the way to this template's output gate, which is
#     what refuses it. These tests exercise the gate.
#
#   * The PLATFORM pattern families are NOT reachable through the document
#     channel. Two boundaries stop them earlier: the entry point screens the
#     structured channel before the graph runs, and if a value somehow appeared
#     later, the platform's own scan of a workflow node's result trips before the
#     output gate is reached. Those paths are covered below as well — the caller
#     must still receive a contained, actionable envelope — but the assertion
#     there is about the WORKFLOW-failure path, not about the output gate.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from tests.conftest import SAMPLE_CONTRACT, invoke
from tests.proof_of_boundary.test_pb02_output_gate import DOMAIN_SHAPES, PLATFORM_SHAPES

_PARTY_MARKER = "Northwind Escrow Holdings"


def contract_with(secret: str) -> str:
    """A contract whose payment clause carries `secret`, so it reaches the report.

    The payment clause is one the extraction step quotes, which is what puts the
    value on the report path rather than leaving it in an unread part of the
    document.
    """
    return SAMPLE_CONTRACT.replace("Company A Inc.", _PARTY_MARKER).replace(
        "Payment shall be made monthly at USD 10,000 within 30 days of invoice.",
        f"Payment shall be made monthly at USD 10,000 via the billing portal ({secret}).",
    )


class TestOutputGateReachableThroughInvoke:
    """The refusal arm this template's own patterns own."""

    @pytest.mark.parametrize("family", sorted(DOMAIN_SHAPES))
    def test_the_caller_receives_a_contained_refusal(self, authorized_client, family):
        body = invoke(authorized_client, contract_with(DOMAIN_SHAPES[family]))
        serialized = json.dumps(body)

        assert body["status"] == AgentStatus.ERROR.value
        assert DOMAIN_SHAPES[family] not in serialized, "the refused value must not be released"
        assert _PARTY_MARKER not in serialized, "no part of the report may be released"
        assert body["output"], "the envelope must carry a reason, not an empty value"
        assert "withheld" in body["output"]

    def test_the_block_happens_at_the_output_gate(self, authorized_client):
        """Proves the refusal came from the gate, not from something upstream."""
        body = invoke(authorized_client, contract_with(DOMAIN_SHAPES["credential_assignment"]))
        assert "PostProcessNode" in body["node_history"]

    def test_the_refusal_carries_no_traceback_or_source_path(self, authorized_client):
        serialized = json.dumps(invoke(authorized_client, contract_with(DOMAIN_SHAPES["private_key_block"])))
        assert "Traceback" not in serialized
        assert "site-packages" not in serialized
        assert "/src/" not in serialized


class TestPlatformShapesAreRefusedEarlier:
    """The same caller experience on the paths that never reach the output gate."""

    @pytest.mark.parametrize("family", sorted(PLATFORM_SHAPES))
    def test_the_entry_point_refuses_the_structured_channel(self, authorized_client, family):
        response = authorized_client.post(
            "/invoke",
            json={
                "input": "review",
                "input_context": {"document_text": contract_with(PLATFORM_SHAPES[family])},
            },
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "input_context.document_text" in detail
        assert PLATFORM_SHAPES[family] not in detail

    def test_a_workflow_failure_still_yields_a_contained_envelope(self, authorized_client, monkeypatch):
        """When the platform refuses a workflow node's own result, nothing partial is released.

        The value is introduced AFTER the entry point, on the data path, so the
        refusal under test is the platform's rather than this template's.
        """
        import src.nodes.summarize_node as summarize_node

        original = summarize_node.render_summary
        monkeypatch.setattr(
            summarize_node,
            "render_summary",
            lambda sections, reference=None: original(sections, reference)
            + f"\n\nOperations note: {PLATFORM_SHAPES['aws_key']}\n",
        )

        body = invoke(authorized_client, SAMPLE_CONTRACT)
        serialized = json.dumps(body)

        assert body["status"] == AgentStatus.ERROR.value
        assert PLATFORM_SHAPES["aws_key"] not in serialized
        assert "Company A Inc." not in serialized
        assert body["output"], "the envelope must carry a reason, not an empty value"
        assert "Traceback" not in serialized and "site-packages" not in serialized


class TestCleanPathControl:
    def test_the_same_request_without_a_secret_still_produces_its_report(self, authorized_client):
        body = invoke(authorized_client, contract_with("standard billing portal"))
        assert body["status"] == AgentStatus.SUCCESS.value
        assert _PARTY_MARKER in body["output"]
        assert "PostProcessNode" in body["node_history"]
