import { useEffect, useMemo, useRef, useState } from "react";

import { SignalSelect } from "../SignalSelect";
import { EmptyState, relativeTime, useLiveData } from "../shared";
import { formatDate, percent, pluralize, scopeLabels, statusTones } from "./format";
import { SyncControl } from "./SyncControl";
import type {
  GroupRow,
  ProductRow,
  RegistryMode,
  RegistryPayload,
  ScopeFilter,
  StatusFilter,
} from "./types";
import "./products.css";

type Filters = {
  period: string;
  mode: RegistryMode;
  scope: ScopeFilter;
  status: StatusFilter;
  q: string;
};

const scopeOptions = [
  { value: "all", label: "All audiences" },
  { value: "internal", label: "Internal" },
  { value: "external", label: "External" },
  { value: "unclassified", label: "Audience not set" },
  { value: "conflict", label: "Audience conflict" },
];

function readFilters(): Filters {
  const params = new URLSearchParams(window.location.search);
  const period = /^\d{4}-Q[1-4]$/.test(params.get("period") ?? "") ? params.get("period")! : "";
  const mode = params.get("mode") === "total" ? "total" : "new";
  const scope = scopeOptions.some((option) => option.value === params.get("scope"))
    ? params.get("scope") as ScopeFilter
    : "all";
  const status = ["synced", "attention", "removed"].includes(params.get("status") ?? "")
    ? params.get("status") as StatusFilter
    : "all";
  return { period, mode, scope, status, q: params.get("q") ?? "" };
}

function filtersQuery(filters: Filters) {
  const params = new URLSearchParams();
  if (filters.period) {
    params.set("period", filters.period);
    params.set("mode", filters.mode);
  }
  if (filters.scope !== "all") params.set("scope", filters.scope);
  if (filters.status !== "all") params.set("status", filters.status);
  if (filters.q) params.set("q", filters.q);
  return params;
}

export function ProductsView() {
  const [filters, setFilters] = useState<Filters>(readFilters);
  const [searchDraft, setSearchDraft] = useState(filters.q);
  const searchTimer = useRef<number | null>(null);
  const query = filtersQuery(filters).toString();
  const { data, state, retry } = useLiveData<RegistryPayload>(
    `/api/ui/products${query ? `?${query}` : ""}`,
    60_000,
  );

  useEffect(() => {
    const onPopState = () => {
      const next = readFilters();
      setFilters(next);
      setSearchDraft(next.q);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const applyFilters = (next: Partial<Filters>, replace = false) => {
    const updated = { ...filters, ...next };
    const params = filtersQuery(updated);
    params.set("level", "product");
    const url = `/targets?${params.toString()}`;
    window.history[replace ? "replaceState" : "pushState"]({}, "", url);
    setFilters(updated);
  };

  const onSearch = (value: string) => {
    setSearchDraft(value);
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
    searchTimer.current = window.setTimeout(() => applyFilters({ q: value.trim() }, true), 300);
  };

  const periodOptions = useMemo(
    () => [
      { value: "", label: "Current registry" },
      ...(data?.periods ?? []).map((period) => ({ value: period.key, label: period.label })),
    ],
    [data?.periods],
  );
  const summary = data?.summary;
  const periodLabel = data?.query.period_label;
  const repositories = summary?.repositories ?? {};
  const scannedTotal = repositories.total ?? 0;

  const heading = filters.period
    ? filters.mode === "new"
      ? `Connected in ${periodLabel ?? filters.period.replace("-", " ")}`
      : `Connected by the end of ${periodLabel ?? filters.period.replace("-", " ")}`
    : "Current registry";

  return (
    <div className="view products-view">
      <header className="page-title">
        <div>
          <p>Product registry · ASOC</p>
          <h1>Products connected through ASOC.</h1>
          <span>A product is connected when its card appears in ASOC. ScanOps then syncs it with the GitLab group from the card and tracks scans across every repository in that group.</span>
        </div>
        {data && <SyncControl sync={data.sync} timezone={data.timezone} onSynced={retry} />}
      </header>

      <section className="filter-surface pr-filters" aria-label="Product registry filters">
        <label className="filter-field">
          <span>Period</span>
          <SignalSelect
            ariaLabel="Reporting period"
            value={filters.period}
            options={periodOptions}
            onChange={(value) => applyFilters({ period: value, status: value ? filters.status : filters.status === "removed" ? "all" : filters.status })}
          />
        </label>
        <div className="filter-field pr-mode" role="group" aria-label="Quarter slice">
          <span>Slice</span>
          <div className="pr-segmented">
            <button type="button" aria-pressed={filters.mode === "new"} disabled={!filters.period} onClick={() => applyFilters({ mode: "new" })}>New in quarter</button>
            <button type="button" aria-pressed={filters.mode === "total"} disabled={!filters.period} onClick={() => applyFilters({ mode: "total" })}>Total at quarter end</button>
          </div>
        </div>
        <label className="filter-field">
          <span>Audience</span>
          <SignalSelect
            ariaLabel="Product audience"
            value={filters.scope}
            options={scopeOptions}
            onChange={(value) => applyFilters({ scope: value as ScopeFilter })}
          />
        </label>
        <label className="filter-field pr-search">
          <span>Search</span>
          <input
            type="search"
            value={searchDraft}
            onChange={(event) => onSearch(event.target.value)}
            placeholder="Product, BI_ID or GitLab group"
            aria-label="Search products"
          />
        </label>
        <a
          className="quiet-button pr-export"
          href={data?.export_href ?? `/api/ui/products/export.xlsx${query ? `?${query}` : ""}`}
          aria-disabled={!data?.rows.length}
          download
        >
          Export XLSX <span>{data ? data.rows.length : "—"}</span>
        </a>
      </section>

      {summary && (
        <section className="pr-summary" aria-label={`${heading} summary`}>
          <SummaryTile
            label={filters.period ? heading : "All products"}
            value={summary.total}
            detail={filters.period && filters.mode === "total" && summary.new_in_period !== null
              ? `${summary.new_in_period} new in the quarter`
              : filters.period ? "created in ASOC in this quarter" : `${pluralize(data?.sync.removed_cards ?? 0, "removed card")} kept for reports`}
            active={filters.status === "all"}
            onSelect={() => applyFilters({ status: "all" })}
          />
          <SummaryTile
            label="Synced with ScanOps"
            value={summary.synced}
            detail={`${percent(summary.synced, summary.total)}% of products`}
            tone="succeeded"
            active={filters.status === "synced"}
            onSelect={() => applyFilters({ status: "synced" })}
          />
          <SummaryTile
            label="Need attention"
            value={summary.attention}
            detail={summary.overdue ? `${summary.overdue} waiting over ${summary.grace_days} days` : "no link, group not in ScanOps or conflict"}
            tone={summary.attention ? "failed" : undefined}
            active={filters.status === "attention"}
            onSelect={() => applyFilters({ status: "attention" })}
          />
          {(filters.period || summary.removed > 0) && (
            <SummaryTile
              label="Removed from ASOC"
              value={summary.removed}
              detail="kept in quarterly history"
              active={filters.status === "removed"}
              onSelect={() => applyFilters({ status: "removed" })}
            />
          )}
          <div className="pr-summary-tile pr-summary-info">
            <span>Repositories scanned</span>
            <strong>{(repositories.scanned ?? 0).toLocaleString()}<small> / {scannedTotal.toLocaleString()}</small></strong>
            <CoverageBar
              scanned={repositories.scanned ?? 0}
              notScanned={repositories.not_scanned ?? 0}
              inactive={repositories.inactive ?? 0}
              unknown={(repositories.unknown ?? 0) + (repositories.not_audited ?? 0)}
              total={scannedTotal}
            />
            <small>{repositories.not_scanned ?? 0} not scanned · {repositories.inactive ?? 0} without recent pipelines</small>
          </div>
          <div className="pr-summary-tile pr-summary-info">
            <span>Pipeline success · 30d</span>
            <strong>{summary.pipelines.success_rate === null ? "—" : `${summary.pipelines.success_rate}%`}</strong>
            <small>{summary.pipelines.completed.toLocaleString()} succeeded · {summary.pipelines.failed.toLocaleString()} failed</small>
          </div>
        </section>
      )}

      {!data && <EmptyState state={state} title="Product registry unavailable" copy="ScanOps could not read the ASOC product registry." onRetry={retry} />}

      {data && (
        <section className="pr-table-surface" aria-label="Products">
          <div className="pr-table" role="table" aria-label={heading}>
            <div className="pr-row pr-head" role="row">
              <span role="columnheader">Product</span>
              <span role="columnheader">Sync status</span>
              <span role="columnheader">GitLab group</span>
              <span role="columnheader">Repositories</span>
              <span role="columnheader">Pipelines · 30d</span>
              <span role="columnheader">Connected</span>
            </div>
            {data.rows.map((row) => <ProductTableRow row={row} timezone={data.timezone} key={row.key} />)}
            {!data.rows.length && (
              <p className="pr-empty" role="status">
                {data.summary.total
                  ? "No products match the selected status. Choose another summary tile or clear the search."
                  : filters.period
                    ? "No ASOC products were connected in this period."
                    : "No ASOC products are registered yet. Synchronize ASOC to load product cards."}
              </p>
            )}
          </div>
          {data.summary.undated > 0 && (
            <p className="pr-footnote">{pluralize(data.summary.undated, "ASOC card")} without a creation date cannot be placed in a quarter{filters.period ? " and are missing from this slice" : ""}.</p>
          )}
        </section>
      )}

      {data && !filters.period && data.unregistered_groups.length > 0 && (
        <UnregisteredGroups groups={data.unregistered_groups} />
      )}
    </div>
  );
}

function SummaryTile({ label, value, detail, tone, active, onSelect }: { label: string; value: number; detail: string; tone?: "succeeded" | "failed"; active: boolean; onSelect: () => void }) {
  return (
    <button type="button" className={`pr-summary-tile${active ? " is-active" : ""}${tone ? ` tone-${tone}` : ""}`} aria-pressed={active} onClick={onSelect}>
      <span>{label}</span>
      <strong>{value.toLocaleString()}</strong>
      <small>{detail}</small>
    </button>
  );
}

export function CoverageBar({ scanned, notScanned, inactive, unknown, total }: { scanned: number; notScanned: number; inactive: number; unknown: number; total: number }) {
  if (!total) return <span className="pr-bar is-empty" aria-hidden="true" />;
  const segment = (value: number) => ({ width: `${value / total * 100}%` });
  return (
    <span className="pr-bar" role="img" aria-label={`${scanned} scanned, ${notScanned} not scanned, ${inactive} without recent pipelines, ${unknown} not verified, of ${total}`}>
      <i className="is-scanned" style={segment(scanned)} />
      <i className="is-not-scanned" style={segment(notScanned)} />
      <i className="is-unknown" style={segment(unknown)} />
      <i className="is-inactive" style={segment(inactive)} />
    </span>
  );
}

function ProductTableRow({ row, timezone }: { row: ProductRow; timezone: string }) {
  const repos = row.repositories;
  const openDetail = (event: React.MouseEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest("a, button")) return;
    if (window.getSelection()?.toString()) return;
    window.location.assign(row.href);
  };
  const statusNote = row.status === "removed"
    ? `Removed ${formatDate(row.removed_at, timezone)}`
    : row.waiting_days !== null
      ? `Waiting ${pluralize(row.waiting_days, "day")}${row.overdue ? " · overdue" : ""}`
      : row.links_total > 1
        ? `${row.links_matched} of ${row.links_total} GitLab links`
        : null;
  return (
    <div className={`pr-row${row.overdue ? " is-overdue" : ""}`} role="row" onClick={openDetail}>
      <div className="pr-product" role="cell">
        <a href={row.href}>{row.name}</a>
        <small>
          {row.bi_id ? `BI_${row.bi_id}` : "No BI_ID"}
          <span className={`pr-scope scope-${row.scope}`}>{scopeLabels[row.scope] ?? row.scope}</span>
          {row.asoc_url && <a className="pr-external" href={row.asoc_url} target="_blank" rel="noopener noreferrer">ASOC ↗</a>}
        </small>
      </div>
      <div className="pr-status-cell" role="cell">
        <span className={`status status-${statusTones[row.status]}`} title={row.status_hint}><i />{row.status_label}</span>
        {statusNote && <small className={row.overdue ? "is-danger" : undefined}>{statusNote}</small>}
      </div>
      <div className="pr-groups" role="cell">
        {row.groups.length ? (
          <>
            {row.groups[0].url
              ? <a className="pr-external" href={row.groups[0].url} target="_blank" rel="noopener noreferrer" title={row.groups[0].path}>{row.groups[0].path || row.groups[0].name} ↗</a>
              : <span>{row.groups[0].path || row.groups[0].name}</span>}
            {row.groups.length > 1 && <small>+{row.groups.length - 1} more {row.groups.length === 2 ? "group" : "groups"}</small>}
          </>
        ) : <span className="pr-muted">{row.status === "no_gitlab_link" ? "No link on the ASOC card" : "Not resolved"}</span>}
      </div>
      <div className="pr-repos" role="cell">
        {repos.total ? (
          <>
            <strong>{repos.scanned}<small> / {repos.total} scanned</small></strong>
            <CoverageBar scanned={repos.scanned} notScanned={repos.not_scanned} inactive={repos.inactive} unknown={repos.unknown + repos.not_audited} total={repos.total} />
            <small title="Not scanned: active repositories without security jobs. Inactive: no pipelines in the last 30 days.">
              {[repos.not_scanned ? `${repos.not_scanned} not scanned` : null, repos.inactive ? `${repos.inactive} inactive` : null].filter(Boolean).join(" · ") || (repos.audited ? "every active repository scanned" : "GitLab audit pending")}
            </small>
          </>
        ) : <span className="pr-muted">—</span>}
      </div>
      <div className="pr-pipelines" role="cell">
        <strong className={row.pipelines.success_rate !== null && row.pipelines.success_rate < 80 ? "is-warning" : undefined}>
          {row.pipelines.success_rate === null ? "—" : `${row.pipelines.success_rate}%`}
        </strong>
        <small>{row.pipelines.repos_with_pipelines ? `${row.pipelines.repos_green}/${row.pipelines.repos_with_pipelines} repos green` : "no pipelines yet"}</small>
      </div>
      <div className="pr-connected" role="cell">
        <strong>{formatDate(row.connected_at, timezone)}</strong>
        <small>{row.connected_quarter ?? "date unknown"}</small>
      </div>
    </div>
  );
}

function UnregisteredGroups({ groups }: { groups: GroupRow[] }) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? groups : groups.slice(0, 5);
  return (
    <section className="pr-unregistered" aria-label="GitLab groups without an ASOC card">
      <div className="pr-section-heading">
        <div>
          <span>Monitored in ScanOps · missing from ASOC</span>
          <h2>{pluralize(groups.length, "GitLab group")} without an ASOC card</h2>
          <p>These groups send security jobs to ScanOps, but no ASOC card is connected to them — either no card links them, or the cards that do are in conflict. Open a group to see any card that points at it; otherwise create the ASOC card or add the group URL to an existing card.</p>
        </div>
      </div>
      <div className="pr-group-list">
        {visible.map((group) => (
          <a className="pr-group-row" href={group.href} key={group.key}>
            <span>
              <strong>{group.name}</strong>
              <small>{group.path}{group.asoc_conflict && <b className="pr-conflict-tag"> · ASOC card conflict</b>}</small>
            </span>
            <span>{group.repositories.total ? `${group.repositories.scanned} / ${group.repositories.total} repos scanned` : `${group.repositories.monitored} monitored repos`}</span>
            <span>{group.pipelines.success_rate === null ? "no pipelines" : `${group.pipelines.success_rate}% pipelines green`}</span>
            <span className="pr-muted">{relativeTime(group.pipelines.last_success_at)}</span>
          </a>
        ))}
      </div>
      {groups.length > 5 && (
        <button type="button" className="pr-link-button" onClick={() => setExpanded((value) => !value)}>
          {expanded ? "Show fewer" : `Show all ${groups.length}`}
        </button>
      )}
    </section>
  );
}
