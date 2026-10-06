# PB-08 — the public path does real work.
#
# Every assertion here goes through the deployed entry point, because that is
# where the properties under test can actually break: the caller credential
# boundary, the structured caller-data channel reaching the workflow across the
# graph boundary, and the declared runtime configuration reaching the nodes that
# consume it. Each of those has a shape that unit tests cannot see — a node
# called directly is handed a state that a real request never builds.


import pytest

from framework.schemas.agent_status import AgentStatus
from src.graph.graph import Graph, _runtime_config, declared_settings
from tests.conftest import SAMPLE_CONTRACT, TEST_INVOKE_TOKEN, invoke


class TestCallerCredentialBoundary:
    def test_a_request_without_a_credential_is_refused(self, anonymous_client):
        response = anonymous_client.post("/invoke", json={"input": SAMPLE_CONTRACT})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["", "Bearer", "Bearer wrong-token", f"Basic {TEST_INVOKE_TOKEN}", TEST_INVOKE_TOKEN],
    )
    def test_a_malformed_credential_is_refused(self, anonymous_client, header):
        response = anonymous_client.post("/invoke", json={"input": SAMPLE_CONTRACT}, headers={"Authorization": header})
        assert response.status_code == 401

    def test_the_refusal_does_not_say_why(self, anonymous_client):
        """One generic body, so a caller cannot tell absent from wrong."""
        absent = anonymous_client.post("/invoke", json={"input": SAMPLE_CONTRACT}).json()
        wrong = anonymous_client.post(
            "/invoke", json={"input": SAMPLE_CONTRACT}, headers={"Authorization": "Bearer nope"}
        ).json()
        assert absent == wrong

    def test_a_valid_credential_reaches_the_workflow(self, authorized_client):
        body = invoke(authorized_client, SAMPLE_CONTRACT)
        assert body["status"] == AgentStatus.SUCCESS.value
        assert body["node_history"] == [
            "InitializeNode",
            "PreProcessNode",
            "ContractReviewGraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ]


class TestCallerDataReachesTheWorkflow:
    def test_the_report_is_computed_from_the_submitted_document(self, authorized_client):
        body = invoke(authorized_client, SAMPLE_CONTRACT)
        assert body["status"] == AgentStatus.SUCCESS.value
        for fact in ("Company A Inc.", "Company B Ltd.", "Service Provider", "60 days"):
            assert fact in body["output"], f"{fact!r} came from the document and must appear"

    def test_a_different_document_produces_a_different_report(self, authorized_client):
        first = invoke(authorized_client, SAMPLE_CONTRACT)["output"]
        second = invoke(authorized_client, SAMPLE_CONTRACT.replace("Company A Inc.", "Northwind Consulting KK"))[
            "output"
        ]
        assert first != second
        assert "Northwind Consulting KK" in second

    def test_the_caller_reference_is_rendered_into_the_report(self, authorized_client):
        body = invoke(authorized_client, SAMPLE_CONTRACT, contract_reference="msa_2025_001")
        assert "msa_2025_001" in body["output"]

    def test_an_oversized_structured_channel_is_refused_at_the_entry_point(self, authorized_client):
        response = authorized_client.post(
            "/invoke",
            json={"input": "review", "input_context": {"document_text": "x" * 300_000}},
        )
        assert response.status_code == 413


class TestDeclaredConfigurationIsLive:
    """The shipped declaration must reach the nodes, not only sit in a file."""

    def test_the_shipped_file_parses_into_validated_settings(self):
        settings = declared_settings(_runtime_config())
        assert settings["max_clauses"] == 200
        assert settings["risk_threshold"] == 0.5

    def test_the_declared_severity_floor_changes_the_report(self):
        """Two deployments differing only in config.yaml produce different reports."""
        lenient = self._report({"review": {"risk_threshold": 0.0}})
        strict = self._report({"review": {"risk_threshold": 0.9}})
        assert lenient.count("- **[") > strict.count("- **[")
        assert "further indicator" in strict

    def test_the_declared_clause_cap_changes_the_report(self):
        capped = self._report({"parser": {"max_clauses": 2}})
        uncapped = self._report({"parser": {"max_clauses": 200}})
        assert len(set(_clause_refs(capped))) == 2
        assert len(set(_clause_refs(uncapped))) > 2

    def test_an_out_of_contract_declaration_falls_back_to_the_built_in_floor(self):
        """A non-finite or non-numeric declaration must not disable the filtering it configures."""
        broken = self._report({"review": {"risk_threshold": float("nan")}, "parser": {"max_clauses": "many"}})
        assert broken == self._report({})

    def test_a_caller_override_changes_the_report_within_the_declared_bounds(self, authorized_client):
        lenient = invoke(authorized_client, SAMPLE_CONTRACT, risk_threshold=0.0)["output"]
        strict = invoke(authorized_client, SAMPLE_CONTRACT, risk_threshold=0.9)["output"]
        assert lenient.count("- **[") > strict.count("- **[")

    @staticmethod
    def _report(config: dict) -> str:
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel

        agent = Graph(config=config)
        agent.compile()
        ctx = InvocationContext(session_id="pb08", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        body = agent.invoke("review", ctx=ctx, input_context={"document_text": SAMPLE_CONTRACT})
        assert body["status"] == AgentStatus.SUCCESS.value, body
        return str(body["output"])


def _clause_refs(report: str) -> list:
    import re

    return re.findall(r"clause_\d+", report)
