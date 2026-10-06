"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no domain logic here. For platform-level
# routing, the gateway calls agent.invoke() directly and this module is unused.

import json
import os
import re
import secrets
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from shared.secrets import factory as secrets_factory
from src.graph.graph import Graph, _runtime_config

app = FastAPI(title="ContractReviewSummaryAgent")

# The registry loads config/config.yaml and passes it as Graph(config=...); this
# entry point does the same, so the declared runtime parameters (max_retry, the
# clause cap and the risk-severity floor) are live in both deployments rather
# than only in the one the registry starts.
agent = Graph(config=_runtime_config())
agent.compile()
# Namespace and agent name match the manifest values in config/agent.yaml.
agent.provision_secrets(secrets_factory(namespace="svc", agent_name="ContractReviewSummaryAgent"))

# Coarse upper bound on the serialized caller-data channel, in bytes. The
# pre_process node enforces the per-field bounds; this guard keeps an oversized
# payload from reaching the graph at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144

# Field names are caller data too: one is echoed only when it is inert and trips
# no credential pattern of its own.
_SAFE_FIELD_NAME_RE = re.compile(r"\A[A-Za-z0-9_.\-]{1,40}\Z")


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller data: the contract document, the report label and the
    # per-request parameter overrides. Each field is validated inside the graph
    # (see src/nodes/pre_process_node.py).
    input_context: Optional[Dict[str, Any]] = None


def _describe_field(name: str, position: int) -> str:
    """Name a caller field safely, or refer to it by position."""
    if _SAFE_FIELD_NAME_RE.match(name) and not detect_credentials_in_value(name):
        return f"input_context.{name}"
    return f"input_context field #{position}"


def _screen_input_context(context: Dict[str, Any]) -> Optional[str]:
    """Return the field path of the first credential-shaped value, or None.

    The platform's first node returns `input_context` verbatim inside its own
    result, and the platform's output gate scans every value of every result —
    so a credential-shaped string anywhere on this channel fails the run at the
    first node, before any of this template's code runs, with an error the
    caller cannot act on. The request cannot succeed either way, so it is
    refused here instead, with a message that names the field.

    The platform's own detector is called, and called per top-level field. Its
    whole-mapping form is defined as the union over the mapping's values, so
    scanning field by field blocks exactly the same set — which is what allows
    the field to be named without widening or narrowing the refusal.
    """
    for position, (name, value) in enumerate(context.items(), start=1):
        if detect_credentials_in_value(value):
            return _describe_field(str(name), position)
    return None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone-deployment caller auth: when INVOKE_AUTH_TOKEN is set on the
    # server environment, a caller that no upstream middleware vouched for
    # (still ANONYMOUS) must present it as a bearer token, and then runs at
    # VERIFIED_EXTERNAL. Trust established by middleware is never demoted.
    #
    # Required here specifically: PreProcessNode, the caller-contract gate,
    # declares required_trust_level = VERIFIED_EXTERNAL. Nothing else sets
    # request.state.trust_level in a standalone deployment, so without this
    # boundary every request arrives ANONYMOUS, the trust gate refuses it, and
    # the agent returns an error for every input.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of a clean 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not reveal whether the token was
            # absent, malformed or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    input_context = req.input_context or {}
    if input_context:
        # Size is checked on the serialized form, before any field is read: the
        # per-field bounds inside the graph cannot bound a payload that is large
        # because of how MANY fields it carries.
        if len(json.dumps(input_context, default=str).encode("utf-8")) > _MAX_INPUT_CONTEXT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"input_context must be {_MAX_INPUT_CONTEXT_BYTES} bytes or fewer when serialized.",
            )
        offending = _screen_input_context(input_context)
        if offending:
            # 400, not 422: the validation layer owns 422 and answers with a
            # list of error objects there, so reusing it makes client handling
            # ambiguous. The field is named; the value never is.
            raise HTTPException(
                status_code=400,
                detail=f"{offending} looks like a credential and cannot be accepted.",
            )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "ContractReviewSummaryAgent"}
