# Test Specification — SVC-C2-002

## Strategy

Two layers, with different jobs.

**Unit tests** exercise the pipeline nodes and the caller-data contract by
calling `execute()` directly, with no framework wrapper in front. That is
deliberate for the input contract: a test that asserts "the framework refused
this" passes only where the framework's own gate happens to be active, and fails
open where it is not. These tests prove the *template* refuses hostile input.

**Two outcomes, asserted apart.** A breach the caller can correct — a document
outside its bounds, an undeclared field, a non-inert label, an out-of-bounds
number — is *declined*: the run completes (`status=SUCCESS`) carrying a reason
code in `error_code`, and the test asserts BOTH halves, because the status alone
would also pass on a run that quietly produced a report. Injection content is
*refused*: the run terminates (`status=ERROR`). The unit suites keep the two in
separate helpers (`declined()` / `refused()`) rather than one predicate, and in
both cases nothing is published and the rejected value is never echoed. Below,
"declined" and "refused" are used in exactly that sense.

**Proof-of-boundary tests** drive the deployed entry point. Some properties are
only visible from out there: the framework does not forward the caller-data
channel into a subgraph, and a runtime setting read from the wrong place
degrades silently to a default that happens to match. A node called directly is
handed a state a real request never builds, so a node-level test cannot see
either failure.

The pipeline is deterministic and makes no network call, so every test runs
offline and produces the same result each time.

## Unit tests

### `tests/unit/test_agent.py` — the review pipeline (TC-01 .. TC-08)

| TC | What it pins |
|---|---|
| TC-01 | A real agreement produces all six sections; the report carries the document's own facts; a different document produces a different report; the metadata counts the extraction |
| TC-02 | An empty, blank or control-character-only document is declined with `status=SUCCESS` + `error_code=EMPTY_INPUT` (control characters are stripped before the emptiness is measured); the reason names the field in `error_log` and never echoes the value; no document is published |
| TC-03 | A document below the minimum is declined with `error_code=INVALID_REQUEST`, above the maximum with `error_code=QUESTION_TOO_LONG`; both complete, and neither publishes a document |
| TC-04 | Risk language produces cited flags; raising the severity floor shortens the list; the suppressed count is reported rather than hidden; a document with no risk language flags nothing |
| TC-05 | Renewal and termination notice periods are extracted and cited to a clause |
| TC-06 | A report missing a required section is refused, naming every missing heading; a contract containing the report's own heading text cannot forge a section |
| TC-07 | The disclaimer is attached, and no runtime setting switches it off |
| TC-08 | A Japanese-language agreement parses into clause blocks and produces a complete report |

### `tests/unit/test_caller_contract.py` — the caller-data contract

| Group | What it pins |
|---|---|
| Accepted requests | The structured channel and a plain text request both work; declared settings survive a request that overrides nothing; a caller may narrow within the declared bounds |
| Numeric contract | Every numeric field declines `NaN`, `Infinity`, `-Infinity` as both strings and floats, bools, `None`, non-numerics, out-of-range magnitudes and (for the clause cap) fractional values — parametrized per field; a rejected number never appears in the reason |
| Rendered identifiers | The caller label accepts only `[a-z0-9_]{1,32}`; uppercase, spaces, markup, over-length and non-string values are declined |
| Injection screen | Chat-template control tokens and directive phrases are refused; a directive spliced with markup is refused after the strip re-assembles it; a hostile field name is masked rather than echoed; a `\u`-escaped payload is caught after parsing; six sentences of ordinary contract language using the same words are NOT refused; a refusal never quotes the matched text |
| Channel shape | An undeclared field is declined rather than ignored; a hostile undeclared field name is masked; a non-mapping context is declined; document bounds hold on the structured channel |

### `tests/unit/test_framework_compliance_tc06_tc07.py`

TC-06 / TC-07: replacing either default node security gate is refused at class
definition time.

## Proof-of-boundary tests

| PB | File | What it pins |
|---|---|---|
| PB-01 | `test_pb01_caller_input_screen.py` | Each attack family is refused through a real request (`status=error`, a body that tells the caller something); the refusal carries no document text, no traceback and no source path; non-finite parameters and undeclared fields are declined instead — envelope `status=success` with `output` equal to the fixed reason sentence and no summary anywhere in it. Clean-path control: ordinary contract language still succeeds and returns its report |
| PB-02 | `test_pb02_output_gate.py` | The gate's refusal set covers every platform pattern family (checked family by family against the platform's own list, so a new family upstream fails the test rather than passing silently) plus this template's domain families; on refusal every report field is **present and cleared** in the returned update, and the notice is truthy; the refusal names the field, not the value; the update matches its declared inventory. Clean-path control: a clean report is released, and contract text that merely mentions credentials is not refused |
| PB-03 | `test_disclaimer_always_present.py` | Both enforcement points attach the disclaimer; no runtime setting removes it; it is not duplicated when already present; a released report carries it end to end |
| PB-03b | `test_pb03_output_containment_e2e.py` | Through a real request: the domain families reach the output gate and produce a contained refusal that names nothing and releases no part of the report; the block is shown to happen at the gate via `node_history`; the platform families are refused earlier (at the entry point, or by the platform's own scan of a workflow node's result) and still yield a contained, actionable envelope |
| PB-04 | `test_import_isolation.py` | No source file imports the platform's internal package |
| PB-05 | `test_state_safety.py` | State carries only checkpoint-safe types and no credential-shaped field |
| PB-06 | `test_pb_invoke_order.py` | A caller below a node's declared trust level never reaches `execute()`; every node runs the full wrapper sequence in order; the caller-contract gate is the single trust boundary and no workflow node declares a level the caller could never hold |
| PB-07 | `test_pb7_hitl_interrupt_propagation.py` | This template does not pause for human review — pinned from both ends (no node calls `interrupt()`, and the runtime configuration does not enable checkpointing) so the claim cannot go stale |
| PB-08 | `test_pb08_public_path.py` | The caller credential boundary refuses absent and malformed credentials with one generic body; a valid credential reaches the whole backbone; the report is computed from the submitted document; a different document produces a different report; the caller label is rendered; an oversized channel is refused; and a value declared in `config/config.yaml` measurably changes the report — with an out-of-contract declaration falling back to the built-in floor |

## Reachability note

PB-03b separates two things that look alike. The **domain** pattern families
(a credential assignment, a private-key header) are the ones the platform's own
detector does not know, so a value of that shape travels to this template's
output gate — those tests exercise the gate. The **platform** families never get
that far: the entry point screens the structured channel before the graph runs,
and a value introduced later trips the platform's own scan of a workflow node's
result first. Those cases are still covered, because the caller must receive a
contained envelope either way, but the refusal under test there is the
platform's rather than this template's.

## Framework compliance

- `State` is a flat TypedDict — verified by `test_state_safety.py`
- No import of the platform's internal package — verified by `test_import_isolation.py`
- Every node implements `execute(self, state) -> dict` and returns a partial update
- Every node declares `required_trust_level` as a `ClassVar[TrustLevel]`
