# Design Specification — SVC-C2-002

## Identity

- **Template ID**: SVC-C2-002
- **Name**: Contract Review Summary Generator
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
- **Category**: Cat 2 (nested two-layer architecture)
- **Industry**: SVC (professional and business services)
- **Pattern**: document generation
- **Generation mode**: deterministic

## What it does

A caller submits a contract document. The agent parses it into clause blocks,
categorizes those blocks into five extraction sections, renders a six-section
review report from them, and releases the report with a mandatory legal
disclaimer attached.

Extraction and rendering are **deterministic**: the same document always
produces the same report. That is a deliberate choice for this domain. A review
summary is read by someone deciding whether to sign, so the value of the output
depends on being able to trace each statement back to the clause it came from —
every extracted item in the report carries its clause reference.

## Architecture

### Two-layer nested structure

```
Outer backbone (AgentBaseGraph — fixed; add_edges is never overridden):
  START
    -> initialize          [InitializeNode — framework default]
    -> pre_process         [PreProcessNode — the caller-contract gate]
    -> main                [ContractReviewGraphNode — GraphNode subclass]
         |
         +- Inner graph (DomainWorkflowGraph — BaseGraph):
              START
                -> contract_parse           [ContractParseNode]
                -> clause_extraction        [ClauseExtractionNode]
                -> summarize                [SummarizeNode]
                -> disclaimer_quality_gate  [DisclaimerQualityGateNode]
              END
         |
    -> post_process        [PostProcessNode — the output gate]
    -> finalize            [FinalizeNode — framework default]
  END
```

### Layer responsibilities

| Layer | Component | Responsibility |
|---|---|---|
| Outer backbone | `SvcC2002Agent` | The fixed five-slot backbone; validates the declared runtime settings once and hands them on |
| Main slot | `ContractReviewGraphNode` | Wraps the inner graph, bridges the validated document across the graph boundary, maps the report back, and contains an inner failure |
| Inner graph | `DomainWorkflowGraph` | Owns the four-step review pipeline |
| Inner nodes | `ContractParseNode`, `ClauseExtractionNode`, `SummarizeNode`, `DisclaimerQualityGateNode` | One processing step each |
| Boundary nodes | `PreProcessNode`, `PostProcessNode` | The caller-contract gate and the output gate — not duplicated inside the inner graph |

### Backbone slots

| Node | Slot | Responsibility | Reads | Writes |
|---|---|---|---|---|
| `InitializeNode` | `initialize` | Framework default | — | session_id, schema_version, caller_trust_level |
| `PreProcessNode` | `pre_process` | Validate the caller-data contract (a correctable breach completes carrying `error_code`); screen for injection (terminates) | user_input, input_context, runtime_settings | document_text, contract_reference, runtime_settings, validated_input |
| `ContractReviewGraphNode` | `main` | Delegate to the inner graph | document_text, runtime_settings | summary_final, result, review_metadata |
| `PostProcessNode` | `post_process` | Scan and release, or refuse and clear; on a run declined upstream, render that reason's fixed sentence as the body and complete | result | result, formatted_output, review_metadata |
| `FinalizeNode` | `finalize` | Framework default | — | response_metadata, total_time_ms |

### Inner nodes

| Node | Inner slot | Responsibility | Reads | Writes |
|---|---|---|---|---|
| `ContractParseNode` | `contract_parse` | Normalize the text and split it into clause blocks | document_text, runtime_settings | contract_text, parsed_clauses |
| `ClauseExtractionNode` | `clause_extraction` | Categorize clauses into the five sections | parsed_clauses, contract_text, runtime_settings | extracted_sections |
| `SummarizeNode` | `summarize` | Render the six-section report | extracted_sections, contract_reference | summary_raw |
| `DisclaimerQualityGateNode` | `disclaimer_quality_gate` | Check the sections; attach the disclaimer | summary_raw, extracted_sections | summary_final, review_metadata |

### Crossing the graph boundary

`GraphNode.execute()` invokes the inner graph as
`subgraph.invoke(user_input, session_id=..., ctx=...)`. **No other outer state
field is forwarded.** An inner node reading `state["document_text"]` would
therefore see nothing on a real request, however well the outer graph populated
it — and the unit tests would not notice, because a node called directly is
handed a state a real request never builds.

`src/graph/context_bridge.py` closes that gap through the two sanctioned hooks:
`extract_input()` stashes the validated document and settings just before the
inner invoke, and the inner graph's `_extra_initial_state()` seeds them into the
inner initial state. A ContextVar carries the hand-off, so concurrent requests
in one process cannot see each other's document.

## State (`src/schemas/state.py`)

`State(AgentState)` — a flat TypedDict. Every field is optional.

| Field | Type | Written by | Description |
|---|---|---|---|
| `document_text` | `Optional[str]` | PreProcessNode | The validated contract document |
| `contract_reference` | `Optional[str]` | PreProcessNode | Caller label echoed into the report; inert identifier |
| `runtime_settings` | `Optional[str]` | SvcC2002Agent / PreProcessNode | Validated settings, JSON-encoded |
| `validated_input` | `Optional[str]` | PreProcessNode | Framework-visible copy of the document |
| `result` | `Optional[str]` | merge_output, PostProcessNode | The report surfaced to the caller |
| `formatted_output` | `Optional[str]` | PreProcessNode, main slot, PostProcessNode | Truthy refusal notice; see *Containment* below |
| `contract_text` | `Optional[str]` | ContractParseNode | Normalized document text |
| `parsed_clauses` | `Optional[List[Dict]]` | ContractParseNode | Clause blocks |
| `extracted_sections` | `Optional[Dict]` | ClauseExtractionNode | parties, key_terms, obligations, risk_flags, renewal_termination_dates |
| `summary_raw` | `Optional[str]` | SummarizeNode | The rendered report before the quality gate |
| `summary_final` | `Optional[str]` | DisclaimerQualityGateNode | The report with the disclaimer attached |
| `review_metadata` | `Optional[Dict]` | DisclaimerQualityGateNode | clause_count, risk_flag_count, renewal_dates_found, risk_flags_suppressed |
| `trace_id`, `correlation_id` | `Optional[str]` | Framework | Tracing identifiers |

Inherited from `AgentState`: `user_input`, `input_context`, `status`,
`session_id`, `node_history`, `error_log`, `response_metadata`, `total_time_ms`.

## Configuration

Two files, with different jobs.

**`config/agent.yaml`** is the static manifest the registry reads for
discovery: identity, category, entry point, the required trust level, and the
compile-time declarations (`requires.secrets`, `requires.extras`). This agent
requires neither, so both are empty — declaring an unprovisioned secret would
make the agent fail at compile time.

**`config/config.yaml`** carries the runtime parameters. The registry loads it
and passes it as `Graph(config=...)`; the standalone entry point reads the same
file through `_runtime_config()`, so both deployments run on one declaration.

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_retry` | int | 3 | Backbone retry budget |
| `timeout_s` | int | 30 | Request timeout |
| `parser.max_clauses` | int | 200 | Upper bound on clause blocks parsed from one document |
| `review.risk_threshold` | float | 0.5 | Severity floor in 0..1; a finding below it is counted but not listed |

Each value is validated once, in `declared_settings()`: a value that is not a
finite number in range falls back to the built-in floor rather than propagating.
That check matters even for a value an operator wrote: `NaN` compares False
against every bound, so a NaN threshold would silently disable the filtering it
configures rather than fail.

The settings then travel to their consumers — the backbone reads `max_retry`
itself, and the inner pipeline receives the rest through the main slot's
`_parent_config()`, republished into inner state by `_extra_initial_state()`.
Nothing reads a second copy of the defaults from disk.

## The caller-data contract

`POST /invoke` accepts `input`, `session_id` and `input_context`. All
`input_context` fields are optional and each is bounded:

| Field | Type | Bounds |
|---|---|---|
| `document_text` | str | 10 .. 200,000 characters after normalization |
| `contract_reference` | str | `[a-z0-9_]`, 1 .. 32 characters |
| `risk_threshold` | number | finite, 0.0 .. 1.0 |
| `max_clauses` | integer | finite, 1 .. 2000 |

When `input_context` carries no document, `user_input` is accepted as the
document instead, so a plain text request still works.

Rules the gate enforces:

- **An undeclared field is refused, not ignored.** Ignoring is not stripping: an
  undeclared key stays on `input_context`, is returned verbatim by the first
  node, and is scanned there — so silently ignoring it converts a clear refusal
  into an opaque failure deeper in the run.
- **Every caller number goes through a finite, bounded parser.** Bools are
  rejected explicitly, and `"NaN"` / `"Infinity"` are rejected as strings too,
  because they arrive that way through JSON and `float()` accepts them.
- **A caller string that renders into the report is locked to an inert
  identifier.** Free text on that channel is caller-controlled output injection.
- **A refusal names the field, never the value.** The field path goes to
  `error_log`, the internal audit channel.
- **A breach the caller can correct stops the request without ending the
  conversation.** An absent, too-short or too-long document, an undeclared
  field, a non-inert label, an out-of-bounds number — the run **completes**
  (`status = AgentStatus.SUCCESS.value`) carrying a reason code in `error_code`,
  and the caller-facing body is the fixed sentence for that code. The rejection
  itself is unchanged: the document is never published, the inner workflow never
  runs, and no report is produced. Terminating instead would end the calling
  surface's turn and surface only an exception type, leaving the reason
  reachable solely from the audit trail — the caller could not correct the value
  and resend on the same conversation. The two **injection screens** are the
  exception: they **terminate** (`status = AgentStatus.ERROR.value`), because
  refused content is not a value to correct and reporting it the same way would
  read as an invitation to reword the request until it gets through. The branch
  is chosen by the call site (`_reject(..., code=...)`), never by reading the
  message text.
- **The reason code is internal.** It marks State so the main slot skips the
  inner graph entirely, it crosses the inner/outer boundary with the outer
  reason winning, and it is never surfaced in the response envelope — the caller
  reads the sentence, not the code.

## Security

### The trust boundary

`PreProcessNode` declares `required_trust_level = VERIFIED_EXTERNAL` and is the
template's single trust boundary. The caller's level travels into the inner
graph unchanged, so the workflow nodes declare the permissive level: a stricter
declaration there could never be satisfied and would refuse every request rather
than add a check. `tests/proof_of_boundary/test_pb_invoke_order.py` asserts that
arrangement so it cannot be changed silently.

`PostProcessNode` is deliberately permissive for the same reason turned around:
the trust check runs *before* `execute()`, so a stricter declaration there would
skip the output scan for exactly the callers it matters most for.

The standalone entry point turns `INVOKE_AUTH_TOKEN` into that trust level: a
caller no upstream middleware vouched for must present it as a bearer token, and
then runs as VERIFIED_EXTERNAL. Without it every request arrives anonymous and
is refused at the gate. Trust established by middleware is never demoted.

### Input screening

Two families, both screened, on the raw text and again after markup is stripped:

- **Directive phrases** — instruction overrides, persona overrides, prompt
  probes. Both ends are anchored so ordinary contract language ("the parties
  shall act as a single entity") does not trip them.
- **Chat-template control tokens** — `<|...|>`, `[INST]`, `<<SYS>>`. An attack
  need not use a phrase at all, and a phrase list does not see these.

Both screens **terminate** the run, unlike the bounds checks around them: this
is the refusal a reworded request must not get past. Screening happens twice
because stripping markup can splice a directive back together out of fragments a
raw scan would not match, while control tokens only exist before the strip. The screen walks the parsed payload depth-first,
including keys, so a `\u`-escaped payload is seen after parsing rather than
before.

### The output boundary

`PostProcessNode` runs two independent checks, each with its own audit event:

1. **Credential scan.** The platform's own detector is called, widened by this
   template's domain patterns — never narrowed. A narrower local set is not
   merely a missed catch: the platform's own scan then refuses the node's result
   one layer later, and that refusal discards the node's entire state update.
2. **Disclaimer invariant.** Every released report carries the mandatory legal
   disclaimer, re-attached here if it is somehow absent.

The entry point additionally screens `input_context` for credential shapes
before the graph runs, and refuses with 400 naming the field. This is not
belt-and-braces: the first node returns `input_context` verbatim inside its own
result, so a credential-shaped value there fails the run at node one with an
error the caller cannot act on. The request cannot succeed either way, so a
readable refusal is strictly better.

### Containment

The framework resolves an agent's output as `formatted_output or result`, with
**no status check**. Returning an error status is therefore not a refusal by
itself, and a *falsy* `formatted_output` re-arms the very fallback a refusal
exists to prevent.

Every path in this template that ends without a report therefore does two
things: it clears the fields that carry report text, and it writes a **truthy**
notice.

| Path | Where | What it clears |
|---|---|---|
| Caller contract declined (completes) | `PreProcessNode._reject` with a code | document_text, validated_input, result |
| Caller content refused (terminates) | `PreProcessNode._reject` with no code | document_text, validated_input, result |
| Workflow failed | `ContractReviewGraphNode.on_subgraph_error` | summary_final, result, review_metadata |
| Output refused | `PostProcessNode._refusal` | result, summary_final |

The main slot uses `error_strategy = "handle"` rather than the framework
default: re-raising loses containment, because the node wrapper turns the
exception into a bare error update that clears nothing and carries a traceback,
and the caller receives an empty envelope with no reason in it.

Refusal messages name field paths only. Quoting the matched text would put the
value back into a returned result, where the framework's output gate would
refuse it and discard the clearing along with it.

### Output invariant, and what is not enforced

The report's stated invariants are: all six sections present, the disclaimer
attached, no credential-shaped content, and caller-supplied labels inert.
`DisclaimerQualityGateNode` matches section headings at the start of a line, and
every excerpt copied out of the contract is collapsed to a single line and
rendered as list-item body text — so a contract containing the report's own
heading text is quoted back (correctly) without being able to become a section.

**A monetary rounding grid is not applicable here.** The report renders no
monetary aggregate of its own: figures appear only inside excerpts quoted
verbatim from the submitted document, where altering them would misrepresent the
contract under review. A precision gate would be actively wrong for this domain.

## Behaviour without the platform

The agent imports the framework at start-up and does not run standalone. There
is no offline mode and no stub fallback: a pipeline that answered from canned
content would be worse than one that refuses, because a review summary that
looks real and describes no actual contract is exactly the failure a reader
cannot detect.
