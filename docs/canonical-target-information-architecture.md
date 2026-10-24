# Canonical Target/Product/Repository information architecture

Status: normative implementation contract proposed by backlog item 315 for
items 316, 317 and 319.
Source task:
the canonical Target/Product/Repository information architecture design.
The machine-readable route inventory is
[`canonical-target-information-architecture.json`](canonical-target-information-architecture.json).

## Decision

`Target` is the operator-facing family name. It has two explicit levels:

- A **Product target** is the ownership and policy aggregate represented by the
  existing `Product` model. It owns repository membership and product-level
  posture rollups.
- A **Repository target** is the concrete, executable observation scope
  represented by the existing `Repository` model. Its detail experience is
  **Target 360**.

The UI must not use Product, Repository and Target as interchangeable names.
Use **Targets** for the collection and navigation destination, **Product
target** when the level matters, **Repository target** when the level matters,
and **Target 360** only for repository detail.

This is a navigation and presentation contract. It does not rename database
models, change identifiers, merge scanner data models, or implement the new
routes.

## Page ownership

### Targets collection — `GET /targets`

This is the only entity browser. It defaults to `?level=product`; the sibling
mode is `?level=repository`. `level` describes entity granularity, not a visual
layout, so future table/card changes do not alter URLs.

The product mode answers “which product targets do we monitor?” and shows
aggregate posture. The repository mode answers “which concrete targets do we
monitor?” and shows parent product plus compact health. Both modes link to the
canonical detail for the row's entity level.

`/repositories` is therefore not retained as a competing page. It becomes a
compatibility redirect to `/targets?level=repository`.

### Product target detail — `GET /targets/products/{product_id}`

The product page owns:

- stable product identity, GitLab scope and ownership context;
- aggregate control, coverage, execution and freshness summaries;
- repository membership, with every repository linking to Target 360;
- explicit links to product-scoped Coverage and scanner workflows.

It does not own full run histories, finding tables, ASOC mapping tables or a
second repository detail. Those remain in their domain pages.

Required title: `{product.name} · Product target`.

### Repository Target 360 — `GET /targets/repositories/{repository_id}`

Target 360 owns:

- repository identity and its parent product;
- expected/configured controls and diagnostic evidence;
- freshness and observed tool/artifact versions;
- the scanner-neutral execution timeline, artifacts and delivery state;
- explicit links to job logs and scanner-specific evidence.

It does not become an editor for product membership and does not duplicate the
global repository browser. The `source` query filter remains valid. The page
title is `{repository.path} · Target 360`.

The canonical identifier is `Repository.id`. An
`IacRepository.id` from `/iac/repositories/{id}` is a different identifier and
must never be substituted without an explicit mapping.

### Coverage — `GET /coverage`

Coverage is a cross-target aggregate, not another Targets index and not a
competing product detail. It answers “where is expected security coverage
missing or stale?” across product and repository targets. It owns matrix,
exception and filtering workflows.

`GET /coverage/products/{product_id}` is the coverage-only drilldown. Rows link
to Product target or Target 360 for entity context. Canonical detail pages may
show compact coverage summaries, but must link here for the complete coverage
workflow.

### Domain evidence pages

Operations and reporting routes remain intentionally separate:

- `/repos/{repository_id}/jobs` is repository-scoped job evidence.
- `/config-scan/products/{product_id}` is Config Guard evidence and actions.
- `/iac/repositories/{repo_row_id}` is IaC run evidence. Its `repo_row_id`
  parameter is `IacRepository.id`, not canonical `Repository.id`.
- Quarterly ASOC product reporting lives on the Products registry
  (`/targets?level=product`) and its XLSX export. `/reports`,
  `/reports/operator` and `/reports/export.xlsx` only redirect there.

These pages must link back to canonical target details. They must not label
themselves Product detail, Repository detail or Target 360.

## Before and after route map

| Current route | Canonical destination | Compatibility |
|---|---|---|
| `GET /products` | `GET /targets?level=product` | `308`, preserve query |
| `GET /repositories` | `GET /targets?level=repository` | `308`, preserve query |
| `GET /products/{product_id}` | `GET /targets/products/{product_id}` | `308`, preserve query |
| `GET /repos/{repository_id}` | `GET /targets/repositories/{repository_id}` | `308`, preserve `source` and other query values |
| `GET /audit` | `GET /coverage`; filtered legacy state uses `GET /coverage/operator` | `308`, preserve filters |
| `GET /audit/products/{product_id}` | `GET /coverage/products/{product_id}` | `308`, preserve filters |
| `GET /repos/{repository_id}/jobs` | unchanged | retained Operations route |
| `GET /config-scan/products/{product_id}` | unchanged | retained module route |
| `GET /iac/repositories/{repo_row_id}` | unchanged | retained module route; `repo_row_id` is `IacRepository.id`, a different ID space |
| `GET /reports`, `GET /reports/operator`, `GET /classic/reports` | `GET /targets?level=product` | `308`; `year` + `quarter` become `period=<year>-Q<quarter>&mode=new`, other filters are dropped |
| `GET /reports/export.xlsx` | `GET /api/ui/products/export.xlsx` | `308`, same period translation |

Mutation endpoints such as `POST /products/{product_id}/delete` are not public
navigation and are outside task 316. They must not be redirected across HTTP
methods. Their forms can move to canonical pages while the action endpoints
remain compatible until a separately reviewed API cleanup.

The retained administrative collection is explicitly addressable at
`GET /targets/operator?level=product|repository`. It owns product path
normalization, duplicate cleanup and product deletion; `/classic/targets`
redirects there so these existing mutations do not disappear behind the
read-only canonical inventory.

## Redirect and deep-link contract

1. Add canonical handlers first, then switch all first-party links, then add
   redirects from legacy GET routes.
2. Use server-side `308` redirects. Do not render the old and new detail
   templates independently.
3. Preserve query parameters. The unfiltered `/audit` alias lands on
   `/coverage`; requests carrying `only_problems`, `selected_product_ids`,
   `status`, `status_filter` or `run_id` land on `/coverage/operator`, which
   owns that complete server-rendered filter contract. When a redirect introduces `level`, the
   canonical value wins and all non-conflicting existing parameters survive.
4. URL fragments are browser-owned. Browser tests must verify that known
   section fragments still land on a real element after redirect.
5. Unknown entity IDs keep current `404` behavior at the canonical handler.
   Redirect handlers do not perform a second lookup.
6. Maintain legacy redirects for at least one release after all first-party
   links move. Removal requires access-log evidence and an explicit changelog
   entry.
7. Use named URL builders/constants in implementation rather than adding new
   hard-coded legacy paths to templates.

## Breadcrumb and naming contract

Breadcrumbs show entity ancestry, while the sidebar shows destinations:

| Page | Breadcrumb |
|---|---|
| Targets collection | `ScanOps / Security posture / Targets` |
| Product target | `ScanOps / Security posture / Targets / {product.name}` |
| Target 360 | `ScanOps / Security posture / Targets / {product.name} / {repository.path}` |
| Coverage | `ScanOps / Security posture / Coverage` |
| Product coverage | `ScanOps / Security posture / Coverage / {product.name}` |
| Repository jobs | `ScanOps / Operations / Executions / {repository.path}` |

The Targets sidebar item is active for `/targets` and descendants, including
Target 360. Coverage is active only for `/coverage` and descendants.
Scanner-specific evidence keeps Operations active. Quarterly ASOC reporting is
part of Products (`/targets?level=product`); there is no separate report
destination.

Use these action labels:

- **Open product target** → canonical product detail.
- **Open Target 360** → canonical repository detail.
- **View job logs** → repository jobs.
- **View coverage** → Coverage aggregate or scoped drilldown.
- **Open Config Guard evidence** / **Open IaC evidence** → module route.

Avoid generic **View details**, **View repositories**, and **View logs** when
the object or evidence type is not otherwise explicit.

## Local navigation contract for task 317

Entity ancestry belongs in breadcrumbs, page sections belong in a local table
of contents, and data subsets belong in filters/tabs. They must not share the
same visual control.

- Targets `level` is a collection mode and must be rendered as a mode switch.
- Target 360 `source` is a data filter and must be rendered as a filter.
- Long-page section anchors such as `#controls` and `#timeline` are a local TOC.
- Short pages omit the TOC. A TOC never navigates to another route.
- Existing Target 360 anchors `#posture`, `#controls` and `#timeline` remain
  stable during task 317.

## ASOC linking contract for task 319

The Products registry lists ASOC cards; it explains their sync state and does
not own targets.

- A card row links to its card view `/targets/products/asoc/{asoc_id}` (the
  ASOC id is URL-encoded); each synced GitLab group links to
  `/targets/products/{product_id}`.
- A repository in a card view links to
  `/targets/repositories/{repository_id}` with **Repository 360**.
- Missing target or ambiguous mapping states have no fabricated target link.
- External ASOC links are explicitly labeled **Open in ASOC** and remain
  visually distinct from internal target navigation.
- `BI_ID` is mapping metadata, never a Product or Repository route identifier.

## Migration plan

### Phase 1 — route foundation (task 316)

Add named canonical routes and render the existing data through one handler per
entity. Add redirects and route contract tests. Do not duplicate service
queries between legacy and canonical handlers.

### Phase 2 — first-party navigation (task 316)

Update sidebar activation, breadcrumbs, page titles, buttons, Coverage links
and ASOC links. Search templates, JavaScript, tests and documentation for every
legacy path. Keep mutation actions and retained evidence routes as documented.

### Phase 3 — local navigation (task 317)

Apply the TOC/filter/mode distinctions above. Preserve section fragment IDs and
add keyboard/reload browser coverage.

### Phase 4 — reporting links (task 319)

Apply explicit ASOC states and canonical links. Do not link missing or ambiguous
mappings.

### Phase 5 — compatibility retirement

After at least one release, review access logs for legacy GET routes. Remove
only redirects with no remaining consumers, document the removal, and keep
external/bookmarked deep links when evidence is inconclusive.

## Definition of done for implementing tasks

- Canonical and legacy route tests cover status, `Location`, query retention,
  `404` behavior and repository-vs-IaC identifier separation.
- No first-party GET link points to a redirect.
- Sidebar, breadcrumbs, page titles and actions use this vocabulary.
- Coverage and the Products registry link to canonical entity details.
- Existing `source` filters and stable section fragments remain reload-safe.
- Scanner-specific workflows remain evidence pages and do not become duplicate
  target details.
