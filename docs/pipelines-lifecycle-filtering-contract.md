# Pipelines lifecycle groups and filtering contract

Status: accepted product/data contract for backlog item 307

Scope: the `/pipelines` page backed by retained `active_pipelines` rows

Executable vocabulary: `app/pipeline_view_contract.py`

This contract is the shared input for the paginated query (item 308) and the
summary/filter UI (item 309). The page title is **Pipelines** and its default
view is **In progress**.

## Stored lifecycle mapping

`active_pipelines.status` currently stores exactly the five
`PipelineStatus` values below. Every value belongs to exactly one group.

| Stored status | Summary group | Reason |
|---|---|---|
| `registered` | **In progress** | Accepted for tracking and waiting for its first poll. |
| `polling` | **In progress** | Polling is active, even when the latest attempt recorded transient `error_message` text. |
| `completed` | **Completed recently** | Polling reached a terminal aggregate result with no failed or cancelled tracked jobs in the selected period. |
| `failed` | **Needs attention** | A tracked job failed or was cancelled, or terminal polling ended with an explicit error. |
| `stale` | **Needs attention** | The legacy table stores `stale` as a status; the canonical execution contract treats it as an operational condition, but it still requires attention here. |

The executable mapping must fail closed when a new `PipelineStatus` appears.
Adding a stored status therefore requires a deliberate group decision, a
mapping change and a contract-test update.

### Documented non-status cases

- `error_message` is diagnostic evidence, not another lifecycle value. A
  `polling` row with transient poll error text remains **In progress**. It does
  not create an **Errors** group.
- Terminal outcome uses the current non-retried GitLab job observation. Older
  retained attempts remain evidence but never override a successful retry or a
  later tracked-job configuration.
- `blocked`, `repeatedly_unpolled`, `queued`, `running` and `cancelled` are not
  currently stored as parent statuses by `active_pipelines`. Do not infer them
  from free text. A cancelled tracked job currently produces the existing
  parent `failed` status. If a distinct parent cancellation status is added,
  extend this contract first; blocked/repeatedly-unpolled belong in **Needs
  attention**, while queued/running belong in **In progress**.
- A zero **Needs attention** count is valid and must remain visible with the
  explanation “Failed and stale pipelines in the selected period.”

## Selected-period semantics

The URL parameter is `timeframe`; allowed values are `today`, `3d`, `7d`,
`30d`, and `all`. The default is `7d`. Boundaries are evaluated in UTC using a
half-open interval: `activity_at >= cutoff` and `activity_at < request_now`.
`today` starts at 00:00 UTC on the request date. `all` has no cutoff.

The row's `activity_at` is status-specific:

| Status | Activity timestamp, first non-null wins |
|---|---|
| `registered`, `polling` | `last_polled_at`, `updated_at`, `created_at` |
| `completed`, `failed` | `completed_at`, `finished_at`, `updated_at`, `created_at` |
| `stale` | `stale_at`, `last_polled_at`, `updated_at`, `created_at` |

This keeps actively polled work visible without treating its original
registration date as its latest activity. Older retained rows remain
discoverable with `timeframe=all`. The fixed label **Completed recently**
means “completed in the selected period”; with `all`, the selected period is
all retained history.

The same request-scoped `request_now` must be used for the list, group counts
and matching count so boundary rows cannot move between queries.

## Count contract

- `total_count`: every retained `active_pipelines` row before group, status,
  product, repository or timeframe filters. It is the stable baseline and does
  not change when any filter is selected.
- `matching_count`: rows after every effective URL filter, before pagination.
- Summary-card count: rows in that card's group after product, repository and
  timeframe filters, while ignoring the current `group` and `status` filters.
  Card counts are therefore useful navigation facets.
- Page copy uses `Showing {matching_count} of {total_count} pipelines`.
- Pagination never changes either count. The list uses deterministic
  descending `activity_at` order with descending `active_pipelines.id` as the
  tie-breaker.

The repository filter is subordinate to the product filter. If both are
present and the repository does not belong to the product, the valid result is
zero matches, not a repository from another product.

## URL schema and normalization

All state required to reproduce the list is encoded in the query string.

| Parameter | Values | Default | Semantics |
|---|---|---|---|
| `group` | `needs_attention`, `in_progress`, `completed_recently` | `in_progress` | Primary lifecycle group. |
| `status` | one stored `PipelineStatus` value | absent | Narrows the selected group to one status. |
| `product` | positive integer `products.id` | absent | Narrows to one product. |
| `repository` | positive integer `repositories.id` | absent | Narrows to one repository. |
| `timeframe` | `today`, `3d`, `7d`, `30d`, `all` | `7d` | Applies the selected-period rule above. |
| `page` | positive integer | `1` | Server-side result page. |
| `page_size` | `25` or `50` | `25` | Rows per page. |

Canonical examples:

```text
/pipelines
/pipelines?group=needs_attention&timeframe=7d
/pipelines?group=needs_attention&status=stale&product=4&timeframe=30d
/pipelines?group=completed_recently&repository=19&timeframe=all&page=2&page_size=50
```

Normalization rules:

1. When `group` is absent and a valid `status` is present, derive the group
   from the status. This makes `/pipelines?status=failed` useful and shareable.
2. When both are present, `status` narrows `group`. An incompatible pair is a
   valid zero-match selection and renders the filtered empty state.
3. Unknown `group`, `status`, `timeframe`, non-positive IDs/pages and
   unsupported page sizes normalize to their defaults or absence and return
   HTTP 200. The rendered controls expose the effective normalized values.
4. Changing any filter other than `page` resets `page` to `1`. Pagination links
   preserve every effective filter.
5. Links and forms emit only the parameter names and values defined here.

## Empty states

Empty-state choice uses the unpaginated counts:

| Condition | Heading | Body/action copy |
|---|---|---|
| `total_count == 0` | **No pipelines recorded yet** | “Register a pipeline through `/api/pipelines/register` to start tracking it.” |
| selected **In progress** has zero matches | **No pipelines are in progress** | “No registered or polling pipelines match the selected filters and period. Clear filters or choose another period.” |
| selected **Needs attention** has zero matches | **No pipelines need attention** | “No failed or stale pipelines match the selected filters and period. Clear filters or choose another period.” |
| selected **Completed recently** has zero matches | **No pipelines completed in this period** | “Choose a longer period or clear filters to include more retained history.” |
| a compatible group has data but an explicit status/product/repository combination has zero matches | **No pipelines match these filters** | “Clear one or more filters to broaden the result.” |
| requested page exceeds the last page while matches exist | no empty state | Redirect to or render the last valid page while preserving filters. |

Zero is evidence, not an error. The three summary cards always render, including
zero counts; there is no fourth **Errors** card.

## Implementation invariants for items 308 and 309

- The list, `matching_count` and the three card counts must use the same mapping,
  timestamp expression and normalized filters.
- Aggregate job counts must not introduce a per-row query.
- A status added to `PipelineStatus` must fail the executable coverage test
  until this document and mapping are updated.
- The UI may explain transient `error_message` evidence on a row but must not
  silently reclassify the row.
- Filters, summary cards and pagination are ordinary links/forms with visible
  selected state and keyboard semantics; JavaScript may enhance but must not be
  required to reproduce the URL state.

## Query implementation (task 308)

`PipelineQueryService` in `app/pipeline_query_service.py` is the authoritative
server query for the list, matching count and lifecycle facet counts. It uses
the status-specific `activity_at` SQL expression above for filtering and
ordering, with descending `active_pipelines.id` as the stable tie-breaker.

The page query joins one pre-aggregated `pipeline_jobs` count subquery, so
rendering 25 or 50 rows does not issue per-row count queries. Its query count is
fixed: total baseline, matching count, lifecycle facet counts and one paginated
row query. No schema migration is required because existing timestamp, foreign
key and status columns are sufficient for the contract.
