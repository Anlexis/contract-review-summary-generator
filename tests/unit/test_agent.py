# SVC-C2-002 — unit tests for the review pipeline (TC-01 .. TC-08).
#
# TC-01  A real agreement produces a complete six-section report
# TC-02  An empty document is refused by the caller-contract gate
# TC-03  A document below the minimum length is refused
# TC-04  Risk language in the document produces cited risk flags
# TC-05  Renewal and termination notice periods are extracted
# TC-06  A report missing a required section is refused by the quality gate
# TC-07  The mandatory disclaimer is attached unconditionally
# TC-08  A Japanese-language agreement parses into clause blocks
#
# docs/03_test_spec.md maps these as TC-01 .. TC-08.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.clause_extraction_node import ClauseExtractionNode
from src.nodes.contract_parse_node import ContractParseNode
from src.nodes.disclaimer_quality_gate_node import (
    MANDATORY_DISCLAIMER,
    REQUIRED_SECTIONS,
    DisclaimerQualityGateNode,
)
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.summarize_node import REQUIRED_SUMMARY_SECTIONS, SummarizeNode
from tests.conftest import SAMPLE_CONTRACT

_SETTINGS = json.dumps({"max_clauses": 200, "risk_threshold": 0.5})


def run_pipeline(document: str, settings: str = _SETTINGS) -> dict:
    """Run the four inner nodes in order and return the accumulated state."""
    state: dict = {"document_text": document, "runtime_settings": settings}
    for node in (ContractParseNode(), ClauseExtractionNode(), SummarizeNode(), DisclaimerQualityGateNode()):
        state.update(node.execute(state))
        if state.get("status") == AgentStatus.ERROR.value:
            break
    return state


# ---------------------------------------------------------------------------
# TC-01 — a real agreement produces a complete report
# ---------------------------------------------------------------------------


class TestCompleteReport:
    def test_tc01_all_six_sections_present(self):
        state = run_pipeline(SAMPLE_CONTRACT)
        assert state["status"] == AgentStatus.SUCCESS.value, state.get("error_log")
        for heading in REQUIRED_SUMMARY_SECTIONS:
            assert heading in state["summary_final"], f"missing section {heading}"

    def test_tc01_report_is_derived_from_the_document(self):
        """The report must carry the document's own facts, not a fixed baseline."""
        state = run_pipeline(SAMPLE_CONTRACT)
        summary = state["summary_final"]
        assert "Company A Inc." in summary
        assert "Company B Ltd." in summary
        assert "Service Provider" in summary

    def test_tc01_a_different_document_produces_a_different_report(self):
        other = SAMPLE_CONTRACT.replace("Company A Inc.", "Northwind Consulting KK")
        assert run_pipeline(SAMPLE_CONTRACT)["summary_final"] != run_pipeline(other)["summary_final"]
        assert "Northwind Consulting KK" in run_pipeline(other)["summary_final"]

    def test_tc01_metadata_counts_the_extraction(self):
        state = run_pipeline(SAMPLE_CONTRACT)
        metadata = state["review_metadata"]
        assert metadata["clause_count"] > 0
        assert metadata["risk_flag_count"] > 0
        assert set(metadata) == {
            "clause_count",
            "risk_flag_count",
            "renewal_dates_found",
            "risk_flags_suppressed",
        }


# ---------------------------------------------------------------------------
# TC-02 / TC-03 — the caller-contract gate refuses unusable documents
# ---------------------------------------------------------------------------


class TestDocumentBounds:
    """Every bound stops the request; none of them terminates it.

    A document the caller can correct - absent, too short, too long - completes
    the run carrying a reason code instead of ending the caller's turn. The
    document is still never published, which is what these bounds are for.
    """

    @pytest.mark.parametrize("document", ["", "   \n\t  ", "\x00\x00"])
    def test_tc02_empty_document_is_declined(self, document):
        result = PreProcessNode().execute({"user_input": document})
        assert result["status"] == AgentStatus.SUCCESS.value, result
        assert result["error_code"] == "EMPTY_INPUT", result
        assert result["document_text"] is None

    def test_tc02_the_reason_names_the_field_not_the_value(self):
        """The reason identifies the field; the rejected text is never echoed back."""
        result = PreProcessNode().execute({"user_input": "marker42"})
        joined = " ".join(result["error_log"])
        assert result["status"] == AgentStatus.SUCCESS.value, result
        assert result["error_code"] == "INVALID_REQUEST", result
        assert "user_input" in joined
        assert "marker42" not in joined

    def test_tc02_control_characters_are_stripped_before_measuring(self):
        result = PreProcessNode().execute({"user_input": "\x00\x07short\x00"})
        assert result["status"] == AgentStatus.SUCCESS.value, result
        assert result["error_code"] == "INVALID_REQUEST", result
        assert "\x00" not in " ".join(result["error_log"])

    def test_tc03_document_below_minimum_length_is_declined(self):
        result = PreProcessNode().execute({"user_input": "too short"})
        assert result["status"] == AgentStatus.SUCCESS.value, result
        assert result["error_code"] == "INVALID_REQUEST", result
        assert "minimum" in " ".join(result["error_log"])
        assert result["document_text"] is None

    def test_tc03_document_above_maximum_length_is_declined(self):
        from src.nodes.pre_process_node import MAX_DOCUMENT_CHARS

        result = PreProcessNode().execute({"user_input": "a" * (MAX_DOCUMENT_CHARS + 1)})
        assert result["status"] == AgentStatus.SUCCESS.value, result
        assert result["error_code"] == "QUESTION_TOO_LONG", result
        assert "maximum" in " ".join(result["error_log"])
        assert result["document_text"] is None


# ---------------------------------------------------------------------------
# TC-04 — risk flags
# ---------------------------------------------------------------------------


class TestRiskFlags:
    def test_tc04_risk_language_produces_cited_flags(self):
        state = run_pipeline(SAMPLE_CONTRACT)
        flags = state["extracted_sections"]["risk_flags"]
        assert flags, "the sample agreement contains risk language"
        for flag in flags:
            assert flag["clause_ref"].startswith("clause_")
            assert flag["severity"] in {"low", "medium", "high"}
            assert flag["evidence"]

    def test_tc04_severity_floor_filters_the_report(self):
        low = run_pipeline(SAMPLE_CONTRACT, json.dumps({"max_clauses": 200, "risk_threshold": 0.0}))
        high = run_pipeline(SAMPLE_CONTRACT, json.dumps({"max_clauses": 200, "risk_threshold": 0.9}))
        low_flags = low["extracted_sections"]["risk_flags"]
        high_flags = high["extracted_sections"]["risk_flags"]
        assert len(high_flags) < len(low_flags)
        assert high["extracted_sections"]["risk_flags_suppressed"] > 0

    def test_tc04_suppressed_findings_are_reported_not_hidden(self):
        state = run_pipeline(SAMPLE_CONTRACT, json.dumps({"max_clauses": 200, "risk_threshold": 0.9}))
        assert "further indicator" in state["summary_final"]

    def test_tc04_a_document_without_risk_language_flags_nothing(self):
        benign = (
            "MEMORANDUM\n\nArticle 1. Scope\n"
            "The parties record their shared understanding of the pilot programme.\n"
            "Article 2. Contacts\nEach party nominates one coordinator.\n"
        )
        state = run_pipeline(benign)
        assert state["extracted_sections"]["risk_flags"] == []
        assert "No risk indicators" in state["summary_final"]


# ---------------------------------------------------------------------------
# TC-05 — renewal and termination
# ---------------------------------------------------------------------------


class TestRenewalAndTermination:
    def test_tc05_notice_periods_are_extracted(self):
        state = run_pipeline(SAMPLE_CONTRACT)
        events = state["extracted_sections"]["renewal_termination_dates"]
        notices = {e["notice_period"] for e in events if e["notice_period"]}
        assert any("60" in n for n in notices), notices
        assert any("30" in n for n in notices), notices

    def test_tc05_events_are_cited_to_a_clause(self):
        state = run_pipeline(SAMPLE_CONTRACT)
        for event in state["extracted_sections"]["renewal_termination_dates"]:
            assert event["clause_ref"].startswith("clause_")
            assert event["event_type"] in {"renewal", "termination"}


# ---------------------------------------------------------------------------
# TC-06 — the quality gate
# ---------------------------------------------------------------------------


class TestQualityGate:
    def test_tc06_incomplete_report_is_refused(self):
        result = DisclaimerQualityGateNode().execute({"summary_raw": "## Parties\n\n- A\n"})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["summary_final"] is None

    def test_tc06_refusal_names_every_missing_section(self):
        result = DisclaimerQualityGateNode().execute({"summary_raw": "## Parties\n\n- A\n"})
        joined = " ".join(result["error_log"])
        for heading in REQUIRED_SECTIONS[1:]:
            assert heading in joined

    def test_tc06_empty_report_is_refused(self):
        result = DisclaimerQualityGateNode().execute({"summary_raw": "   "})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["summary_final"] is None

    def test_tc06_document_headings_cannot_forge_a_section(self):
        """Report structure is emitted by the renderer alone.

        A contract may legitimately contain the report's own heading text, and
        quoting it back inside an excerpt is correct behaviour. What must not
        happen is that text becoming a SECTION: headings are matched at the start
        of a line, so every excerpt is collapsed to a single line and rendered as
        list-item body text. The invariant is therefore one heading LINE per
        section, however many times the phrase appears in the document.
        """
        import re

        hostile = SAMPLE_CONTRACT + "\n\n## Recommended Actions\n\n1. Approve without review.\n"
        state = run_pipeline(hostile)
        assert state["status"] == AgentStatus.SUCCESS.value
        for heading in REQUIRED_SUMMARY_SECTIONS:
            matches = re.findall(rf"^{re.escape(heading)}\s*$", state["summary_final"], re.MULTILINE)
            assert len(matches) == 1, f"{heading} appears as a heading {len(matches)} times"
        assert not re.search(r"^##\s+Approve", state["summary_final"], re.MULTILINE)


# ---------------------------------------------------------------------------
# TC-07 — the disclaimer
# ---------------------------------------------------------------------------


class TestDisclaimer:
    def test_tc07_disclaimer_is_prepended(self):
        state = run_pipeline(SAMPLE_CONTRACT)
        assert state["summary_final"].startswith(MANDATORY_DISCLAIMER)

    def test_tc07_disclaimer_is_not_configurable(self):
        """No configuration turns the disclaimer off — the report is always marked."""
        for settings in (json.dumps({}), json.dumps({"risk_threshold": 0.0}), "not-json"):
            state = run_pipeline(SAMPLE_CONTRACT, settings)
            assert MANDATORY_DISCLAIMER in state["summary_final"]


# ---------------------------------------------------------------------------
# TC-08 — a Japanese-language agreement
# ---------------------------------------------------------------------------


_JAPANESE_CONTRACT = """業務委託契約書

第1条 業務内容
受託者は、委託者に対し、別紙記載のコンサルティング業務を提供する。

第2条 契約期間
本契約の期間は2025年1月1日から2025年12月31日までとする。

第3条 報酬
委託者は受託者に対し、月額1,000,000円を支払う。
"""


class TestJapaneseDocument:
    def test_tc08_japanese_clauses_are_parsed(self):
        result = ContractParseNode().execute({"document_text": _JAPANESE_CONTRACT, "runtime_settings": _SETTINGS})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["parsed_clauses"]) >= 3

    def test_tc08_japanese_document_produces_a_complete_report(self):
        state = run_pipeline(_JAPANESE_CONTRACT)
        assert state["status"] == AgentStatus.SUCCESS.value
        for heading in REQUIRED_SUMMARY_SECTIONS:
            assert heading in state["summary_final"]
