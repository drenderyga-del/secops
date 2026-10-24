# Unified Execution contract-test matrix

Status: INT-0 contract accepted; INT-1 persistence/adapter suites pending

The scanner-agnostic INT-0 subset is executable in
`tests/test_execution_contract.py`. Persistence, backfill, retention and
adapter integration cases below are the validation contract for the separate
INT-1 Unified Execution implementation tasks.

The suite should be adapter-independent. Each adapter supplies raw events and
expected canonical transitions through the same fixtures.

## Core transition tests

| Case | Input | Expected |
|---|---|---|
| Happy path | requested → queued → running → succeeded | Ordered terminal success |
| Delivery path | running → delivering → confirmed | `succeeded` only after confirmation |
| Required delivery failure | scanner success + upload failure | Terminal `failed` or `partial`, never `succeeded` |
| Optional delivery skipped | scanner success + optional skip | Terminal `succeeded` |
| Cooperative cancellation | running → cancel requested → cancelled | Intent and confirmation are separate events |
| Timeout | running past deadline | `failed/execution/scanner_timeout` |
| Stalled observation | unchanged running execution past threshold | Status remains `running`; stalled condition event exists |
| Terminal regression | succeeded followed by running event | State does not regress; event is retained as ignored/out-of-order evidence |
| Retry | retry of failed execution | New execution linked by `retry_of`, old execution unchanged |

## Idempotency and ordering tests

1. Replaying the same `(source_system, source_scope, source_event_id)` creates
   one event.
2. Replaying a create/trigger request returns the existing execution.
3. Two workers ingesting the same event concurrently produce one transition.
4. A late `running` event cannot regress terminal state.
5. A late terminal event may enrich missing evidence without changing a
   different already-confirmed terminal outcome.
6. Step attempts are unique by `(execution_id, step_key, attempt)`.
7. Delivery retries reuse the intended idempotency key or explicitly increment
   an attempt identity; they never produce accidental duplicate ASOC sessions.

## Failure classification tests

| Raw scenario | Category | Reason |
|---|---|---|
| GitLab token missing | `authentication` | `missing_credential` |
| GitLab returns 401/403 | `authentication` | `credential_rejected` or `insufficient_scope` |
| TLS handshake fails | `connectivity` | `tls_failed` |
| Worker disappears during scan | `orchestration` | `worker_interrupted` |
| No compatible scanner worker | `capacity` | `no_compatible_worker` |
| Scanner exits non-zero | `execution` | `scanner_failed` |
| SARIF file absent | `artifact` | `artifact_missing` |
| SARIF parses but has no results unexpectedly | `artifact` | `artifact_empty` |
| ASOC upload rejected | `delivery` | `upload_rejected` |
| ASOC accepted upload but confirmation never appears | `delivery` | `confirmation_missing` |
| Source identifiers conflict with target | `data_integrity` | `target_mismatch` |

Each classifier test also asserts:

- raw status/text is preserved in sanitized form;
- failed step is populated;
- retryability is explicit;
- no credential or authorization header is stored.

## Adapter contract suites

### GitLab pipeline/job adapter

- Registering the same pipeline twice is idempotent.
- Polling the same GitLab job repeatedly updates one control execution.
- Final job status maps to one terminal outcome.
- Pipeline polling error is distinct from scanner job failure.
- Pipeline `stale` is a derived condition, not a replacement terminal state.
- Multiple security jobs under one pipeline become distinct child control
  executions.

### IaC adapter

- Two workers cannot claim the same queued run.
- Group run creates one orchestration parent and child executions per
  repository with evidence.
- Finder failure is not treated as `no_iac`.
- Mixed repository outcomes produce parent `partial`.
- Successful scan plus failed required ASOC upload is not end-to-end success.
- Queued cancellation is immediately terminal and idempotent.
- Running cancellation remains requested until the worker confirms it.
- Worker restart creates a stable interrupted failure reason.

### Config scan adapter

- Queued/running/succeeded/failed map consistently.
- App restart classifies the run as `orchestration/worker_interrupted`.
- Partial project failures produce `partial` when project evidence exists.
- Report JSON/HTML becomes artifact metadata with provenance.
- Repeated trigger/event does not create duplicate execution.

### PT AI / ASOC adapter

- GitLab trigger completion does not mark PT AI execution successful.
- Missing ASOC pipeline/task IDs classify as `orchestration/missing_correlation_id`.
- ASOC and PT AI engine statuses remain separate events/steps.
- Engine success with ASOC unavailable does not falsely claim confirmed
  delivery unless the contract permits engine confirmation.
- `PARTIAL_SUCCESS` maps consistently and preserves engine error evidence.
- Repoll adds events without creating another execution.

## Backfill verification

For a seeded fixture containing all legacy flow types:

1. Run backfill twice and assert identical row counts.
2. Assert every backfilled execution has a deterministic source identity.
3. Assert all child executions point to the correct orchestration parent.
4. Assert no timestamps are fabricated when legacy evidence is absent.
5. Compare legacy and unified latest-state views per repository/control.
6. Emit a mismatch report with source row IDs and reason.
7. Delete the new backfill rows and confirm legacy tables are unchanged.

## Retention and recovery tests

- Active, delivering and cancel-requested executions are never pruned.
- Latest execution per control binding is preserved.
- Child steps/events/deliveries cascade with an intentionally deleted
  execution.
- Artifact file deletion and artifact metadata deletion are independently
  testable.
- Database restore reproduces timeline ordering and idempotency constraints.

## Security tests

- Bearer tokens, private tokens and authorization headers are redacted.
- Raw event size is bounded.
- Unknown event fields cannot overwrite canonical columns.
- Source event identity cannot be forged across connectors without permission.
- Control actions and externally submitted events have audit actor/context.

## Release validation scenarios

The INT-1 exit criterion is satisfied only when GitLab, IaC, config scan, PT AI
and ASOC delivery can be rendered from the unified model from trigger through
required delivery. A fixture must include:

1. one successful end-to-end execution;
2. one scanner failure;
3. one silent/stalled execution;
4. one partial multi-repository execution;
5. one scan success with delivery failure;
6. one cancellation;
7. one duplicate and one out-of-order event.
