# ASOC product registry and connection reporting

ASOC is the system of record for product-card identity. ScanOps enriches each
ASOC card with exact GitLab group/repository matches, pipeline and coverage
signals, and the date ScanOps first linked it. The Products view
(`/targets?level=product`) is the quarterly registry built from these cards.
Product directory leader data is not part of the current registry.

## Identity and matching

- `asoc_id` identifies one product card. `BI_ID` is a reporting attribute, not
  a unique key; repeated BI_IDs remain separate until an operator resolves the
  source cards.
- A card may reference more than one monitored GitLab product group. ScanOps
  retains one product row and lists each exact matching group under it.
- A GitLab URL that resolves to multiple monitored groups, or multiple ASOC
  cards claiming the same monitored group, remains in `conflict` for review.
  ScanOps does not infer or merge business products from names or BI_IDs.
- A conflict never exposes a candidate target as an authoritative link. The
  card remains visible with its match counts and conflict reason until the
  source ambiguity is resolved.
- An unmatched Description URL is not evidence of a monitored target. Mapping
  uses normalized exact URLs from the ASOC Description, not ASOC slug or
  repository membership.
- A complete snapshot that omits a previously known card marks it inactive
  instead of deleting it. Its creation date, last verified links and
  `first_connected_at` are kept, so the card stays in the quarters it belongs
  to (`mode=total` counts it in every quarter that ended before it was
  removed); if the card
  reappears, the same record is reactivated with its original dates.
- Malformed, truncated, and duplicate-ID snapshots are rejected before the
  registry changes. If a snapshot would archive at least a quarter of active
  cards, or the entire catalog, the operator must confirm the displayed active,
  incoming, and removal counts. The confirmation is bound to both the current
  registry and the exact incoming ASOC IDs; a changed snapshot requires a new
  confirmation. Cancelling the prompt makes no changes.
- Smaller removals (below that threshold) are applied by an operator's sync
  without a prompt. The scheduled sync never archives a card: if its snapshot
  would remove any, nothing from that snapshot is applied and the attempt waits
  for an operator (ASOC products are not normally removed, so this guards
  against a faulty upstream shrinking the registry run by run). Every sync that
  archives cards writes an audit row (see below); cards are archived, never
  deleted.
- A valid empty snapshot is recoverable, but archiving all active cards
  requires the same explicit confirmation as any other mass removal. Do not
  manually delete ASOC cards or lifecycle guards to recover a sync.
- A database guard intentionally blocks physical deletion of ASOC cards, even
  if an older release is rolled back and tries to delete omitted cards. A
  transaction-local database gate also rejects ASOC inserts/updates from a
  pre-lifecycle release, which could otherwise store raw Description text or
  bypass the lifecycle rules above. The one exception is PostgreSQL's own
  `ON DELETE SET NULL` when a ScanOps product is deleted or merged: it only
  clears the card's target and is allowed, so product maintenance keeps
  working. Restore the current release and synchronize forward; never drop
  lifecycle guards or triggers as part of rollback.
- ASOC Description is untrusted input. ScanOps stores only vetted HTTP(S)
  links to GitLab or configured product-reference hosts and exact
  `ScanOps-Scope` markers; it strips query/fragment data and GitLab sub-routes
  (`/-/…`), rejects userinfo, token/PAT-shaped path values, `key=value`
  credential segments and reserved GitLab routes (`/api`, `/oauth`, `/-`,
  `/profile`, `/admin`), and drops free-form prose. Ordinary group or project
  names such as `platform/secrets` are kept. A GitLab host is recognized when
  it is a configured GitLab instance or the host of an existing ScanOps
  product (so webhook-only instances count). Existing descriptions are
  sanitized by a tracked one-time migration.
  Configured ASOC public links with userinfo, query, or fragment are omitted.

## Audience and quarterly connection date

Set exactly one machine-readable line in the ASOC Description:

```text
ScanOps-Scope: internal
```

or:

```text
ScanOps-Scope: external
```

Without the marker the card is `unclassified`; contradictory markers are a
scope conflict. ScanOps does not guess audience from names, URL hostnames or
free-form descriptions.

The quarterly connection date is the ASOC card's `created` timestamp,
interpreted in `UI_TIMEZONE`: a product counts as connected when its card
appears in ASOC. `period=YYYY-QN&mode=new` lists cards created in the quarter;
`mode=total` lists every card that existed at the quarter end, including
cards removed from ASOC later. The XLSX export is built from the same slice.

`first_connected_at` is kept separately as “Linked in ScanOps”: the first
successful sync that confirmed a Description link to a monitored group. Cards
that were already linked before this date was tracked keep no date and are
shown as “Unknown · linked before date tracking”; ScanOps never invents one.

A card counts as waiting while it needs attention (no GitLab link, group not
in ScanOps, conflict or not yet synced) and has never been linked — no link
date and no currently linked group. It shows the days since its ASOC creation
and is marked overdue after `PRODUCT_SYNC_GRACE_DAYS` (default 3). A linked
card that degrades (for example to partial) keeps its attention status but is
not waiting. Cards linked before date tracking are flagged
(`connected_before_tracking`), so they stay out of the waiting count and keep
no invented date even if they later lose every link.

A snapshot that omits `created`, or sends a value outside 2000–2099, never
overwrites a stored plausible date; an implausible stored date is cleared on
the next sync. Cards without a usable date cannot be placed in a quarter. The
current registry still lists them; each view and the XLSX summary report how
many of them match the view's audience and search filters, and a quarter view
notes that they are missing from its slice.

ASOC synchronization runs two ways:

- **Operator action** (Products → Synchronize ASOC). It requires configured UI
  Basic authentication and a same-origin request; a CI Bearer token cannot
  trigger it, and it is disabled server-side when UI auth is not configured.
  Only this path can confirm a mass removal.
- **Scheduled sync** in the pipeline worker every
  `ASOC_PRODUCT_SYNC_INTERVAL_MINUTES` (default 60, `0` disables it), using
  the configured ASOC connection regardless of UI auth. It applies new and
  changed cards, never archives any card (such a snapshot waits for an
  operator, including its new cards), and stores failures only as an opaque
  reference; the last attempt is shown on Products.

Every sync that archives cards writes one `asoc_product_sync_events` row with
the actor (`operator` or `scheduler`), counts, an opaque snapshot version and
`confirmed` (true only for an operator-confirmed mass removal); the row
contains no upstream names or URLs. XLSX exports force every ASOC-controlled
string to text and strip control characters, so formula-like names and slugs
are not evaluated by spreadsheet software.

## Product drill-downs

`/targets/products/asoc/{asoc_id}` is the card view: connection facts, every
GitLab link on the card with its sync state, the linked groups, all
repositories in those groups (from the GitLab audit, so projects that never
sent a security job are included) with their scan status and 30-day pipeline
outcome, and the card's ASOC deliveries. Several groups under one card are one
business product with several technical scopes, not duplicate products.

`/targets/products/{id}` opens a monitored GitLab group. When exactly one
active card owns it, the card view is shown with that group in focus.
Otherwise the group view lists the cards that point at it and why they do not
own it: awaiting the next sync, in conflict (including several cards waiting
for one group), or linking it in a form the sync cannot resolve. Groups that no
active card owns are listed under Products as GitLab groups without an ASOC
card, including groups a card points at but has not connected yet and groups
held in conflict (tagged “ASOC card conflict”).

The operator workbench (`/targets/products/{id}/operator`) keeps the linked
ASOC cards next to product maintenance; inactive tombstones remain in quarter
reports but are excluded from current cards there.
