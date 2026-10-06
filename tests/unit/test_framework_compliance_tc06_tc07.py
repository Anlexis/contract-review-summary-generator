"""TC-06 / TC-07 — the platform's node security gates are not bypassable.

A domain node extends the default input and output gates only through the
documented injection hooks. Replacing a default gate outright is refused at
class definition time, before any instance exists, so the check below is a
compile-time property rather than a runtime one.
"""

import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class TestNodeGatesCannotBeReplaced:
    def test_tc06_the_input_gate_cannot_be_overridden(self):
        with pytest.raises(TypeError, match="_security_gate_input"):

            class _InvalidInputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_input(self, state):
                    return state

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}

    def test_tc07_the_output_gate_cannot_be_overridden(self):
        with pytest.raises(TypeError, match="_security_gate_output"):

            class _InvalidOutputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_output(self, result):
                    return result

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}
