import { useMemo, useState } from "react";

import { EmptyState, Status, relativeTime, useLiveData } from "../shared";
import { CoverageBar } from "./ProductsView";
import { formatDate, pluralize, repositoryTones, scopeLabels, statusTones } from "./format";
import type { DetailRepository, GroupRow, ProductDetailPayload, ProductRow, RepositoryStatus } from "./types";
import "./products.css";

const repositoryFilters: { value: RepositoryStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "not_scanned", label: "Not scanned" },
  { value: "unknown", label: "Audit failed" },
  { value: "not_audited", label: "Not audited" },
  { value: "inactive", label: "No recent pipelines" },
  { value: "scanned", label: "Scanned" },
  { value: "excluded", label: "Excluded" },
];

function detailApiPath(pathname: string) {
  const card = pathname.match(/^\/targets\/products\/asoc\/(.+?)\/?$/);
  if (card) {
    try {
      return `/api/ui/products/widgets/${encodeURIComponent(decodeURIComponent(card[1]))}`;
    } catch {
      return null;
    }
  }
  const group = pathname.match(/^\/targets\/products\/(\d+)\/?$/);
  return group ? `/api/ui/products/targets/${group[1]}` : null;
}

function registryBackHref() {
  try {
    const referrer = new URL(document.referrer);
    if (referrer.origin === window.location.origin && referrer.pathname === "/targets" && referrer.searchParams.get("level") !== "repository") {
      return `${referrer.pathname}${referrer.search}`;
    }
  } catch {
    // No same-origin referrer: fall back to the registry root.
  }
  return "/targets?level=product";
}

function isCard(product: ProductRow | GroupRow): product is ProductRow {
  return "asoc_id" in product;
}

export function ProductDetail() {
  const apiPath = detailApiPath(window.location.pathname);
  const { data, state, retry } = useLiveData<ProductDetailPayload>(apiPath, 60_000);
  const [repositoryFilter, setRepositoryFilter] = useState<RepositoryStatus | "all">("all");
  const backHref = useMemo(registryBackHref, []);

  const repositoryCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const repository of data?.repositories ?? []) counts.set(repository.status, (counts.get(repository.status) ?? 0) + 1);
    return counts;
  }, [data?.repositories]);
  const visibleRepositories = (data?.repositories ?? []).filter(
    (repository) => repositoryFilter === "all" || repository.status === repositoryFilter,
  );

  if (!data) {
    return (
      <div className="view products-view">
        <a className="pr-back" href={backHref}>← Products</a>
        <EmptyState state={state} title="Product unavailable" copy="The product was not found or ScanOps could not read its evidence." onRetry={retry} />
      </div>
    );
  }

  const product = data.product;
  const card = isCard(product) ? product : null;
  const groups = card ? card.groups : [product as GroupRow];
  const repos = product.repositories;
  const pipelines = product.pipelines;

  return (
    <div className="view products-view pr-detail">
      <a className="pr-back" href={backHref}>← Products</a>
      <header className="page-title">
        <div>
          <p>{card ? ["Product", card.bi_id ? `BI_${card.bi_id}` : "No BI_ID", scopeLabels[card.scope] ?? card.scope].join(" · ") : "GitLab group · no ASOC card"}</p>
          <h1>{product.name}</h1>
          <span className="pr-detail-status">
            {card
              ? <><span className={`status status-${statusTones[card.status]}`}><i />{card.status_label}</span>{card.status_hint}</>
              : data.claimed_by.some((claim) => claim.state === "conflict")
                ? <><span className="status status-failed"><i />ASOC card conflict</span>An ASOC card that links this group is in conflict, so no card owns the group yet.</>
                : data.claimed_by.some((claim) => claim.state === "pending_sync")
                  ? <><span className="status status-partial"><i />Waiting for ASOC sync</span>An ASOC card links this group; the next sync will connect them.</>
                  : data.claimed_by.length
                    ? <><span className="status status-failed"><i />ASOC link not resolved</span>An ASOC card points at this group, but the sync cannot connect them. Put the exact group URL on the card.</>
                    : <><span className="status status-failed"><i />Missing from ASOC</span>ScanOps receives security jobs from this group, but no ASOC product card links to it.</>}
          </span>
        </div>
        <div className="header-actions">
          {card?.asoc_url && <a className="quiet-button" href={card.asoc_url} target="_blank" rel="noopener noreferrer">Open in ASOC <span>↗</span></a>}
          {groups.length === 1 && <a className="quiet-button" href={groups[0].pipelines_href}>Pipelines</a>}
        </div>
      </header>

      <section className="pr-facts" aria-label="Product facts">
        {card && <Fact label="Connected" value={formatDate(card.connected_at, data.timezone)} note={card.connected_quarter ?? "ASOC date unknown"} />}
        {card && (
          <Fact
            label="Linked in ScanOps"
            value={card.linked_at ? formatDate(card.linked_at, data.timezone) : card.linked_before_tracking ? "Unknown" : "Not yet"}
            note={card.waiting_days !== null
              ? `waiting ${pluralize(card.waiting_days, "day")}`
              : card.linked_before_tracking
                ? "linked before date tracking"
                : card.linked_at ? "first successful sync" : "not linked"}
            tone={card.overdue ? "failed" : undefined}
          />
        )}
        <div className="pr-fact pr-fact-wide">
          <span>Repositories scanned</span>
          <strong>{repos.scanned}<small> / {repos.total}</small></strong>
          <CoverageBar scanned={repos.scanned} notScanned={repos.not_scanned} inactive={repos.inactive} unknown={repos.unknown + repos.not_audited} total={repos.total} />
          <small>
            <b className={repos.not_scanned ? "is-danger" : undefined}>{repos.not_scanned} not scanned</b>
            {" · "}{repos.inactive} without recent pipelines
            {" · "}{repos.audited_at ? `audit ${relativeTime(repos.audited_at)}` : "audit pending"}
          </small>
        </div>
        <Fact label="Pipeline success · 30d" value={pipelines.success_rate === null ? "—" : `${pipelines.success_rate}%`} note={`${pipelines.repos_green}/${pipelines.repos_with_pipelines} repos green`} tone={pipelines.success_rate !== null && pipelines.success_rate < 80 ? "partial" : undefined} />
        <Fact label="Open failed jobs" value={String(data.open_failed_jobs)} note={`last success ${relativeTime(pipelines.last_success_at)}`} tone={data.open_failed_jobs ? "failed" : undefined} />
      </section>

      <div className="pr-detail-grid">
        <section className="pr-panel" aria-label="ASOC to GitLab sync">
          <div className="pr-section-heading">
            <div>
              <span>ASOC card → GitLab</span>
              <h2>{card ? "Links on the ASOC card" : "ASOC cards"}</h2>
            </div>
          </div>
          {card && (data.links.length ? (
            <ul className="pr-links">
              {data.links.map((link) => (
                <li key={link.url ?? link.state} className={`link-${link.state}`}>
                  {link.url ? <a href={link.url} target="_blank" rel="noopener noreferrer">{link.url.replace(/^https?:\/\//, "")} ↗</a> : <span>Unsafe link hidden</span>}
                  <small>
                    {link.state === "matched" && link.group && <>Synced with <a href={link.group.href}>{link.group.name}</a></>}
                    {link.state === "not_monitored" && "GitLab group has not reported to ScanOps yet"}
                    {link.state === "ambiguous" && "Resolves to more than one ScanOps group"}
                    {link.state === "reference" && "Product reference"}
                  </small>
                </li>
              ))}
            </ul>
          ) : <p className="pr-muted">The ASOC card description has no GitLab link. Add the product group URL to the card in ASOC.</p>)}
          {!card && (data.claimed_by.length ? (
            <ul className="pr-links">
              {data.claimed_by.map((claim) => (
                <li key={claim.asoc_id} className={claim.state === "pending_sync" ? "link-pending" : "link-ambiguous"}>
                  <a href={claim.href}>{claim.name}{claim.bi_id ? ` · BI_${claim.bi_id}` : ""}</a>
                  <small>
                    {claim.state === "conflict" && "Its GitLab links are in conflict: another ASOC card, or another ScanOps product with the same group URL, claims the same target."}
                    {claim.state === "pending_sync" && "Links this group; the next ASOC sync will connect them."}
                    {claim.state === "unresolved" && `Its GitLab links point here but do not resolve to this group during sync · ${claim.status_label}. Put the exact group URL on the card.`}
                  </small>
                </li>
              ))}
            </ul>
          ) : <p className="pr-muted">No ASOC card links to this group. Create the product card in ASOC or add this group URL to an existing card.</p>)}
        </section>

        <section className="pr-panel" aria-label="GitLab groups">
          <div className="pr-section-heading">
            <div>
              <span>ScanOps targets</span>
              <h2>{pluralize(groups.length, "GitLab group")}</h2>
            </div>
          </div>
          {groups.length ? (
            <ul className="pr-links">
              {groups.map((group) => (
                <li key={group.id} className={data.focus_group_id === group.id ? "is-focus" : undefined}>
                  {group.url ? <a href={group.url} target="_blank" rel="noopener noreferrer">{group.path || group.name} ↗</a> : <span>{group.path || group.name}</span>}
                  <small><a href={group.pipelines_href}>Pipelines</a> · <a href={group.operator_href}>Operator workbench</a></small>
                </li>
              ))}
            </ul>
          ) : <p className="pr-muted">No GitLab group is synced for this product yet.</p>}
        </section>
      </div>

      <section className="pr-table-surface" aria-label="Repositories">
        <div className="pr-section-heading">
          <div>
            <span>Repositories in the product groups</span>
            <h2>{pluralize(data.repositories.length, "repository", "repositories")}</h2>
          </div>
          <div className="pr-chips" role="group" aria-label="Repository status filter">
            {repositoryFilters
              .filter((option) => option.value === "all" || repositoryCounts.get(option.value))
              .map((option) => (
                <button
                  type="button"
                  key={option.value}
                  aria-pressed={repositoryFilter === option.value}
                  onClick={() => setRepositoryFilter(option.value)}
                >
                  {option.label} <b>{option.value === "all" ? data.repositories.length : repositoryCounts.get(option.value)}</b>
                </button>
              ))}
          </div>
        </div>
        <div className="pr-table pr-repo-table" role="table" aria-label="Product repositories">
          <div className="pr-row pr-head" role="row">
            <span role="columnheader">Repository</span>
            <span role="columnheader">Security scans</span>
            <span role="columnheader">Last pipeline</span>
            <span role="columnheader">Success · 30d</span>
            <span role="columnheader">Open failures</span>
            <span role="columnheader" className="pr-sr-only">Actions</span>
          </div>
          {visibleRepositories.map((repository) => (
            <RepositoryRow repository={repository} showGroup={groups.length > 1} key={`${repository.group_id}:${repository.path}`} />
          ))}
          {!visibleRepositories.length && (
            <p className="pr-empty" role="status">
              {data.repositories.length ? "No repositories match this filter." : "No repositories are known for this product yet. They appear after the GitLab audit or the first security job."}
            </p>
          )}
        </div>
      </section>

      {data.deliveries.summary.total > 0 && (
        <section className="pr-panel" aria-label="ASOC delivery">
          <div className="pr-section-heading">
            <div>
              <span>Results delivered to ASOC</span>
              <h2>{data.deliveries.summary.confirmed} confirmed of {data.deliveries.summary.total} deliveries</h2>
            </div>
            <small className="pr-muted">{data.deliveries.summary.failed} failed · {data.deliveries.summary.pending} awaiting confirmation</small>
          </div>
          <ul className="pr-deliveries">
            {data.deliveries.rows.map((delivery) => (
              <li key={delivery.id}>
                <code>EX-{String(delivery.execution_id).padStart(5, "0")}</code>
                <span>{delivery.source}</span>
                <Status value={delivery.status} />
                <small>{delivery.failure_reason ?? relativeTime(delivery.confirmed_at ?? delivery.attempted_at)}</small>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Fact({ label, value, note, tone }: { label: string; value: string; note: string; tone?: "failed" | "partial" }) {
  return (
    <div className={`pr-fact${tone ? ` tone-${tone}` : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </div>
  );
}

function RepositoryRow({ repository, showGroup }: { repository: DetailRepository; showGroup: boolean }) {
  const pipeline = repository.pipelines;
  return (
    <div className="pr-row" role="row">
      <div className="pr-product" role="cell">
        {repository.url
          ? <a href={repository.url} target="_blank" rel="noopener noreferrer" title={repository.path}>{repository.path} ↗</a>
          : <span>{repository.path}</span>}
        <small>{showGroup && repository.group_name ? `${repository.group_name} · ` : ""}{repository.scanops_repository_id ? "reports to ScanOps" : "never reported to ScanOps"}</small>
      </div>
      <div className="pr-status-cell" role="cell">
        <span className={`status status-${repositoryTones[repository.status]}`}><i />{repository.status_label}</span>
        {repository.audit_reason && <small title={repository.audit_reason}>{repository.audit_reason}</small>}
      </div>
      <div className="pr-pipelines" role="cell">
        {pipeline.last_status ? (
          <>
            {pipeline.last_url
              ? <a href={pipeline.last_url} target="_blank" rel="noopener noreferrer"><Status value={pipeline.last_status === "completed" ? "succeeded" : pipeline.last_status} /></a>
              : <Status value={pipeline.last_status === "completed" ? "succeeded" : pipeline.last_status} />}
            <small>{relativeTime(pipeline.last_at)}</small>
          </>
        ) : <span className="pr-muted">{repository.last_activity_at ? `activity ${relativeTime(repository.last_activity_at)}` : "—"}</span>}
      </div>
      <div className="pr-pipelines" role="cell">
        <strong className={pipeline.success_rate !== null && pipeline.success_rate < 80 ? "is-warning" : undefined}>{pipeline.success_rate === null ? "—" : `${pipeline.success_rate}%`}</strong>
        <small>{pipeline.completed + pipeline.failed ? `${pipeline.completed} ok · ${pipeline.failed} failed` : "no pipelines"}</small>
      </div>
      <div className="pr-pipelines" role="cell">
        {repository.open_failed_jobs && repository.jobs_href
          ? <a className="is-danger" href={repository.jobs_href}>{pluralize(repository.open_failed_jobs, "failed job")}</a>
          : <span className="pr-muted">none</span>}
      </div>
      <div className="pr-actions" role="cell">
        {repository.href && <a href={repository.href}>Repository 360</a>}
      </div>
    </div>
  );
}
