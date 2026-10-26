# ScanOps Execution lifecycle and failure taxonomy

Status: accepted implementation contract for `INT-0 Baseline`

Notion task: `Agree on the execution lifecycle and failure taxonomy`
Effort: `M`
Scope: GitLab pipelines/jobs, IaC, config scan, PT AI and ASOC delivery.

Executable contract: `app/execution_contract.py`
Automated validation: `tests/test_execution_contract.py`

## Contract goals

- One lifecycle describes every security-control execution.
- Scanner-specific statuses remain raw adapter data, not core lifecycle values.
- Scan completion and artifact delivery are separate steps.
- A successful scanner exit is not end-to-end success when the control contract
  requires artifact delivery or external confirmation.
- Current and future adapters can record transitions idempotently.
- Coverage observations are derived from executions; they are not executions
  themselves.

## Core entities

- `Execution`: one attempt to run a security control, or an orchestration
  parent grouping related control executions against a target and ref.
- `ExecutionStep`: an ordered unit of work within an execution.
- `Artifact`: output produced by an execution step, such as SARIF or JSON.
- `Delivery`: an attempt to deliver an artifact to an external destination.
- `ExecutionEvent`: immutable evidence for a transition or observed condition.

Every execution must carry:

- `execution_kind` (`control` or `orchestration`), optional
  `parent_execution_id`, `target_id`, `trigger`, `source_system`;
- `control_id` for every `control` execution;
- repository/scope, ref and commit SHA when applicable;
- correlation keys from GitLab, scanner, worker and ASOC;
- timestamps for requested, queued, started and finished;
- canonical status and raw source status;
- failure category, reason code and sanitized evidence when unsuccessful.

## Canonical lifecycle

| Status | Meaning | Terminal |
|---|---|---:|
| `requested` | ScanOps or an external source accepted the intent to run. | No |
| `queued` | The execution is waiting for a compatible worker or external system. | No |
| `running` | At least one execution step is actively running. | No |
| `delivering` | Scanner work finished; required artifacts are being delivered or confirmed. | No |
| `cancel_requested` | A cooperative cancellation was requested but not confirmed. | No |
| `succeeded` | Every required step and delivery obligation succeeded. | Yes |
| `partial` | Useful work completed, but at least one required target/step/delivery failed. | Yes |
| `failed` | The execution did not satisfy its control contract. | Yes |
| `cancelled` | Cancellation was confirmed before contract completion. | Yes |

`stalled`, `stale` and `delayed` are derived operational conditions, not
terminal lifecycle statuses. They must retain the underlying status, for
example `running + stalled` or `delivering + delayed`.

Timeout is represented as terminal `failed` with a timeout reason code. This
avoids a separate terminal status whose operational meaning is still failure.

## Standard steps

Adapters use only the steps required by their control contract:

1. `resolve_target`
2. `prepare_input`
3. `start_scanner`
4. `poll_scanner`
5. `produce_artifact`
6. `deliver_artifact`
7. `verify_delivery`
8. `finalize`

Examples:

- GitLab job monitoring can begin at `poll_scanner`.
- Config scan normally uses `resolve_target`, `start_scanner`,
  `produce_artifact`, and `finalize`.
- IaC with ASOC upload uses the full chain through `verify_delivery`.
- PT AI triggered through ASOC separates GitLab trigger acceptance, ASOC
  orchestration, PT AI engine execution and ASOC completion into distinct
  steps.

## Failure taxonomy

The category is stable and suitable for aggregation. The reason code is
specific and extensible. Raw scanner text is preserved separately.

| Category | Typical reason codes | Examples |
|---|---|---|
| `configuration` | `missing_endpoint`, `invalid_profile`, `unsupported_ref`, `missing_binding` | Incorrect target, profile or integration configuration |
| `authentication` | `missing_credential`, `credential_rejected`, `credential_expired`, `insufficient_scope` | GitLab/ASOC/PT AI token problems |
| `connectivity` | `dns_failed`, `connection_refused`, `tls_failed`, `request_timeout`, `upstream_unreachable` | Network and endpoint availability |
| `capacity` | `queue_saturated`, `no_compatible_worker`, `worker_unavailable`, `concurrency_limit`, `resource_exhausted` | Queue and worker pressure |
| `orchestration` | `trigger_rejected`, `missing_correlation_id`, `poll_failed`, `invalid_transition`, `worker_interrupted` | Control-plane and lifecycle failures |
| `execution` | `scanner_failed`, `scanner_timeout`, `cancelled_by_source`, `invalid_output`, `partial_target_failure` | Scanner/runtime failure |
| `artifact` | `artifact_missing`, `artifact_empty`, `artifact_unreadable`, `artifact_schema_invalid` | Output production and validation |
| `delivery` | `destination_missing`, `upload_rejected`, `upload_failed`, `delivery_timeout`, `confirmation_missing`, `duplicate_delivery` | ASOC or another destination |
| `data_integrity` | `target_mismatch`, `ref_mismatch`, `correlation_conflict`, `duplicate_event` | Provenance or identity inconsistency |

Cancellation is lifecycle intent/outcome, not a failure category. The
initiator and reason are recorded on the cancellation event.

## Mapping of current flows

| Current source | Raw statuses | Canonical mapping |
|---|---|---|
| `active_pipelines` | `registered`, `polling`, `completed`, `failed`, `stale` | Orchestration parent; `stale` becomes a condition |
| `pipeline_jobs` / `job_reports` | running/success/failed/cancelled/skipped | Child control executions when the job represents a control; otherwise steps of the orchestration parent |
| `iac_scan_runs` | `queued`, `running`, `succeeded`, `partial`, `failed`, `cancelled` | Orchestration parent, except single-repository runs which may map directly to a control execution |
| IaC repository finder | `queued`, `running`, `succeeded`, `failed` | `resolve_target`/`prepare_input` step |
| IaC repository scan | `queued`, `running`, `succeeded`, `no_iac`, `failed` | Scanner step; `no_iac` is successful only when absence of IaC is expected |
| IaC ASOC upload | `pending`, `uploaded`, `skipped`, failure variants | Delivery step; `skipped` succeeds only if delivery is optional |
| `config_scan_runs` | `queued`, `running`, `succeeded`, `failed` | Direct mapping to execution without delivery unless the contract requires it |
| `ptai_scans.asoc_status` | `PENDING`, `IN_PROGRESS`, `SUCCESS`, `PARTIAL_SUCCESS`, `ERROR`, `TRIGGER_FAILED` | Split into trigger, orchestration, engine and delivery/confirmation steps |
| audit coverage | `covered`, `pipeline_no_sec`, `no_pipelines`, `inaccessible`, `excluded` | Derived coverage state, not execution status |

## End-to-end outcome rules

The control contract declares which steps and deliveries are required.

- `succeeded`: all required steps succeeded and every required delivery is
  confirmed.
- `partial`: at least one target or required step succeeded and at least one
  failed.
- `failed`: no useful required result was produced, or a mandatory final
  obligation failed.
- An optional delivery may be `skipped` without changing execution success.
- A required delivery that is skipped, rejected or unconfirmed prevents
  `succeeded`.
- Findings count and finding severity never determine operational success.

## Transition and event rules

- Events have a source-scoped idempotency key.
- Replaying the same event does not create another transition.
- Terminal executions cannot return to a non-terminal state. A retry creates a
  new execution linked with `retry_of_execution_id`.
- Source timestamps and ingestion timestamps are both preserved.
- Out-of-order events may enrich evidence but cannot regress canonical state.
- Every failure stores category, reason code, failed step, source status,
  retryability and sanitized raw context.
- Derived `stalled`/`stale` conditions record threshold, last evidence time and
  affected scope.

## Current implementation gaps

1. Pipeline aggregation can become `completed` even when polling recorded an
   error; job outcome and polling health are not represented separately.
2. Pipeline `stale` replaces the underlying lifecycle status instead of being
   an operational condition.
3. IaC run success is calculated primarily from scan statuses; required ASOC
   delivery is not part of the final verdict.
4. IaC finder, scanner and ASOC upload use independent strings without a shared
   transition/event contract.
5. Config scan has no partial or cancellation semantics.
6. PT AI stores engine-derived and ASOC-derived outcomes in
   `asoc_status`, obscuring which system supplied the evidence.
7. Error text exists, but stable failure categories/reason codes and
   retryability are not persisted consistently.
8. There is no immutable cross-flow event history with correlation and
   idempotency keys.

## Validation scenarios

The contract must classify these prepared scenarios consistently:

1. GitLab token rejected before jobs can be polled.
2. Pipeline is still running but has not changed within the freshness window.
3. IaC scan succeeds and required ASOC upload fails.
4. IaC group run succeeds for some repositories and fails for others.
5. Config scan is interrupted by application restart.
6. PT AI trigger job succeeds but ASOC returns no pipeline/task identifier.
7. ASOC accepts the trigger but PT AI engine fails.
8. Artifact exists but is empty or cannot be parsed.
9. Duplicate or out-of-order telemetry is received.
10. User requests cancellation during a long-running scan.

## Validation against current internal failure paths

The dependency on real failure stories is satisfied for this Internal task by
the failure paths already exercised in the current application and test suite:

- rejected or missing GitLab/ASOC credentials;
- worker interruption during config and IaC scans;
- finder failure versus an expected `no_iac` outcome;
- mixed-success IaC group scans;
- missing, invalid or failed SARIF delivery;
- PT AI trigger success followed by engine or ASOC failure;
- missing PT AI/ASOC correlation identifiers;
- stale pipelines and long-running scanner work;
- queued and cooperative running cancellation.

The executable mapping intentionally fails closed for an unknown vendor state.
New adapters or statuses require an explicit mapping and contract test. Control
policy still decides whether `no_iac`, a skipped job or a skipped delivery is
acceptable; none of those raw values implicitly becomes end-to-end success.

Operator thresholds and required delivery obligations belong in each future
control contract. Reliability baseline values are tracked separately by the
`Measure the reliability baseline` task and do not change this vocabulary.
