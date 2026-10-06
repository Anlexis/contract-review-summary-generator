# Contract Review Summary Agent

AI agent for summarizing contract reviews, built with Agentic Star.

> **Category**: Cat 2 (domain-specific document-generation pipeline)
> **Industry**: Services
> **Template ID**: SVC-C2-002

## Overview

Turns a contract into a structured review summary. You submit the contract text; the agent
splits it into clauses, categorises them, and returns a six-section report — parties, key terms,
obligations, risk flags, renewal and termination dates, and recommended actions — with a legal
disclaimer attached to every result.

Extraction and rendering are **deterministic**, and every item in the report cites the clause it
came from. That is the point of the design: a review summary is read by someone deciding whether
to sign, so it has to be checkable against the source text rather than merely readable. The agent
produces a first pass for a human reviewer; it does not give advice and does not decide.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | 3.11 or later |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode: the agent imports the framework at start-up, and without it start-up fails rather than the
agent coming up in a partially working state. That is intentional — a half-running agent is worse
than one that refuses to start.

This template has no stub or offline answer path either. A review summary that looks real and
describes no actual contract is exactly the failure a reader cannot detect.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/graph/     the outer graph, the inner workflow graph, and the bridge between them
src/nodes/     the pipeline steps and the two boundary gates
src/services/  the contract risk rules
src/schemas/   the graph state
src/api/       the HTTP entry point
tests/         unit and boundary tests
config/        agent.yaml (manifest) and config.yaml (runtime parameters)
docs/          design specification and test specification
```

Start with `docs/02_design.md` for how the pieces fit together, and `docs/03_test_spec.md`
for what the test suite pins.

## Customising

1. `config/config.yaml` — the runtime parameters. `review.risk_threshold` sets the severity floor
   for which risk indicators are listed; `parser.max_clauses` bounds how much of a long document
   is parsed. Both are validated at start-up and fall back to a built-in default if out of range.
2. `src/services/service.py` — the contract risk rules, as an explicit scored table. This is the
   part most worth adapting to your own portfolio: each rule carries the pattern it matches, a
   severity score, and the follow-up action it implies.
3. `src/nodes/` — the pipeline steps: parse, extract, render, and the two boundary gates.
4. Re-run the test suite. `docs/03_test_spec.md` explains what each file pins.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
