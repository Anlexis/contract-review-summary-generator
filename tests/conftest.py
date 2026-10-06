"""Shared fixtures for the SVC-C2-002 test suite."""

import warnings
from typing import Any, Dict

import pytest

# fastapi.testclient's import of starlette.testclient warns about the installed
# httpx generation — an environment-side packaging note, not a behaviour of this
# template. Import it once here with the warning contained, so the suite's
# warning report stays about the code under test.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from src.api import server

# A short but realistic service agreement. It carries, on purpose: two defined
# parties, a payment term, an automatic-renewal clause, a one-sided liability
# cap, an indemnity, a sole-discretion termination right, a governing-law clause
# and two notice periods — so every extracted section has something real in it
# and the severity floor has findings on both sides of it to filter.
SAMPLE_CONTRACT = """SERVICE AGREEMENT

This Service Agreement is entered into as of January 1, 2025 by and between
Company A Inc., a corporation ("Service Provider") and Company B Ltd., a company ("Client").

Article 1. Services
Service Provider shall deliver consulting services as described in Schedule A.
Payment shall be made monthly at USD 10,000 within 30 days of invoice.

Article 2. Term and Renewal
This Agreement commences on January 1, 2025 and expires on December 31, 2025.
The Agreement shall automatically renew for successive one-year terms unless either
party gives 60 days written notice.

Article 3. Intellectual Property
All intellectual property created under this Agreement belongs to Client.

Article 4. Liability
Service Provider liability is limited to the fees paid in the prior 3 months.
Client shall indemnify Service Provider against third party claims.

Article 5. Termination
Client may terminate this Agreement at its sole discretion upon 30 days written notice.

Article 6. Governing Law
This Agreement is governed by the laws of Japan.
"""

# Bearer credential used by the entry-point tests. Not a real secret: it is
# generated per test session and only ever compared against itself.
TEST_INVOKE_TOKEN = "test-invoke-token-000000"


@pytest.fixture()
def contract_text() -> str:
    """The sample agreement the pipeline tests run against."""
    return SAMPLE_CONTRACT


@pytest.fixture()
def authorized_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An HTTP client for the entry point, holding a valid caller credential."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", TEST_INVOKE_TOKEN)
    client = TestClient(server.app)
    client.headers.update({"Authorization": f"Bearer {TEST_INVOKE_TOKEN}"})
    return client


@pytest.fixture()
def anonymous_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An HTTP client for the entry point with no caller credential."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", TEST_INVOKE_TOKEN)
    return TestClient(server.app)


def invoke(client: TestClient, document: str, **context: Any) -> Dict[str, Any]:
    """POST one review request and return the decoded response body."""
    payload: Dict[str, Any] = {
        "input": "contract review request",
        "input_context": {"document_text": document, **context},
    }
    return client.post("/invoke", json=payload).json()


__all__ = ["SAMPLE_CONTRACT", "TEST_INVOKE_TOKEN", "invoke"]
