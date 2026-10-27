"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { SearchableSelect, SignalSelect } from "./SignalSelect";
import {
  Aperture,
  EmptyState,
  LiveDataStatusContext,
  Status,
  relativeTime,
  statusTone,
  useLiveData,
  type LiveDataStatus,
  type LoadState,
} from "./shared";
import { ProductDetail } from "./products/ProductDetail";
import { ProductsView } from "./products/ProductsView";

type View =
  | "overview"
  | "duration"
  | "executions"
  | "jobs"
  | "pipelines"
  | "config"
  | "iac"
  | "ptai"
  | "failed"
  | "coverage"
  | "products"
  | "repositories"
  | "scanners"
  | "integrations";

type Topology = {
  target_count: number;
  gitlab_triggers: number;
  active_pipelines: number;
  success_rate: number;
  execution_completed: number;
  execution_failed: number;
  execution_total: number;
  execution_timeframe_label: string;
  pipeline_success_rate: number;
  pipeline_failure_rate: number;
  pipeline_completed: number;
  pipeline_failed: number;
  pipeline_total: number;
  normalized_outputs: number;
  failed_outputs: number;
  config_drift: number;
  asoc_confirmed_deliveries: number;
  asoc_mapped_products: number;
  total_products: number;
  last_execution_label: string;
  telemetry_state: "live" | "stale" | "empty";
};

type OverviewSignal = {
  tone: "stale" | "empty" | "problem" | "warning" | "healthy";
  title: string;
  context: string;
  action_label: string;
  action_href: string;
};

type OverviewData = {
  topology: Topology;
  attention: {
    failed_jobs: number;
    failed_jobs_in_window: number;
    config_drift: number;
    config_products_scanned: number;
    iac_critical_high: number;
    iac_repositories_total: number;
    iac_repositories_scanned: number;
    coverage_gaps: number;
    coverage_targets_total: number;
  };
  overview_signal: OverviewSignal;
  hotspots: {
    repository_id: number | null;
    repository: string;
    product: string | null;
    job: string;
    open_failures: number;
    last_failure_at: string | null;
    href: string;
  }[];
  posture: {
    audit_covered: number;
    audit_targets: number;
    audit_gaps: number;
    audit_rate: number | null;
    controls_expected: number;
    controls_configured: number;
    controls_missing: number;
    control_rate: number | null;
    iac_critical_high: number;
    iac_repositories_scanned: number;
    config_findings: number;
    config_products_scanned: number;
    asoc_mapped_products: number;
    total_products: number;
  };
  platform: {
    instances_total: number;
    instances_healthy: number;
    instances_problem: number;
    problems: {
      name: string;
      status: string;
      reason: string | null;
      last_seen_at: string | null;
    }[];
  };
  throughput: {
    label: string;
    range_label: string;
    start_at: string;
    end_at: string;
    is_current: boolean;
    succeeded: number;
    failed: number;
  }[];
  throughput_window_label: string;
  throughput_timezone: string;
  throughput_granularity: string;
  latency: {
    sample_count: number;
    average: number | null;
    average_label: string;
    median: number | null;
    median_label: string;
    p95: number | null;
    p95_label: string;
    p95_tail: number | null;
    p95_tail_label: string;
  };
  duration_breakdown: {
    pipeline: {
      count: number;
      median: number | null;
      median_label: string;
      average: number | null;
      average_label: string;
    };
    categories: {
      key: string;
      label: string;
      count: number;
      median: number | null;
      median_label: string;
      average: number | null;
      average_label: string;
    }[];
    jobs: {
      key: string;
      label: string;
      count: number;
      median: number | null;
      median_label: string;
      average: number | null;
      average_label: string;
    }[];
  };
};

type DurationSummary = {
  count: number;
  average: number | null;
  average_label: string;
  median: number | null;
  median_label: string;
  p95: number | null;
  p95_label: string;
};

type DurationRow = {
  key: string;
  label: string;
  count: number;
  median: number | null;
  median_label: string;
  average: number | null;
  average_label: string;
};

type DurationTrend = {
  mode: string;
  labels: string[];
  display_labels: string[];
  pipeline_avg: (number | null)[];
  job_avg: (number | null)[];
  pipeline_count: number[];
  job_count: number[];
};

type DurationPageData = {
  timeframe: OverviewTimeframe;
  pipeline: DurationSummary;
  jobs: DurationSummary;
  categories: DurationRow[];
  job_breakdown: DurationRow[];
  trend: DurationTrend | null;
};

type HealthData = {
  status: string;
  operational_status: string;
  degraded_reasons?: string[];
};

type HealthContextValue = {
  data: HealthData | null;
  state: LoadState;
  retry: () => void;
};

const HealthContext = createContext<HealthContextValue>({
  data: null,
  state: "loading",
  retry: () => undefined,
});

const views: { id: View; label: string; section: "OPERATE" | "UNDERSTAND" | "PLATFORM"; glyph: string; parent?: View; hidden?: boolean }[] = [
  { id: "overview", label: "Overview", section: "OPERATE", glyph: "⌁" },
  { id: "duration", label: "Execution duration", section: "OPERATE", glyph: "", hidden: true },
  { id: "executions", label: "Executions", section: "OPERATE", glyph: "≋" },
  { id: "jobs", label: "Job journal", section: "OPERATE", glyph: "↳", parent: "executions" },
  { id: "pipelines", label: "Active pipelines", section: "OPERATE", glyph: "↳", parent: "executions" },
  { id: "config", label: "Config Guard", section: "OPERATE", glyph: "↳", parent: "executions" },
  { id: "iac", label: "IaC scanning", section: "OPERATE", glyph: "↳", parent: "executions" },
  { id: "ptai", label: "PT AI", section: "OPERATE", glyph: "↳", parent: "executions" },
  { id: "failed", label: "Failed jobs", section: "OPERATE", glyph: "!" },
  { id: "coverage", label: "Coverage", section: "UNDERSTAND", glyph: "◫" },
  { id: "products", label: "Products", section: "UNDERSTAND", glyph: "◈" },
  { id: "repositories", label: "Repositories", section: "UNDERSTAND", glyph: "◇" },
  { id: "scanners", label: "Scanner fleet", section: "PLATFORM", glyph: "⌬" },
  { id: "integrations", label: "Integrations", section: "PLATFORM", glyph: "⌘" },
];

const viewRoutes: Record<View, string> = {
  overview: "/",
  duration: "/dashboard/duration",
  executions: "/executions",
  jobs: "/logs",
  pipelines: "/pipelines",
  config: "/config-scan",
  iac: "/iac",
  ptai: "/ptai-scans",
  failed: "/logs?status=failed",
  coverage: "/coverage",
  products: "/targets?level=product",
  repositories: "/targets?level=repository",
  scanners: "/scanners",
  integrations: "/integrations",
};

type PageSize = 25 | 50 | 100;
type OverviewTimeframe = "today" | "3d" | "7d" | "30d" | "1y" | "all";
type DurationCategory = "all" | "sast" | "sca" | "secrets" | "cs" | "asoc" | "mobsf";
const overviewTimeframes: OverviewTimeframe[] = ["today", "3d", "7d", "30d", "1y", "all"];
const overviewTimeframeOptions = [
  { value: "today", label: "Today" },
  { value: "3d", label: "Last 3 days" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "1y", label: "Last 12 months" },
  { value: "all", label: "All time" },
];
const durationCategoryOptions = [
  { value: "all", label: "All tools" },
  { value: "sast", label: "SAST" },
  { value: "sca", label: "SCA" },
  { value: "secrets", label: "Secrets" },
  { value: "cs", label: "CS" },
  { value: "asoc", label: "ASOC" },
  { value: "mobsf", label: "MobSF" },
];
const pageSizeOptions = [25, 50, 100].map((value) => ({
  value: String(value),
  label: String(value),
}));

function pageSizeFrom(value: string | null): PageSize {
  const parsed = Number(value);
  return parsed === 50 || parsed === 100 ? parsed : 25;
}

function overviewTimeframeFromLocation(): OverviewTimeframe {
  if (typeof window === "undefined") return "7d";
  const value = new URLSearchParams(window.location.search).get("timeframe");
  return overviewTimeframes.includes(value as OverviewTimeframe)
    ? value as OverviewTimeframe
    : "7d";
}

function durationCategoryFromLocation(): DurationCategory {
  if (typeof window === "undefined") return "all";
  const value = new URLSearchParams(window.location.search).get("tool");
  return durationCategoryOptions.some((option) => option.value === value)
    ? value as DurationCategory
    : "all";
}

function durationLabel(value?: number | null) {
  if (value === null || value === undefined) return "—";
  const seconds = Math.max(0, Math.round(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours ? `${hours}h ${minutes}m` : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function ageLabel(value?: number | null) {
  if (value == null) return "—";
  const days = Math.floor(value / 86_400);
  const hours = Math.floor((value % 86_400) / 3_600);
  if (days) return `${days}d ${hours}h`;
  return durationLabel(value);
}

function PaginationBar({
  ariaLabel,
  page,
  totalPages,
  pageSize,
  start,
  end,
  total,
  onPageChange,
  onPageSizeChange,
  embedded = false,
}: {
  ariaLabel: string;
  page: number;
  totalPages: number;
  pageSize: PageSize;
  start: number;
  end: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: PageSize) => void;
  embedded?: boolean;
}) {
  if (total <= 0) return null;
  return (
    <nav className={`surface-pagination unified-pagination${embedded ? " embedded" : ""}`} aria-label={`${ariaLabel} pages`}>
      <span className="pagination-range">{start}–{end} of {total}</span>
      <div className="pagination-navigation">
        <button type="button" aria-label="Previous page" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>← Previous</button>
        <span>Page {page} of {totalPages}</span>
        <button type="button" aria-label="Next page" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>Next →</button>
      </div>
      <label className="pagination-page-size">
        <span>Rows</span>
        <SignalSelect
          ariaLabel={`${ariaLabel} per page`}
          value={String(pageSize)}
          options={pageSizeOptions}
          onChange={(value) => onPageSizeChange(pageSizeFrom(value))}
        />
      </label>
    </nav>
  );
}

function FlowMap({ topology, timeframe }: { topology: Topology | null; timeframe: OverviewTimeframe }) {
  const stages: [string, string | number, string, string][] = [
    ["GitLab", topology?.gitlab_triggers ?? "—", "pipeline signals · selected window", `/pipelines?timeframe=${timeframe}`],
    ["Pipelines", topology?.active_pipelines ?? "—", "active now", `/pipelines?group=in_progress&timeframe=${timeframe}`],
    ["Controls", topology?.execution_total ?? "—", "terminal jobs · selected window", `/logs?timeframe=${timeframe}`],
    ["Evidence", topology?.normalized_outputs ?? "—", "normalized records · selected window", `/logs?timeframe=${timeframe}`],
    ["ASOC", topology?.asoc_confirmed_deliveries ?? "—", `${topology?.asoc_mapped_products ?? "—"} / ${topology?.total_products ?? "—"} products mapped`, "/targets?level=product"],
  ];
  const activeIndex = topology
    ? topology.asoc_confirmed_deliveries > 0
      ? 4
      : topology.normalized_outputs > 0
        ? 3
        : topology.execution_total > 0
          ? 2
          : topology.active_pipelines > 0
            ? 1
            : topology.gitlab_triggers > 0
              ? 0
              : -1
    : -1;
  return (
    <div className={`flow-map ${activeIndex < 0 ? "is-empty" : ""}`} aria-label="Observed execution delivery stages">
      <div className="flow-track" aria-hidden="true">
        <i className="packet packet-a" /><i className="packet packet-b" /><i className="packet packet-c" />
      </div>
      {stages.map(([label, value, meta, href], index) => (
        <a className={`flow-stage ${index === activeIndex ? "flow-stage-active" : ""}`} key={label} href={href} aria-label={`Open ${label} stage details`}>
          <span className="flow-node"><i /></span>
          <span className="flow-index">0{index + 1}</span>
          <strong>{label}</strong>
          <b>{value}</b>
          <small>{meta}</small>
        </a>
      ))}
    </div>
  );
}

function ThroughputChart({ data, windowLabel, timezone }: { data: OverviewData["throughput"], windowLabel: string, timezone: string }) {
  const maximum = Math.max(1, ...data.map((bucket) => bucket.succeeded + bucket.failed));
  const hasActivity = data.some((bucket) => bucket.succeeded + bucket.failed > 0);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const validActiveIndex = activeIndex !== null && activeIndex < data.length
    ? activeIndex
    : null;
  const activeBucket = validActiveIndex === null ? null : data[validActiveIndex];
  const observedIndex = data.findIndex((bucket) => bucket.is_current);
  const fallbackIndex = data.reduce((latest, bucket, index) => (
    bucket.succeeded + bucket.failed > 0 ? index : latest
  ), observedIndex >= 0 ? observedIndex : 0);
  const selectIndex = (index: number) => {
    if (!data.length) return;
    setActiveIndex(Math.max(0, Math.min(data.length - 1, index)));
  };
  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!data.length) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setActiveIndex(null);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const current = validActiveIndex ?? fallbackIndex;
    selectIndex(current + (event.key === "ArrowRight" ? 1 : -1));
  };
  return (
    <div
      className="chart"
      role="group"
      tabIndex={data.length ? 0 : undefined}
      aria-label={`Job outcomes · ${windowLabel} · ${timezone}`}
      onKeyDown={handleKeyDown}
      onMouseLeave={() => setActiveIndex(null)}
      onFocus={() => {
        if (validActiveIndex === null && data.length) setActiveIndex(fallbackIndex);
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setActiveIndex(null);
      }}
    >
      <div className="chart-grid">{[1, 2, 3, 4].map((line) => <i key={line} />)}</div>
      {!hasActivity && <div className="chart-empty">No executions in this window</div>}
      <div className="bar-field">
        {data.map((bucket, index) => {
          const total = bucket.succeeded + bucket.failed;
          return (
          <span
            key={bucket.start_at}
            aria-hidden="true"
            onMouseEnter={() => selectIndex(index)}
            className={`${bucket.is_current ? "current" : ""} ${index === validActiveIndex ? "is-active" : ""}`}
            style={{ "--h": total ? `${Math.max(3, (total / maximum) * 126)}px` : "0px", "--delay": `${index * 20}ms` } as React.CSSProperties}
          >
            <i style={{ height: `${total ? Math.max(3, (bucket.failed / total) * 100) : 0}%` }} />
          </span>
        )})}
      </div>
      {activeBucket && (
        <div
          className="chart-tooltip"
          role="status"
          style={{ "--position": `${((validActiveIndex ?? 0) + 0.5) / Math.max(1, data.length) * 100}%` } as React.CSSProperties}
        >
          <strong>{activeBucket.range_label}</strong>
          <span><i className="tooltip-total" />Total <b>{activeBucket.succeeded + activeBucket.failed}</b></span>
          <span><i className="tooltip-success" />Succeeded <b>{activeBucket.succeeded}</b></span>
          <span><i className="tooltip-failed" />Failed <b>{activeBucket.failed}</b></span>
        </div>
      )}
      <div className="chart-axis">
        {(() => {
          const labelStep = Math.max(1, Math.ceil(data.length / 5));
          const lastIndex = data.length - 1;
          return data.map((bucket, index) => {
          const hasForcedEndLabel = lastIndex % labelStep !== 0;
          const isTooCloseToForcedEnd = hasForcedEndLabel && index !== lastIndex && lastIndex - index < labelStep;
          if ((index % labelStep !== 0 && index !== lastIndex) || isTooCloseToForcedEnd) return null;
          const positionClass = index === 0 ? "is-start" : index === lastIndex ? "is-end" : "";
          return <span className={positionClass} key={bucket.start_at} style={{ left: `${(index / Math.max(1, lastIndex)) * 100}%` }}>{bucket.label}</span>;
          });
        })()}
      </div>
    </div>
  );
}

function DurationTrendChart({ trend }: { trend: DurationTrend | null }) {
  if (!trend) {
    return <div className="duration-trend-empty">Trend is available for a bounded observation window.</div>;
  }
  const values = [...trend.pipeline_avg, ...trend.job_avg].filter(
    (value): value is number => value != null,
  );
  if (!values.length) {
    return <div className="duration-trend-empty">No completed durations in this window.</div>;
  }
  const width = 860;
  const height = 220;
  const padding = { top: 18, right: 18, bottom: 34, left: 42 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const maximum = Math.max(1, ...values);
  const xFor = (index: number) => padding.left + (index / Math.max(1, trend.display_labels.length - 1)) * plotWidth;
  const yFor = (value: number) => padding.top + (1 - value / maximum) * plotHeight;
  const pointsFor = (series: (number | null)[]) => series
    .map((value, index) => value == null ? null : `${xFor(index)},${yFor(value)}`)
    .filter((point): point is string => point !== null)
    .join(" ");
  const labelIndexes = [0, Math.floor((trend.display_labels.length - 1) / 2), trend.display_labels.length - 1]
    .filter((value, index, array) => array.indexOf(value) === index);
  const accessibleLabel = `Average duration trend, ${trend.display_labels.length} ${trend.mode} buckets`;
  return (
    <div className="duration-trend-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-labelledby="duration-trend-title"
        aria-describedby="duration-trend-data"
      >
        <title id="duration-trend-title">{accessibleLabel}</title>
        {[0, .33, .66, 1].map((ratio) => (
          <line
            key={ratio}
            x1={padding.left}
            x2={width - padding.right}
            y1={padding.top + ratio * plotHeight}
            y2={padding.top + ratio * plotHeight}
            className="duration-grid-line"
          />
        ))}
        <polyline points={pointsFor(trend.pipeline_avg)} className="duration-trend-line pipeline" />
        <polyline points={pointsFor(trend.job_avg)} className="duration-trend-line jobs" />
        {trend.pipeline_avg.map((value, index) => value == null ? null : (
          <circle
            key={`pipeline-${index}`}
            cx={xFor(index)}
            cy={yFor(value)}
            r="3.5"
            className="duration-trend-point pipeline"
          >
            <title>{`${trend.display_labels[index]} · pipeline average ${durationLabel(value * 60)}`}</title>
          </circle>
        ))}
        {trend.job_avg.map((value, index) => value == null ? null : (
          <circle
            key={`job-${index}`}
            cx={xFor(index)}
            cy={yFor(value)}
            r="3"
            className="duration-trend-point jobs"
          >
            <title>{`${trend.display_labels[index]} · job average ${durationLabel(value * 60)}`}</title>
          </circle>
        ))}
        {labelIndexes.map((index) => (
          <text key={index} x={xFor(index)} y={height - 8} className="duration-trend-label" textAnchor={index === 0 ? "start" : index === trend.display_labels.length - 1 ? "end" : "middle"}>
            {trend.display_labels[index]}
          </text>
        ))}
      </svg>
      <div className="duration-trend-legend" aria-hidden="true">
        <span><i className="pipeline" />Pipeline average</span>
        <span><i className="jobs" />Job average</span>
      </div>
      <table className="sr-only" id="duration-trend-data">
        <caption>Average durations for each {trend.mode} bucket</caption>
        <thead>
          <tr><th scope="col">Bucket</th><th scope="col">Pipeline average</th><th scope="col">Job average</th></tr>
        </thead>
        <tbody>
          {trend.display_labels.map((label, index) => (
            <tr key={`${label}-${index}`}>
              <th scope="row">{label}</th>
              <td>{trend.pipeline_avg[index] == null ? "No data" : durationLabel(trend.pipeline_avg[index] * 60)}</td>
              <td>{trend.job_avg[index] == null ? "No data" : durationLabel(trend.job_avg[index] * 60)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ExecutionDuration() {
  const [timeframe, setTimeframe] = useState<OverviewTimeframe>(overviewTimeframeFromLocation);
  const [category, setCategory] = useState<DurationCategory>(durationCategoryFromLocation);
  const {
    data,
    state: loadState,
    retry,
  } = useLiveData<DurationPageData>(`/api/ui/duration?timeframe=${timeframe}&tool=${category}`);

  useEffect(() => {
    const syncTimeframeFromUrl = () => {
      setTimeframe(overviewTimeframeFromLocation());
      setCategory(durationCategoryFromLocation());
    };
    window.addEventListener("popstate", syncTimeframeFromUrl);
    return () => window.removeEventListener("popstate", syncTimeframeFromUrl);
  }, []);

  const selectTimeframe = (value: OverviewTimeframe) => {
    setTimeframe(value);
    const url = new URL(window.location.href);
    if (value === "7d") url.searchParams.delete("timeframe");
    else url.searchParams.set("timeframe", value);
    window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
  };
  const selectCategory = (value: DurationCategory) => {
    setCategory(value);
    const url = new URL(window.location.href);
    if (value === "all") url.searchParams.delete("tool");
    else url.searchParams.set("tool", value);
    window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
  };
  const durationRows = data?.job_breakdown ?? [];
  const durationScale = Math.max(1, ...durationRows.map((row) => row.average ?? 0));

  return (
    <div className="view duration-view">
      <header className="page-title duration-page-title">
        <div>
          <a className="duration-back-link" href="/">← Overview</a>
          <p>Performance / execution</p>
          <h1>Pipeline duration</h1>
          <span>End-to-end pipeline time with the individual job runtimes that make it up.</span>
        </div>
        <div className="header-actions">
          <SignalSelect
            className="observation-window"
            ariaLabel="Duration scanner family"
            value={category}
            options={durationCategoryOptions}
            onChange={(value) => selectCategory(value as DurationCategory)}
          />
          <SignalSelect
            className="observation-window"
            ariaLabel="Duration timeframe"
            value={timeframe}
            options={overviewTimeframeOptions}
            onChange={(value) => selectTimeframe(value as OverviewTimeframe)}
          />
        </div>
      </header>

      {loadState === "error" && (
        <EmptyState
          state={loadState}
          title="Duration data unavailable"
          copy="ScanOps could not read completed pipeline durations."
          onRetry={retry}
        />
      )}
      {loadState !== "error" && (
        <>
          <section className="duration-hero-grid">
            <article className="duration-focus-panel">
              <div className="panel-heading">
                <div><span className="panel-label">AVERAGE PIPELINE</span><h2>Time from trigger to delivery</h2></div>
                <span className="duration-window-label">{data?.timeframe === "all" ? "All retained history" : "Selected window"}</span>
              </div>
              <div className="duration-focus-layout">
                <div className="duration-average-orbit" aria-label={`${data?.pipeline.average_label ?? "No"} average pipeline duration`}>
                  <span>Average</span>
                  <strong>{data?.pipeline.average_label ?? "—"}</strong>
                  <small>pipeline</small>
                </div>
                <div className="duration-focus-copy">
                  <strong>{data?.pipeline.count.toLocaleString() ?? "—"} terminal pipelines</strong>
                  <p>One end-to-end measurement from the selected observation window. It is the number to use when judging overall pipeline speed.</p>
                  <div className="duration-focus-facts">
                    <span><small>Median</small><b>{data?.pipeline.median_label ?? "—"}</b></span>
                    <span><small>P95</small><b>{data?.pipeline.p95_label ?? "—"}</b></span>
                  </div>
                </div>
              </div>
            </article>
            <aside className="duration-metrics-panel">
              <div className="panel-heading"><div><span className="panel-label">JOB CONTEXT</span><h2>Runtime reference</h2></div></div>
              <div className="duration-metric-list">
                <div><span>Average job runtime</span><b>{data?.jobs.average_label ?? "—"}</b></div>
                <div><span>Median job runtime</span><b>{data?.jobs.median_label ?? "—"}</b></div>
                <div><span>Job samples</span><b>{data?.jobs.count.toLocaleString() ?? "—"}</b></div>
              </div>
              <p className="duration-method-note">Job runtimes are independent observations; parallel jobs are not added together.</p>
            </aside>
          </section>

          <section className="duration-detail-grid">
            <article className="duration-trend-panel">
              <div className="panel-heading">
                <div><span className="panel-label">TIME SERIES</span><h2>Average duration trend</h2></div>
                <span className="duration-window-label">Minutes per bucket</span>
              </div>
              <DurationTrendChart trend={data?.trend ?? null} />
            </article>
            <article className="duration-breakdown-panel">
              <div className="panel-heading">
                <div><span className="panel-label">JOB RUNTIMES</span><h2>By job</h2></div>
                <span className="duration-window-label">{durationRows.length} observed</span>
              </div>
              <div className="duration-job-list">
                {durationRows.length ? durationRows.map((row) => (
                  <div className="duration-job-row" key={row.key}>
                    <span className="duration-job-name"><strong>{row.label}</strong><small>{row.count.toLocaleString()} samples</small></span>
                    <span className="duration-job-bar" aria-hidden="true"><i style={{ width: `${Math.max(4, ((row.average ?? 0) / durationScale) * 100)}%` }} /></span>
                    <span className="duration-job-value"><b>{row.average_label}</b><small>average</small></span>
                    <span className="duration-job-value"><b>{row.median_label}</b><small>median</small></span>
                  </div>
                )) : <div className="duration-trend-empty">No completed job durations in this window.</div>}
              </div>
            </article>
          </section>
        </>
      )}
    </div>
  );
}

function Overview() {
  const health = useContext(HealthContext);
  const [timeframe, setTimeframe] = useState<OverviewTimeframe>(
    overviewTimeframeFromLocation,
  );
  const {
    data,
    state: loadState,
    retry,
  } = useLiveData<OverviewData>(`/api/ui/overview?timeframe=${timeframe}`);
  const [pulse, setPulse] = useState(1);

  useEffect(() => {
    const syncTimeframeFromUrl = () => setTimeframe(overviewTimeframeFromLocation());
    window.addEventListener("popstate", syncTimeframeFromUrl);
    return () => window.removeEventListener("popstate", syncTimeframeFromUrl);
  }, []);

  useEffect(() => {
    if (loadState !== "loading") return;
    const timer = window.setInterval(
      () => setPulse((value) => value >= 9 ? 1 : value + 1),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [loadState]);

  const topology = data?.topology ?? null;
  const attentionCategoryCount = data
    ? [
      data.attention.failed_jobs,
      data.attention.config_drift,
      data.attention.iac_critical_high,
      data.attention.coverage_gaps,
    ].filter((value) => value > 0).length
    : 0;
  const headline = loadState === "error"
    ? "Signal is unavailable."
    : loadState === "stale"
      || topology?.telemetry_state === "stale"
      || attentionCategoryCount > 0
      ? "Signal needs attention."
      : topology?.telemetry_state === "live"
        ? "Everything is moving."
        : "Waiting for first signal.";
  const statusLabel = loadState === "error"
    ? "Offline"
    : loadState === "stale"
      ? "Stale"
    : topology?.telemetry_state === "live"
      ? "Telemetry live"
      : topology?.telemetry_state === "stale"
      ? "Stale"
        : "Waiting";
  const signalState = loadState === "error"
    ? "offline"
    : loadState === "stale"
      ? "stale"
    : topology?.telemetry_state ?? "waiting";
  const overallDuration = data?.duration_breakdown?.pipeline;
  const hasJobOutcomeSample = Boolean(topology && topology.execution_total > 0);
  const runtimeDegraded = health.state === "error"
    || health.data?.operational_status === "degraded"
    || health.data?.operational_status === "unhealthy";
  const showPlatformHealth = Boolean(
    data && (runtimeDegraded || data.platform.instances_problem > 0),
  );
  const selectTimeframe = (value: OverviewTimeframe) => {
    setTimeframe(value);
    const url = new URL(window.location.href);
    if (value === "7d") url.searchParams.delete("timeframe");
    else url.searchParams.set("timeframe", value);
    window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
  };

  return (
    <div className="view">
      <header className="page-title">
        <div>
          <p>Operational signal</p>
          <h1>{headline}</h1>
          <span>Security execution across {topology?.total_products ?? "—"} products and {topology?.target_count ?? "—"} targets.</span>
        </div>
        <div className="header-actions">
          <SignalSelect
            className="observation-window"
            ariaLabel="Overview timeframe"
            value={timeframe}
            options={overviewTimeframeOptions}
            onChange={(value) => selectTimeframe(value as OverviewTimeframe)}
          />
          <span className={`primary-button is-static signal-state signal-${signalState}`}><i /> {statusLabel}</span>
        </div>
      </header>
      {data?.overview_signal && (
        <section className={`overview-action-strip tone-${data.overview_signal.tone}`} aria-label="Next operator action">
          <div className="overview-action-copy">
            <span className="panel-label">NEXT ACTION</span>
            <strong>{data.overview_signal.title}</strong>
            <small>{data.overview_signal.context}</small>
          </div>
          <a className="overview-action-link" href={data.overview_signal.action_href}>{data.overview_signal.action_label} <span>↗</span></a>
        </section>
      )}
      {!data && (
        <EmptyState
          state={loadState}
          title={loadState === "error" ? "Overview unavailable" : "Loading operational signal"}
          copy="ScanOps could not read the current operational signal."
          onRetry={retry}
        />
      )}

      {data && <>
      <section className="overview-grid">
        <article className="signal-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-label">SYSTEM PULSE{loadState === "stale" ? " · LAST CONFIRMED" : ""}</span>
              <h2>Execution river</h2>
            </div>
            <span className={`sync-note ${loadState === "error" || loadState === "stale" ? "is-attention" : ""}`}><i /> {loadState === "error" ? "Connection failed" : loadState === "stale" ? "Refresh paused" : topology ? `Signal ${topology.last_execution_label}` : `Updating ${pulse}s`}</span>
          </div>
          <div className="pulse-stats">
            <div className={`hero-stat ${topology?.telemetry_state === "stale" ? "is-stale" : ""} ${!hasJobOutcomeSample ? "is-empty" : ""}`}>
              <strong>{hasJobOutcomeSample ? topology?.success_rate : "—"}{hasJobOutcomeSample && <small>%</small>}</strong>
              <span>
                {hasJobOutcomeSample
                  ? `${topology?.execution_completed.toLocaleString()} / ${topology?.execution_total.toLocaleString()} successful jobs · ${topology?.execution_timeframe_label}`
                  : `No terminal jobs · ${topology?.execution_timeframe_label ?? "Last 7 days"}`}
              </span>
            </div>
            <div><strong>{topology?.execution_completed.toLocaleString() ?? "—"}</strong><span>Successful jobs</span></div>
            <div className={`danger-stat ${!topology ? "is-empty" : ""}`}><strong>{topology?.execution_failed.toLocaleString() ?? "—"}</strong><span>Failed jobs · {topology?.execution_timeframe_label ?? "Last 7 days"}</span></div>
            <div className={`danger-stat ${!topology ? "is-empty" : ""}`}><strong>{topology?.pipeline_failed.toLocaleString() ?? "—"}</strong><span><a href={`/pipelines?status=failed&timeframe=${timeframe}`}>Pipeline failures</a> · {topology?.pipeline_total.toLocaleString() ?? "—"} terminal</span></div>
          </div>
          <FlowMap topology={topology} timeframe={timeframe} />
          <div className="flow-caption"><span>Source signal</span><b>Active control plane</b><span>Verified evidence</span></div>
        </article>

        <aside className={`attention ${!data ? "is-unavailable" : ""}`}>
          <div className="panel-heading">
            <div><span className="panel-label">ATTENTION · NEEDS REVIEW</span><h2>Resolve next</h2></div>
            <b className="attention-count" aria-label={data ? `${attentionCategoryCount} attention categories` : "Attention unavailable"}>{data ? attentionCategoryCount : "—"}</b>
          </div>
          <a className="attention-item attention-item--inline-count is-hot" href={`/logs?status=failed&timeframe=${timeframe}`}>
            <span className="attention-icon">!</span>
            <span><strong>Failed jobs</strong><small>{data ? data.attention.failed_jobs.toLocaleString() : "—"} need attention</small></span>
          </a>
          <a className="attention-item" href="/config-scan">
            <span className="attention-icon">↯</span>
            <span><strong>Config drift</strong><small>{data?.attention.config_products_scanned ?? "—"} scanned products</small></span>
            <b>{data ? data.attention.config_drift.toLocaleString() : "—"}</b>
          </a>
          <a className="attention-item" href="/iac">
            <span className="attention-icon">△</span>
            <span><strong>IaC critical / high</strong><small>{data?.attention.iac_repositories_scanned ?? "—"} scanned repositories</small></span>
            <b>{data ? data.attention.iac_critical_high.toLocaleString() : "—"}</b>
          </a>
          <a className="attention-item" href="/coverage">
            <span className="attention-icon">◇</span>
            <span><strong>Coverage gaps</strong><small>{data?.attention.coverage_targets_total ?? "—"} audited targets</small></span>
            <b>{data ? data.attention.coverage_gaps.toLocaleString() : "—"}</b>
          </a>
          <div className={`calm-note ${!data ? "is-unavailable" : ""}`}><i /> {!data ? "Prioritization unavailable" : attentionCategoryCount === 0 ? "No critical delivery backlog" : `${attentionCategoryCount} categories need attention`}</div>
        </aside>
      </section>

      <section className="telemetry">
        <article className="throughput-panel">
          <div className="panel-heading">
            <div><span className="panel-label">JOB OUTCOMES</span><span className="chart-window-label">{data?.throughput_window_label ?? "Selected window"} · {data?.throughput_timezone ?? "local time"}</span><h2>Signal velocity</h2></div>
            <div className="legend"><span><i className="ok" />Succeeded</span><span><i className="bad" />Failed</span></div>
          </div>
          <ThroughputChart key={timeframe} data={data?.throughput ?? []} windowLabel={data?.throughput_window_label ?? "Selected window"} timezone={data?.throughput_timezone ?? "local time"} />
        </article>
        <article className="duration-panel">
          <div className="panel-heading">
            <div><span className="panel-label">PIPELINE DURATION</span><h2>Pipeline duration</h2></div>
            <a className="duration-detail-link" aria-label="Open pipeline duration details" href={`/dashboard/duration?timeframe=${timeframe}`}>
              <span>Open details</span><span aria-hidden="true">↗</span>
            </a>
          </div>
          <div className="duration-card-content">
            <div className="duration-card-orbit" aria-label={`${overallDuration?.average_label ?? "No"} average pipeline duration`}>
              <span>Average pipeline</span>
              <strong>{overallDuration?.average_label ?? "—"}</strong>
              <small>{overallDuration?.count?.toLocaleString() ?? "—"} terminal samples</small>
            </div>
            <div className="duration-card-facts">
              <span><small>Selected window</small><b>{timeframe === "all" ? "All retained history" : overviewTimeframeOptions.find((option) => option.value === timeframe)?.label ?? "Selected window"}</b></span>
              <span><small>Median pipeline</small><b>{overallDuration?.median_label ?? "—"}</b></span>
            </div>
          </div>
        </article>
      </section>
      <section className="overview-detail-grid">
        <article className="hotspot-panel">
          <div className="panel-heading">
            <div><span className="panel-label">UNRESOLVED FAILURE QUEUE</span><h2>Failure hotspots</h2></div>
            <a className="river-text-link" href={`/logs?status=failed&timeframe=${timeframe}`}>Open queue ↗</a>
          </div>
          <div className="hotspot-list">
            {data?.hotspots.length ? data.hotspots.map((hotspot) => (
              <a className="hotspot-row" href={hotspot.href} key={`${hotspot.repository_id}-${hotspot.job}`}>
                <span className="hotspot-signal" aria-hidden="true"><i /></span>
                <span className="hotspot-copy"><strong>{hotspot.repository}</strong><small>{hotspot.product || "Unassigned product"} · {hotspot.job}</small></span>
                <b>{hotspot.open_failures}</b>
                <time dateTime={hotspot.last_failure_at || undefined}>{relativeTime(hotspot.last_failure_at)}</time>
                <span className="hotspot-open">Open ↗</span>
              </a>
            )) : <div className="overview-inline-empty">No unresolved failure hotspots in this window.</div>}
          </div>
        </article>
        <article className="posture-panel">
          <div className="panel-heading">
            <div><span className="panel-label">CURRENT POSTURE</span><h2>Posture snapshot</h2></div>
            <a className="river-text-link" href="/coverage">Coverage ↗</a>
          </div>
          <div className="posture-list">
            <a href="/coverage"><span><strong>Pipeline coverage</strong><small>{data?.posture.audit_covered ?? "—"} of {data?.posture.audit_targets ?? "—"} audited targets</small></span><b>{data?.posture.audit_rate == null ? "—" : `${data.posture.audit_rate}%`}</b></a>
            <a href="/coverage"><span><strong>Control bindings</strong><small>{data?.posture.controls_configured ?? "—"} of {data?.posture.controls_expected ?? "—"} expected</small></span><b>{data?.posture.control_rate == null ? "—" : `${data.posture.control_rate}%`}</b></a>
            <a href="/iac"><span><strong>IaC critical / high</strong><small>{data?.posture.iac_repositories_scanned ?? "—"} repositories scanned</small></span><b className={data?.posture.iac_critical_high ? "is-danger" : ""}>{data?.posture.iac_critical_high ?? "—"}</b></a>
            <a href="/config-scan"><span><strong>Config drift</strong><small>{data?.posture.config_products_scanned ?? "—"} products scanned</small></span><b className={data?.posture.config_findings ? "is-warning" : ""}>{data?.posture.config_findings ?? "—"}</b></a>
            <a href="/targets?level=product"><span><strong>ASOC mapping</strong><small>Confirmed delivery in selected window</small></span><b>{data?.posture.asoc_mapped_products ?? "—"}/{data?.posture.total_products ?? "—"}</b></a>
          </div>
        </article>
      </section>
      {showPlatformHealth && data?.platform && (
        <section className="platform-health-panel" aria-label="Platform health">
          <div className="platform-health-copy">
            <span className="panel-label">PLATFORM HEALTH</span>
            <strong>{health.state === "error" ? "Runtime health unavailable" : "Platform needs review"}</strong>
            <small>{health.data?.degraded_reasons?.[0] || `${data.platform.instances_problem} scanner instances need attention.`}</small>
          </div>
          <div className="platform-health-metric"><b>{data.platform.instances_healthy}/{data.platform.instances_total}</b><span>instances healthy</span></div>
          <div className="platform-health-problems">
            {data.platform.problems.map((problem) => <a href="/scanners" key={`${problem.name}-${problem.status}`}><i className={`status-dot status-dot-${statusTone(problem.status)}`} /><span><strong>{problem.name}</strong><small>{problem.reason || problem.status}</small></span><b>↗</b></a>)}
          </div>
          <a className="overview-action-link" href="/scanners">Open Scanner Fleet <span>↗</span></a>
        </section>
      )}
      </>}
    </div>
  );
}

function Executions() {
  type Row = {
    id: number; reference: string; source_label: string; status: string;
    repository: string | null; product: string | null; correlation_key: string;
    requested_at: string | null; started_at: string | null; duration_seconds: number | null; href: string;
    steps: { id: number; name: string; source: string; status: string; failure_reason: string | null; duration_seconds: number | null }[];
    artifacts: { id: number; kind: string; status: string; size_bytes: number; deliveries: { id: number; status: string }[] }[];
  };
  type Payload = {
    rows: Row[];
    counts: Record<string, number>;
    repository_filter: number | null;
    matching: number;
    pagination: { page: number; page_size: PageSize; total_pages: number; start: number; end: number };
  };
  const executionStatuses = ["all", "running", "failed", "succeeded"] as const;
  const readExecutionFilters = () => {
    const params = new URLSearchParams(window.location.search);
    const repository = Number(params.get("repository"));
    const statusParam = params.get("status")?.toLowerCase() ?? "all";
    return {
      repository: Number.isInteger(repository) && repository > 0 ? repository : null,
      status: executionStatuses.includes(statusParam as typeof executionStatuses[number])
        ? statusParam
        : "all",
    };
  };
  const readRepositoryFilter = () => {
    const value = Number(new URLSearchParams(window.location.search).get("repository"));
    return Number.isInteger(value) && value > 0 ? value : null;
  };
  const readFilters = () => {
    const initial = readExecutionFilters();
    const params = new URLSearchParams(window.location.search);
    return {
      status: initial.status,
      page: Math.max(1, Number(params.get("page")) || 1),
      pageSize: pageSizeFrom(params.get("page_size")),
    };
  };
  const [filters, setFilters] = useState(readFilters);
  const [repositoryFilter, setRepositoryFilter] = useState<number | null>(readRepositoryFilter);
  const executionQuery = new URLSearchParams({
    status: filters.status,
    page: String(filters.page),
    page_size: String(filters.pageSize),
  });
  if (repositoryFilter) executionQuery.set("repository", String(repositoryFilter));
  const { data, state, retry } = useLiveData<Payload>(`/api/ui/executions?${executionQuery.toString()}`);
  const [selected, setSelected] = useState<number | null>(null);
  const selectedRow = data?.rows.find((row) => row.id === selected) ?? data?.rows[0] ?? null;
  const scopedRepository = data?.repository_filter ?? repositoryFilter;

  useEffect(() => {
    if (data?.rows.length && !data.rows.some((row) => row.id === selected)) setSelected(data.rows[0].id);
  }, [data, selected]);

  useEffect(() => {
    const effectivePage = data?.pagination?.page;
    if (!effectivePage || effectivePage === filters.page) return;
    const url = new URL(window.location.href);
    if (effectivePage > 1) url.searchParams.set("page", String(effectivePage));
    else url.searchParams.delete("page");
    window.history.replaceState({}, "", url);
    setFilters((current) => ({ ...current, page: effectivePage }));
  }, [data?.pagination?.page, filters.page]);

  useEffect(() => {
    const normalizeLocation = (next: ReturnType<typeof readFilters>, repository: number | null) => {
      const url = new URL(window.location.href);
      if (next.status === "all") url.searchParams.delete("status");
      else url.searchParams.set("status", next.status);
      if (repository === null) url.searchParams.delete("repository");
      else url.searchParams.set("repository", String(repository));
      if (next.page > 1) url.searchParams.set("page", String(next.page));
      else url.searchParams.delete("page");
      if (next.pageSize === 25) url.searchParams.delete("page_size");
      else url.searchParams.set("page_size", String(next.pageSize));
      const canonical = `${url.pathname}${url.search}${url.hash}`;
      const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (canonical !== current) window.history.replaceState({}, "", canonical);
    };
    const onPopState = () => {
      const next = readFilters();
      const repository = readRepositoryFilter();
      normalizeLocation(next, repository);
      setFilters(next);
      setRepositoryFilter(repository);
    };
    normalizeLocation(filters, repositoryFilter);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const selectExecutionFilter = (status: string) => {
    applyFilters({ status, page: 1 });
  };

  const clearRepositoryScope = () => {
    const url = new URL(window.location.href);
    url.searchParams.delete("repository");
    url.searchParams.delete("page");
    window.history.pushState({}, "", url);
    setRepositoryFilter(null);
    setFilters((current) => ({ ...current, page: 1 }));
  };

  const applyFilters = (next: Partial<typeof filters>) => {
    const updated = { ...filters, ...next };
    const url = new URL(window.location.href);
    if (updated.status === "all") url.searchParams.delete("status");
    else url.searchParams.set("status", updated.status);
    if (updated.page > 1) url.searchParams.set("page", String(updated.page));
    else url.searchParams.delete("page");
    if (updated.pageSize === 25) url.searchParams.delete("page_size");
    else url.searchParams.set("page_size", String(updated.pageSize));
    window.history.pushState({}, "", url);
    setFilters(updated);
  };

  const selectExecution = (id: number) => {
    setSelected(id);
    if (!window.matchMedia("(max-width: 1120px)").matches) return;
    window.setTimeout(() => {
      const detail = document.getElementById("execution-detail");
      const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
      detail?.scrollIntoView({ behavior, block: "start" });
      detail?.focus({ preventScroll: true });
    }, 0);
  };

  return (
    <div className="view">
      <header className="page-title">
        <div><p>Execution control</p><h1>Live activity.</h1><span>Durable executions, observed steps, normalized artifacts and delivery confirmations.</span></div>
        <div className="header-actions"><a className="quiet-button" href="/logs">Job history <span>↗</span></a><a className="primary-button" href="/pipelines">Open pipelines</a></div>
      </header>
      <div className="filter-row" aria-label="Execution filters">
        {executionStatuses.map((value) => <button type="button" aria-pressed={filters.status === value} className={filters.status === value ? "active" : ""} onClick={() => selectExecutionFilter(value)} key={value}>{value === "all" ? "All" : value} <b>{data?.counts[value] ?? 0}</b></button>)}
        {scopedRepository
          ? <div className="execution-scope" role="status"><span>Repository #{scopedRepository}</span><button onClick={clearRepositoryScope}>All executions</button></div>
          : <span />}
        <span className="live-filter-note">{data?.rows.length ?? 0} visible · {state === "stale" ? "refresh paused" : "auto-refreshing"}</span>
      </div>
      {!selectedRow && <EmptyState state={state} title="No executions match this view" copy="Execution telemetry will appear after the first accepted lifecycle event." onRetry={retry} />}
      {selectedRow && <section className="execution-workbench">
        <div className="execution-list">
          <div className="table-head"><span>Execution</span><span>Control</span><span>Started</span><span>Duration</span><span>Status</span></div>
          {data?.rows.map((row) => (
            <button
              type="button"
              className={`execution-row ${selectedRow.id === row.id ? "selected" : ""}`}
              aria-pressed={selectedRow.id === row.id}
              aria-controls="execution-detail"
              onClick={() => selectExecution(row.id)}
              key={row.id}
            >
              <span className="repo-cell"><i className={`repo-signal ${statusTone(row.status)}`} /><span><strong>{row.repository ?? row.product ?? row.source_label}</strong><small>{row.reference} · {row.correlation_key}</small></span></span>
              <span>{row.source_label}</span><span>{relativeTime(row.started_at)}</span><span className="mono">{durationLabel(row.duration_seconds)}</span><Status value={row.status} />
            </button>
          ))}
        </div>
        <aside className="execution-detail" id="execution-detail" tabIndex={-1} key={selectedRow.id}>
          <div className="detail-top"><span>{selectedRow.reference}</span><a aria-label="Open canonical repository detail" href={selectedRow.href}>↗</a></div>
          <h2>{selectedRow.repository ?? selectedRow.product ?? selectedRow.source_label}</h2>
          <p>{selectedRow.correlation_key}</p>
          <div className="detail-status"><Status value={selectedRow.status} /><span>durable lifecycle</span></div>
          <ol className="step-list">
            <li className="done"><i /><span><strong>Execution accepted</strong><small>{selectedRow.source_label}</small></span><b>{relativeTime(selectedRow.requested_at)}</b></li>
            {selectedRow.steps.map((step) => <li className={statusTone(step.status) === "succeeded" ? "done" : statusTone(step.status) === "running" ? "active" : statusTone(step.status) === "failed" ? "failed" : ""} key={step.id}><i /><span><strong>{step.name}</strong><small>{step.source}{step.failure_reason ? ` · ${step.failure_reason}` : ""}</small></span><b>{durationLabel(step.duration_seconds)}</b></li>)}
            <li className={selectedRow.artifacts.length ? "done" : ""}><i /><span><strong>Artifacts normalized</strong><small>{selectedRow.artifacts.length ? selectedRow.artifacts.map((artifact) => artifact.kind).join(", ") : "No artifacts recorded"}</small></span><b>{selectedRow.artifacts.length}</b></li>
            <li className={selectedRow.artifacts.some((artifact) => artifact.deliveries.some((delivery) => delivery.status === "confirmed")) ? "done" : ""}><i /><span><strong>Evidence delivery</strong><small>{selectedRow.artifacts.flatMap((artifact) => artifact.deliveries).length} attempts recorded</small></span><b>{selectedRow.artifacts.flatMap((artifact) => artifact.deliveries).filter((delivery) => delivery.status === "confirmed").length}</b></li>
          </ol>
          <div className="live-log"><span><i /> observed state</span>{selectedRow.artifacts.length ? selectedRow.artifacts.slice(0, 3).map((artifact) => <code key={artifact.id}>{artifact.kind} · {artifact.status} · {artifact.size_bytes.toLocaleString()} bytes</code>) : <code>No artifact payloads recorded.</code>}</div>
        </aside>
      </section>}
      <PaginationBar
        ariaLabel="Executions"
        page={data?.pagination?.page ?? filters.page}
        totalPages={data?.pagination?.total_pages ?? 1}
        pageSize={data?.pagination?.page_size ?? filters.pageSize}
        start={data?.pagination?.start ?? 0}
        end={data?.pagination?.end ?? 0}
        total={data?.matching ?? 0}
        onPageChange={(page) => applyFilters({ page })}
        onPageSizeChange={(pageSize) => applyFilters({ pageSize, page: 1 })}
      />
    </div>
  );
}

function JobJournal() {
  type Row = {
    id: number; reference: string; name: string; repository: string; product: string | null;
    ref: string | null; pipeline_url: string | null; duration_seconds: number | null;
    finished_at: string; status: string; detail: string; href: string;
  };
  type Filters = { repo: string; job: string; category: string; status: string; timeframe: string; page: number; pageSize: PageSize };
  type Payload = {
    rows: Row[];
    summary: {
      matching: number;
      returned: number;
      duration_samples: number;
      median_duration_seconds: number | null;
      oldest_age_seconds: number | null;
      oldest_finished_at: string | null;
      successful: number;
      failed: number;
      terminal: number;
      success_rate: number | null;
    };
    pagination: { page: number; page_size: number; total_pages: number; start: number; end: number };
    filters: Filters & { repositories: string[]; jobs: string[] };
  };
  const categories = [
    ["all", "All"],
    ["sast", "SAST"],
    ["sca", "SCA"],
    ["secrets", "Secrets"],
    ["cs", "CS"],
    ["asoc", "ASOC"],
    ["mobsf", "MobSF"],
  ];
  const allowedCategories = new Set(categories.map(([value]) => value));
  const allowedTimeframes = new Set(overviewTimeframeOptions.map(({ value }) => value));
  const readFilters = (): Filters => {
    const params = new URLSearchParams(window.location.search);
    const requestedStatus = params.get("status")?.toLowerCase() ?? "";
    const requestedCategory = params.get("category")?.toLowerCase() ?? "all";
    const requestedTimeframe = params.get("timeframe")?.toLowerCase() ?? "7d";
    const status = ["all", "success", "failed"].includes(requestedStatus)
      ? requestedStatus
      : window.location.pathname === "/logs"
        ? "all"
        : "failed";
    return {
      repo: params.get("repo") ?? "",
      job: params.get("job") ?? "",
      category: allowedCategories.has(requestedCategory) ? requestedCategory : "all",
      status,
      timeframe: allowedTimeframes.has(requestedTimeframe) ? requestedTimeframe : "7d",
      page: Math.max(1, Number(params.get("page")) || 1),
      pageSize: pageSizeFrom(params.get("page_size")),
    };
  };
  const filtersUrl = (values: Filters) => {
    const url = new URL("/logs", window.location.origin);
    if (values.repo) url.searchParams.set("repo", values.repo);
    if (values.job) url.searchParams.set("job", values.job);
    if (values.category !== "all") url.searchParams.set("category", values.category);
    if (values.status !== "all") url.searchParams.set("status", values.status);
    if (values.timeframe !== "7d") url.searchParams.set("timeframe", values.timeframe);
    if (values.page > 1) url.searchParams.set("page", String(values.page));
    if (values.pageSize !== 25) url.searchParams.set("page_size", String(values.pageSize));
    return url;
  };
  const [filters, setFilters] = useState<Filters>(readFilters);
  const [repoDraft, setRepoDraft] = useState(filters.repo);
  const [jobDraft, setJobDraft] = useState(filters.job);
  const repoDraftRef = useRef(filters.repo);
  const jobDraftRef = useRef(filters.job);
  const apiPath = useMemo(() => {
    const params = new URLSearchParams({
      repo: filters.repo,
      job: filters.job,
      category: filters.category,
      status: filters.status,
      timeframe: filters.timeframe,
      page: String(filters.page),
      page_size: String(filters.pageSize),
    });
    return `/api/ui/job-journal?${params}`;
  }, [filters]);
  const { data, state, retry } = useLiveData<Payload>(apiPath);
  const rows = data?.rows ?? [];
  const isFailedQueue = filters.status === "failed";
  useEffect(() => {
    const syncFromLocation = () => {
      if (window.location.pathname !== "/logs") return;
      const next = readFilters();
      const canonical = filtersUrl(next);
      if (`${canonical.pathname}${canonical.search}` !== `${window.location.pathname}${window.location.search}`) {
        window.history.replaceState({}, "", canonical);
      }
      setFilters(next);
      setRepoDraft(next.repo);
      repoDraftRef.current = next.repo;
      setJobDraft(next.job);
      jobDraftRef.current = next.job;
    };
    const canonical = filtersUrl(filters);
    if (`${canonical.pathname}${canonical.search}` !== `${window.location.pathname}${window.location.search}`) {
      window.history.replaceState({}, "", canonical);
    }
    window.addEventListener("popstate", syncFromLocation);
    return () => window.removeEventListener("popstate", syncFromLocation);
  }, []);

  useEffect(() => {
    const repo = repoDraft.trim();
    const job = jobDraft.trim();
    if (repo === filters.repo && job === filters.job) return;
    const timeout = window.setTimeout(() => {
      applyFilters({ repo, job, page: 1 }, true);
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [repoDraft, jobDraft, filters.repo, filters.job]);

  const applyFilters = (next: Partial<Filters>, replace = false) => {
    const updated = {
      ...filters,
      ...next,
      repo: next.repo ?? repoDraftRef.current.trim(),
      job: next.job ?? jobDraftRef.current.trim(),
    };
    const url = filtersUrl(updated);
    window.history[replace ? "replaceState" : "pushState"]({}, "", url);
    setFilters(updated);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };

  const resetFilters = () => {
    setRepoDraft("");
    repoDraftRef.current = "";
    setJobDraft("");
    jobDraftRef.current = "";
    applyFilters({ repo: "", job: "", category: "all", status: isFailedQueue ? "failed" : "all", timeframe: "7d", page: 1 });
  };
  const hasActiveFilters = Boolean(repoDraft.trim())
    || Boolean(filters.job)
    || filters.category !== "all"
    || filters.status !== (isFailedQueue ? "failed" : "all")
    || filters.timeframe !== "7d";
  const jobOptions = [
    { value: "", label: "All jobs" },
    ...(data?.filters.jobs ?? []).map((value) => ({ value, label: value })),
  ];

  useEffect(() => {
    const effectivePage = data?.pagination?.page;
    if (!effectivePage || effectivePage === filters.page) return;
    const url = new URL(window.location.href);
    if (effectivePage > 1) url.searchParams.set("page", String(effectivePage));
    else url.searchParams.delete("page");
    window.history.replaceState({}, "", url);
    setFilters((current) => ({ ...current, page: effectivePage }));
  }, [data?.pagination?.page, filters.page]);

  return (
    <div className="view">
      <header className="page-title">
        <div><p>{isFailedQueue ? "Recovery queue" : "Execution journal"}</p><h1>{isFailedQueue ? "Failures, explained." : "Every observed job."}</h1><span>{isFailedQueue ? "Terminal failed security jobs, with their repository, ref and source pipeline." : "The complete security job journal across monitored repositories."}</span></div>
        <div className="header-actions"><span className="header-evidence">{data?.summary.matching ?? "—"} matching jobs</span><a className="primary-button" href="/pipelines?group=needs_attention">Inspect pipelines</a></div>
      </header>
      <nav className="journal-categories" aria-label="Job categories">
        {categories.map(([value, label]) => <button type="button" aria-pressed={filters.category === value} className={filters.category === value ? "active" : ""} onClick={() => applyFilters({ category: value, page: 1 })} key={value}>{label}</button>)}
      </nav>
      <div className="filter-surface journal-filters" aria-label="Job journal filters">
        <label className="filter-field"><span>Repository</span><SearchableSelect ariaLabel="Job repository" value={repoDraft} options={(data?.filters.repositories ?? []).map((value) => ({ value, label: value }))} onChange={(value) => { repoDraftRef.current = value; setRepoDraft(value); }} placeholder="Filter by repository" /></label>
        <label className="filter-field"><span>Job name</span><SearchableSelect ariaLabel="Job name" value={jobDraft} options={jobOptions} onChange={(value) => { jobDraftRef.current = value; setJobDraft(value); }} placeholder="Search jobs" /></label>
        <label className="filter-field"><span>Status</span><SignalSelect ariaLabel="Job status" value={filters.status} options={[{ value: "all", label: "All" }, { value: "success", label: "Success" }, { value: "failed", label: "Failed" }]} onChange={(value) => applyFilters({ status: value, page: 1 })} /></label>
        <label className="filter-field"><span>Timeframe</span><SignalSelect ariaLabel="Job timeframe" value={filters.timeframe} options={overviewTimeframeOptions} onChange={(value) => applyFilters({ timeframe: value, page: 1 })} /></label>
        {hasActiveFilters && <button className="filter-clear journal-clear-filters" type="button" onClick={resetFilters}>Clear filters</button>}
      </div>
      <section className="failure-summary job-outcome-summary">
        <div><span>MATCHING JOBS</span><strong>{data?.summary.matching ?? "—"}</strong><small>{isFailedQueue ? "terminal failures after current filters" : "after current filters"}</small></div>
        <div><span>SUCCESSFUL</span><strong>{data?.summary.successful.toLocaleString() ?? "—"}</strong><small>{data?.summary.success_rate == null ? "no terminal outcomes" : `${data.summary.success_rate}% of ${data.summary.terminal.toLocaleString()} terminal jobs`}</small></div>
        <div><span>FAILED</span><strong className="metric-danger">{data?.summary.failed.toLocaleString() ?? "—"}</strong><small>{data?.summary.terminal ? `${(100 - (data.summary.success_rate ?? 0)).toFixed(1)}% of terminal outcomes` : "no terminal outcomes"}</small></div>
        <div><span>MEDIAN RUNTIME</span><strong>{durationLabel(data?.summary.median_duration_seconds)}</strong><small>{data?.summary.duration_samples ?? 0} jobs with reported duration</small></div>
        <div><span>OLDEST RESULT</span><strong>{ageLabel(data?.summary.oldest_age_seconds)}</strong><small>{data?.summary.oldest_finished_at ? `finished ${relativeTime(data.summary.oldest_finished_at)}` : "no finished jobs in this view"}</small></div>
      </section>
      {!rows.length && <EmptyState state={state} title={isFailedQueue ? "No failed jobs" : "No jobs match these filters"} copy={isFailedQueue ? "There are no active terminal failures in the current retention window." : "Adjust the repository, job, status, category or timeframe filters."} onRetry={retry} />}
      {!!rows.length && (
      <section className="job-journal-table" aria-label={isFailedQueue ? "Failed jobs" : "Job journal"}>
        <div className="job-journal-heading">
          <div><span>{isFailedQueue ? "FAILURE QUEUE" : "JOB JOURNAL"}</span><h2>Observed jobs</h2></div>
          <b>{data?.pagination?.start ?? 0}–{data?.pagination?.end ?? 0} of {data?.summary.matching ?? 0}</b>
        </div>
        <div className="job-journal-columns" aria-hidden="true">
          <span>Status</span><span>Job</span><span>Repository</span><span>Ref</span><span>Finished</span><span>Duration</span><span>Source</span>
        </div>
        <div className="job-journal-rows">
          {rows.map((job) => (
            <article className={`job-journal-row tone-${statusTone(job.status)}`} key={job.id}>
              <Status value={job.status} />
              <span className="job-identity">
                <span className="job-mobile-label">Job</span>
                <strong title={job.name}>{job.name}</strong>
                <small title={job.detail}>{job.reference} · {job.detail}</small>
              </span>
              <a className="job-repository-link" href={job.href} title={`Open ${job.repository}`}>
                <span className="job-mobile-label">Repository</span>
                <strong>{job.repository}</strong>
                <small>Repository 360 ↗</small>
              </a>
              <span className="job-ref">
                <span className="job-mobile-label">Ref</span>
                <code title={job.ref ?? "Not reported"}>{job.ref ?? "—"}</code>
              </span>
              <span className="job-finished">
                <span className="job-mobile-label">Finished</span>
                <time dateTime={job.finished_at} title={new Date(job.finished_at).toLocaleString()}>{relativeTime(job.finished_at)}</time>
              </span>
              <span className="job-duration">
                <span className="job-mobile-label">Duration</span>
                <b>{durationLabel(job.duration_seconds)}</b>
              </span>
              <span className="job-source">
                <span className="job-mobile-label">Source</span>
                {job.pipeline_url
                  ? <a className="job-pipeline-link" href={job.pipeline_url} target="_blank" rel="noreferrer" aria-label={`Open source pipeline for ${job.name}`}><span>Open pipeline</span><span aria-hidden="true">↗</span></a>
                  : <span title="The source pipeline URL was not reported">Not reported</span>}
              </span>
            </article>
          ))}
        </div>
        <PaginationBar
          ariaLabel={isFailedQueue ? "Failed jobs" : "Jobs"}
          page={data?.pagination?.page ?? filters.page}
          totalPages={data?.pagination?.total_pages ?? 1}
          pageSize={pageSizeFrom(String(data?.pagination?.page_size ?? filters.pageSize))}
          start={data?.pagination?.start ?? 0}
          end={data?.pagination?.end ?? 0}
          total={data?.summary.matching ?? 0}
          onPageChange={(page) => applyFilters({ page })}
          onPageSizeChange={(pageSize) => applyFilters({ pageSize, page: 1 })}
          embedded
        />
      </section>
      )}
    </div>
  );
}

function ScreenHeader({ signal, title, copy, primary = "Open details", secondary = "Last 24 hours", primaryHref, secondaryHref }: { signal: string; title: string; copy: string; primary?: string | null; secondary?: string; primaryHref?: string; secondaryHref?: string }) {
  return (
    <header className="page-title">
      <div><p>{signal}</p><h1>{title}</h1><span>{copy}</span></div>
      <div className="header-actions">
        {secondaryHref ? <a className="quiet-button" href={secondaryHref}>{secondary} <span>↗</span></a> : <span className="header-evidence">{secondary}</span>}
        {primary !== null && (primaryHref ? <a className="primary-button" href={primaryHref}>{primary}</a> : <span className="primary-button is-static">{primary}</span>)}
      </div>
    </header>
  );
}

function ActivePipelines() {
  type Row = { id: number; pipeline_id: number; status: string; repository_id: number | null; repository: string | null; product_id: number | null; product: string | null; ref: string | null; commit_sha: string | null; job_count: number; activity_at: string; started_at: string | null; finished_at: string | null; duration_seconds: number | null; error_message: string | null; web_url: string | null; href: string };
  type PipelineGroup = "in_progress" | "needs_attention" | "completed_recently";
  type PipelineFilters = { group: PipelineGroup; timeframe: string; status: string; page: number; pageSize: PageSize; product: string; repository: string };
  type ProductOption = { id: number; name: string; path: string };
  type RepositoryOption = { id: number; path: string; product_id: number };
  type Payload = {
    rows: Row[];
    counts: Record<string, number>;
    total: number;
    matching: number;
    group: PipelineGroup;
    timeframe: string;
    status: string | null;
    product: number | null;
    repository: number | null;
    page: number;
    page_size: PageSize;
    page_count: number;
    filter_options: {
      products: ProductOption[];
      repositories: RepositoryOption[];
    };
  };
  const readFilters = (): PipelineFilters => {
    const params = new URLSearchParams(window.location.search);
    const groupValue = params.get("group");
    const timeframeValue = params.get("timeframe") ?? "7d";
    const statusValue = params.get("status") ?? "";
    const statusGroups: Record<string, PipelineGroup> = {
      registered: "in_progress",
      polling: "in_progress",
      failed: "needs_attention",
      stale: "needs_attention",
      completed: "completed_recently",
    };
    const statusGroup = Object.prototype.hasOwnProperty.call(statusGroups, statusValue)
      ? statusGroups[statusValue]
      : null;
    const normalizedStatus = statusGroup ? statusValue : "";
    const explicitGroup: PipelineGroup | null = (
      groupValue === "in_progress"
      || groupValue === "needs_attention"
      || groupValue === "completed_recently"
    ) ? groupValue : null;
    const group: PipelineGroup = explicitGroup
      ?? (
        groupValue === null && statusGroup
          ? statusGroup
          : "in_progress"
      );
    const positiveId = (key: string) => {
      const value = params.get(key) ?? "";
      return /^\d+$/.test(value) && Number(value) > 0 ? String(Number(value)) : "";
    };
    const pageValue = params.get("page") ?? "";
    return {
      group,
      timeframe: ["today", "3d", "7d", "30d", "1y", "all"].includes(timeframeValue) ? timeframeValue : "7d",
      status: normalizedStatus,
      page: /^\d+$/.test(pageValue) && Number(pageValue) > 0 ? Number(pageValue) : 1,
      pageSize: pageSizeFrom(params.get("page_size")),
      product: positiveId("product"),
      repository: positiveId("repository"),
    };
  };
  const [filters, setFilters] = useState<PipelineFilters>(readFilters);
  const query = useMemo(() => {
    const params = new URLSearchParams({
      group: filters.group,
      timeframe: filters.timeframe,
      page: String(filters.page),
      page_size: String(filters.pageSize),
    });
    if (filters.status) params.set("status", filters.status);
    if (filters.product) params.set("product", filters.product);
    if (filters.repository) params.set("repository", filters.repository);
    return params.toString();
  }, [filters]);
  const { data, state, retry } = useLiveData<Payload>(`/api/ui/pipelines?${query}`);
  const group = filters.group;
  const lanes = data?.rows ?? [];
  const groupLabels: Record<PipelineGroup, { label: string; title: string; copy: string; empty: string }> = {
    in_progress: {
      label: "In progress",
      title: "Pipelines in flight.",
      copy: "Registered and polling GitLab pipelines across the current control plane.",
      empty: "No pipelines are in progress",
    },
    needs_attention: {
      label: "Needs attention",
      title: "Intervention queue.",
      copy: "Failed and stale GitLab pipelines that require an operator decision.",
      empty: "No pipelines need attention",
    },
    completed_recently: {
      label: "Completed",
      title: "Recently completed.",
      copy: "Terminal GitLab pipelines observed during the current retention window.",
      empty: "No pipelines completed recently",
    },
  };
  const groupMeta = groupLabels[group];
  const windowTotal = Object.values(data?.counts ?? {}).reduce(
    (total, count) => total + count,
    0,
  );
  const applyFilters = (next: Partial<PipelineFilters>) => {
    const updated = { ...filters, ...next };
    const url = new URL(window.location.href);
    if (updated.group === "in_progress") url.searchParams.delete("group");
    else url.searchParams.set("group", updated.group);
    if (updated.timeframe === "7d") url.searchParams.delete("timeframe");
    else url.searchParams.set("timeframe", updated.timeframe);
    if (updated.status) url.searchParams.set("status", updated.status);
    else url.searchParams.delete("status");
    if (updated.product) url.searchParams.set("product", updated.product);
    else url.searchParams.delete("product");
    if (updated.repository) url.searchParams.set("repository", updated.repository);
    else url.searchParams.delete("repository");
    if (updated.page > 1) url.searchParams.set("page", String(updated.page));
    else url.searchParams.delete("page");
    if (updated.pageSize !== 25) url.searchParams.set("page_size", String(updated.pageSize));
    else url.searchParams.delete("page_size");
    window.history.pushState({}, "", url);
    setFilters(updated);
  };

  useEffect(() => {
    const onPopState = () => setFilters(readFilters());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const effectivePage = data?.page;
    if (!effectivePage || effectivePage === filters.page) return;
    const url = new URL(window.location.href);
    if (effectivePage > 1) url.searchParams.set("page", String(effectivePage));
    else url.searchParams.delete("page");
    window.history.replaceState({}, "", url);
    setFilters((current) => ({ ...current, page: effectivePage }));
  }, [data?.page, filters.page]);

  const statusOptions: Record<PipelineGroup, { value: string; label: string }[]> = {
    in_progress: [{ value: "", label: "All active states" }, { value: "registered", label: "Registered" }, { value: "polling", label: "Polling" }],
    needs_attention: [{ value: "", label: "All attention states" }, { value: "failed", label: "Failed" }, { value: "stale", label: "Stale" }],
    completed_recently: [{ value: "", label: "All completed states" }, { value: "completed", label: "Completed" }],
  };
  const selectedStatusOptions = statusOptions[group].some((option) => option.value === filters.status)
    ? statusOptions[group]
    : [
      ...statusOptions[group],
      {
        value: filters.status,
        label: `${filters.status.replaceAll("_", " ")} · outside lifecycle`,
      },
    ];
  const timeframeLabel = {
    today: "Today",
    "3d": "Last 3 days",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "1y": "Last 12 months",
    all: "All retained history",
  }[filters.timeframe] ?? "Last 7 days";
  const productOptions = data?.filter_options.products ?? [];
  const repositoryOptions = (data?.filter_options.repositories ?? []).filter((option) => (
    !filters.product
    || option.product_id === Number(filters.product)
    || option.id === Number(filters.repository)
  ));
  return (
    <div className="view">
      <ScreenHeader signal="Pipeline control" title={groupMeta.title} copy={groupMeta.copy} primary={null} secondary={timeframeLabel} />
      <section className="filter-surface pipeline-filters" aria-label="Pipeline filters">
        <div className="filter-surface-heading">
          <span>Pipeline filters</span>
          <small>{data?.matching ?? 0} matching · {state === "stale" ? "refresh paused" : "auto-refreshing"}</small>
        </div>
        <nav className="pipeline-lifecycle-tabs" aria-label="Pipeline lifecycle">
          {(Object.keys(groupLabels) as PipelineGroup[]).map((value) => (
            <button type="button" aria-pressed={group === value} className={group === value ? "active" : ""} onClick={() => applyFilters({ group: value, status: "", page: 1 })} key={value}>
              {groupLabels[value].label} <b>{data?.counts[value] ?? 0}</b>
            </button>
          ))}
        </nav>
        <div className="pipeline-filter-grid">
          <label className="filter-field"><span>State</span><SignalSelect ariaLabel="Pipeline status" value={filters.status} options={selectedStatusOptions} onChange={(value) => applyFilters({ status: value, page: 1 })} /></label>
          <label className="filter-field"><span>Window</span><SignalSelect ariaLabel="Pipeline timeframe" value={filters.timeframe} options={overviewTimeframeOptions.map((option) => option.value === "all" ? { ...option, label: "All retained history" } : option)} onChange={(value) => applyFilters({ timeframe: value, page: 1 })} /></label>
          <label className="filter-field"><span>Product</span><SignalSelect ariaLabel="Pipeline product" value={filters.product} options={[{ value: "", label: "All products" }, ...productOptions.map((option) => ({ value: String(option.id), label: option.name }))]} onChange={(value) => applyFilters({ product: value, repository: "", page: 1 })} /></label>
          <label className="filter-field pipeline-repository-filter"><span>Repository</span><SignalSelect ariaLabel="Pipeline repository" value={filters.repository} options={[{ value: "", label: "All repositories" }, ...repositoryOptions.map((option) => ({ value: String(option.id), label: option.path }))]} onChange={(value) => applyFilters({ repository: value, page: 1 })} /></label>
        </div>
      </section>
      <section className="ops-metric-strip">
        <div><span>{groupMeta.label.toUpperCase()}</span><strong>{data?.matching ?? "—"}</strong><small>matching lifecycle</small></div>
        <div><span>VISIBLE PIPELINES</span><strong>{lanes.length}</strong><small>current page</small></div>
        <div><span>POLLING SHOWN</span><strong className="acid-text">{lanes.filter((row) => row.status === "polling").length}</strong><small>within {lanes.length} visible</small></div>
        <div className="capacity-cell"><span>{timeframeLabel.toUpperCase()}</span><b><i style={{ width: `${windowTotal ? Math.min(100, ((data?.matching ?? 0) / windowTotal) * 100) : 0}%` }} /></b><small>{data?.matching ?? 0} matching of {windowTotal} observed</small></div>
      </section>
      {!lanes.length && <EmptyState state={state} title={groupMeta.empty} copy="Change the lifecycle, status or observation-window filters to inspect retained history." onRetry={retry} />}
      {!!lanes.length && (
      <section className="pipeline-workbench">
        <article className="pipeline-board">
          <div className="surface-heading"><div><span>RETAINED PIPELINE FACTS</span><h2>Observed pipelines</h2></div><b>{lanes.length} of {data?.matching ?? 0} shown</b></div>
          <div className="pipeline-register-columns" aria-hidden="true">
            <span>Pipeline</span><span>Observed facts</span><span>Status</span>
          </div>
          {lanes.map((lane) => (
            <div className="pipeline-lane" key={lane.id}>
              <a className="pipeline-lane-link" href={`/pipelines/${lane.id}`} aria-label={`Open pipeline detail for #${lane.pipeline_id}${lane.repository ? ` for ${lane.repository}` : ""}`}>
                <span className="lane-name"><strong>{lane.repository ?? lane.product ?? `Pipeline ${lane.pipeline_id}`}</strong><small>#{lane.pipeline_id} · {lane.ref ?? "no ref"} · {lane.job_count} observed jobs</small></span>
                <span className="lane-facts">
                  <span><small>Last activity</small><strong title={lane.activity_at}>{relativeTime(lane.activity_at)}</strong></span>
                  <span><small>Duration</small><strong>{durationLabel(lane.duration_seconds)}</strong></span>
                  <span className={lane.error_message ? "lane-diagnostic" : ""}><small>{lane.error_message ? "Diagnostic" : "Commit"}</small><strong title={lane.error_message ?? lane.commit_sha ?? undefined}>{lane.error_message ?? lane.commit_sha?.slice(0, 8) ?? "—"}</strong></span>
                </span>
              </a>
              <span className="pipeline-lane-action"><Status value={lane.status} />{lane.web_url ? <a className="pipeline-external-link" href={lane.web_url} target="_blank" rel="noreferrer">GitLab ↗</a> : <span className="pipeline-external-link is-muted">GitLab link unavailable</span>}</span>
            </div>
          ))}
        </article>
      </section>
      )}
      <PaginationBar
        ariaLabel="Pipelines"
        page={data?.page ?? filters.page}
        totalPages={data?.page_count ?? 1}
        pageSize={data?.page_size ?? filters.pageSize}
        start={(data?.matching ?? 0) ? ((data?.page ?? filters.page) - 1) * (data?.page_size ?? filters.pageSize) + 1 : 0}
        end={Math.min((data?.page ?? filters.page) * (data?.page_size ?? filters.pageSize), data?.matching ?? 0)}
        total={data?.matching ?? 0}
        onPageChange={(page) => applyFilters({ page })}
        onPageSizeChange={(pageSize) => applyFilters({ pageSize, page: 1 })}
      />
    </div>
  );
}

function PipelineDetail() {
  type Job = {
    id: number;
    gitlab_job_id: number;
    name: string;
    stage: string | null;
    status: string;
    ref: string | null;
    started_at: string | null;
    finished_at: string | null;
    duration_seconds: number | null;
    error: string | null;
    allow_failure: null;
    retry: number | null;
    last_seen_at: string | null;
    links: { gitlab: string | null };
  };
  type Stage = {
    id: string;
    name: string;
    status: string;
    succeeded: number;
    total: number;
    jobs: Job[];
  };
  type Payload = {
    pipeline: {
      id: number;
      pipeline_id: number;
      project_id: number;
      repository_id: number | null;
      repository: string | null;
      product: string | null;
      ref: string | null;
      commit_sha: string | null;
      status: string;
      started_at: string | null;
      finished_at: string | null;
      duration_seconds: number | null;
      error_message: string | null;
      last_polled_at: string | null;
      sync: { last_polled_at: string | null; mode: "polling" | "snapshot"; refresh_seconds: number | null };
    };
    completeness: {
      mode: "tracked_only" | "observed";
      is_partial: boolean | null;
      jobs_complete: boolean | null;
      known_jobs: number;
      expected_jobs: number | null;
      tracked_job_names: string[] | null;
      message: string;
    };
    summary: { stages: number; jobs: number; succeeded_jobs: number; failed_jobs: number; skipped_jobs: number; duration_seconds: number | null };
    ordering: { mode: string; message: string };
    stages: Stage[];
    links: { pipelines: string; gitlab: string | null; repository: string | null };
  };

  const activeId = window.location.pathname.match(/^\/pipelines\/(\d+)\/?$/)?.[1] ?? null;
  const detailPath = activeId ? `/api/ui/pipelines/${activeId}` : null;
  const [detailData, setDetailData] = useState<Payload | null>(null);
  const refreshMs = detailData?.pipeline.status && ["registered", "polling"].includes(detailData.pipeline.status)
    ? 15_000
    : detailData
      ? null
      : 30_000;
  const { data, state, retry } = useLiveData<Payload>(detailPath, refreshMs);
  useEffect(() => {
    if (data) setDetailData(data);
  }, [data]);
  const current = data ?? detailData;
  const pipeline = current?.pipeline;
  const stages = current?.stages ?? [];
  const syncLabel = pipeline?.sync.mode === "polling"
    ? `Syncing · ${pipeline.sync.refresh_seconds}s`
    : pipeline?.last_polled_at
      ? `Snapshot · ${relativeTime(pipeline.last_polled_at)}`
      : "Snapshot · not polled";
  const commitLabel = pipeline?.commit_sha ? pipeline.commit_sha.slice(0, 12) : "SHA not recorded";

  if (!activeId) {
    return <ActivePipelines />;
  }

  return (
    <div className="view pipeline-detail-view">
      <nav className="pipeline-detail-breadcrumbs" aria-label="Pipeline detail breadcrumb">
        <a href="/pipelines">Active pipelines</a><span aria-hidden="true">/</span><strong>{pipeline?.repository ?? `Pipeline ${activeId}`}</strong>
      </nav>
      <ScreenHeader
        signal="Pipeline detail"
        title={pipeline?.repository ?? `Pipeline ${pipeline?.pipeline_id ?? activeId}`}
        copy={pipeline ? `GitLab pipeline #${pipeline.pipeline_id} · ${pipeline.ref ?? "ref not recorded"}` : "Reading the persisted execution record."}
        primary={current?.links?.gitlab ? "Open in GitLab ↗" : null}
        primaryHref={current?.links?.gitlab ?? undefined}
        secondary={syncLabel}
      />
      {state !== "ready" && !current && <EmptyState state={state} title="Pipeline detail unavailable" copy="Retry to read the persisted pipeline and its observed jobs." onRetry={retry} />}
      {current && pipeline && (
        <>
          <section className="pipeline-detail-facts" aria-label="Pipeline header facts">
            <div><span>Repository</span><strong>{pipeline.repository ?? "Not recorded"}</strong>{current.links.repository && <a href={current.links.repository} target="_blank" rel="noreferrer">GitLab repository ↗</a>}</div>
            <div><span>Ref</span><strong title={pipeline.ref ?? undefined}>{pipeline.ref ?? "Not recorded"}</strong><small>Pipeline #{pipeline.pipeline_id}</small></div>
            <div><span>Commit SHA</span><strong title={pipeline.commit_sha ?? undefined}>{commitLabel}</strong><small>{pipeline.commit_sha ? "Persisted commit" : "Not recorded"}</small></div>
            <div><span>Status</span><Status value={pipeline.status} /><small>{pipeline.finished_at ? `Finished ${relativeTime(pipeline.finished_at)}` : "No finish signal"}</small></div>
            <div><span>Duration</span><strong>{durationLabel(pipeline.duration_seconds)}</strong><small>{pipeline.started_at ? `Started ${relativeTime(pipeline.started_at)}` : "Start not recorded"}</small></div>
            <div><span>Last sync</span><strong>{pipeline.last_polled_at ? relativeTime(pipeline.last_polled_at) : "Not recorded"}</strong><small>{pipeline.sync.mode === "polling" ? "Active poll" : "Terminal snapshot"}</small></div>
          </section>
          {pipeline.error_message && <div className="pipeline-detail-error" role="alert"><Status value="failed" /><p>{pipeline.error_message}</p></div>}
          <section className={`pipeline-completeness ${current.completeness.is_partial === true ? "is-partial" : "is-unknown"}`} aria-label="Pipeline data completeness">
            <div><span>DATA SCOPE</span><strong>{current.completeness.mode === "tracked_only" ? "Tracked jobs only" : "Persisted job observation"}</strong></div>
            <p>{current.completeness.message}</p>
            <b>{current.completeness.known_jobs} known jobs{current.completeness.expected_jobs == null ? " · expected total not recorded" : ` of ${current.completeness.expected_jobs}`}</b>
          </section>
          <section className="pipeline-detail-summary" aria-label="Pipeline summary">
            <div><span>STAGES</span><strong>{current.summary.stages}</strong><small>alphabetically grouped</small></div>
            <div><span>JOBS</span><strong>{current.summary.jobs}</strong><small>{current.summary.succeeded_jobs} succeeded</small></div>
            <div><span>FAILED</span><strong className={current.summary.failed_jobs ? "red-text" : ""}>{current.summary.failed_jobs}</strong><small>{current.summary.skipped_jobs} skipped</small></div>
            <div><span>RUNTIME</span><strong>{durationLabel(current.summary.duration_seconds)}</strong><small>{pipeline.sync.mode === "polling" ? "live snapshot" : "terminal snapshot"}</small></div>
          </section>
          <section className="pipeline-river" aria-label="Pipeline execution river">
            <div className="pipeline-river-heading"><div><span>EXECUTION RIVER</span><h2>Persisted GitLab stages</h2></div><b title={current.ordering.message}>{stages.length ? `${stages.length} stages · ${current.summary.jobs} jobs` : "No persisted stages"}</b></div>
            {!stages.length && <EmptyState state="ready" title="No persisted jobs" copy="The pipeline record exists, but no GitLab jobs have been persisted yet. This view will not invent stage progress." />}
            {!!stages.length && <div className="pipeline-river-track">
              {stages.map((stage) => (
                <details className={`pipeline-stage stage-${statusTone(stage.status)}`} key={stage.id} open={stage.status === "failed"}>
                  <summary>
                    <span className="pipeline-stage-node" aria-hidden="true"><i /></span>
                    <span className="pipeline-stage-title"><strong>{stage.name}</strong><small>Stage group</small></span>
                    <span className="pipeline-stage-progress"><b>{stage.succeeded}/{stage.total}</b><small>succeeded</small></span>
                    <Status value={stage.status} />
                    <span className="pipeline-stage-chevron" aria-hidden="true">⌄</span>
                  </summary>
                  <div className="pipeline-job-list">
                    {stage.jobs.map((job) => (
                      <div className="pipeline-job-row" key={job.id}>
                        <span className="pipeline-job-status"><Status value={job.status} /></span>
                        <span className="pipeline-job-name"><strong>{job.name}</strong><small>{job.gitlab_job_id ? `GitLab job #${job.gitlab_job_id}` : "Job id not recorded"}</small></span>
                        <span><small>Duration</small><b>{durationLabel(job.duration_seconds)}</b></span>
                        <span><small>Started</small><b>{job.started_at ? relativeTime(job.started_at) : "—"}</b></span>
                        <span><small>Finished</small><b>{job.finished_at ? relativeTime(job.finished_at) : "—"}</b></span>
                        <span className="pipeline-job-flags">{job.retry != null && <em>retry {job.retry}</em>}{job.error && <em className="job-error">{job.error}</em>}</span>
                        {job.links.gitlab ? <a className="pipeline-job-link" href={job.links.gitlab} target="_blank" rel="noreferrer">GitLab ↗</a> : <span className="pipeline-job-link is-muted">No link</span>}
                      </div>
                    ))}
                  </div>
                </details>
              ))}
            </div>}
          </section>
        </>
      )}
    </div>
  );
}

function ConfigGuard() {
  type Run = { id: number; product: string | null; status: string; scope_path: string | null; total_projects: number; scanned_projects: number; findings_count: number; duration_seconds: number | null; href: string };
  type Finding = { id: number; run_id: number; project_path: string; severity: string; rule_id: string; title: string; description: string | null; recommendation: string | null; href: string };
  type Payload = { active_run_id?: number | null; selected_run_id?: number | null; selected_finding?: Finding | null; summary: { products_scanned: number; open_findings: number; retained_findings: number }; runs: Run[]; findings: Finding[]; severity: Record<string, number>; pagination: { page: number; page_size: PageSize; total_pages: number; start: number; end: number } };
  const readFilters = () => {
    const params = new URLSearchParams(window.location.search);
    return {
      page: Math.max(1, Number(params.get("page")) || 1),
      pageSize: pageSizeFrom(params.get("page_size")),
    };
  };
  const [filters, setFilters] = useState(readFilters);
  const { data, state, retry } = useLiveData<Payload>(`/api/ui/config-guard?page=${filters.page}&page_size=${filters.pageSize}`);
  const findings = data?.findings ?? [];
  const applyFilters = (next: Partial<typeof filters>) => {
    const updated = { ...filters, ...next };
    const url = new URL(window.location.href);
    if (updated.page > 1) url.searchParams.set("page", String(updated.page));
    else url.searchParams.delete("page");
    if (updated.pageSize !== 25) url.searchParams.set("page_size", String(updated.pageSize));
    else url.searchParams.delete("page_size");
    window.history.pushState({}, "", url);
    setFilters(updated);
  };
  useEffect(() => {
    const onPopState = () => setFilters(readFilters());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => {
    const effectivePage = data?.pagination?.page;
    if (!effectivePage || effectivePage === filters.page) return;
    const url = new URL(window.location.href);
    if (effectivePage > 1) url.searchParams.set("page", String(effectivePage));
    else url.searchParams.delete("page");
    window.history.replaceState({}, "", url);
    setFilters((current) => ({ ...current, page: effectivePage }));
  }, [data?.pagination?.page, filters.page]);
  const activeRun = data?.runs.find((run) => run.id === data.active_run_id)
    ?? data?.runs.find((run) => ["queued", "running"].includes(run.status))
    ?? null;
  const latest = data?.runs.find((run) => run.id === data.selected_run_id)
    ?? data?.runs.find((run) => ["succeeded", "failed", "cancelled"].includes(run.status))
    ?? activeRun
    ?? data?.runs[0]
    ?? null;
  const selectedFinding = data?.selected_finding
    ?? findings.find((finding) => finding.run_id === latest?.id)
    ?? null;
  const selectedRunComplete = latest?.status === "succeeded";
  const noFindingTitle = selectedRunComplete
    ? "No policy drift observed"
    : "No finding evidence";
  const noFindingDescription = selectedRunComplete
    ? "No configuration differences were reported."
    : `Run ${latest?.status.replaceAll("_", " ") ?? "did not complete"} before complete finding evidence was persisted.`;
  const noFindingRecommendation = selectedRunComplete
    ? "No remediation required."
    : "Inspect the run status before drawing a configuration conclusion.";
  const scanProgress = latest?.total_projects
    ? Math.min(100, Math.round(latest.scanned_projects / latest.total_projects * 100))
    : 0;
  const openSevere = (data?.severity.critical ?? 0) + (data?.severity.high ?? 0);
  const runEvidence = activeRun
    ? activeRun.status === "queued"
      ? `Run #${activeRun.id} · queued · awaiting workers`
      : activeRun.total_projects
        ? `Run #${activeRun.id} · ${activeRun.status.replaceAll("_", " ")} · ${activeRun.scanned_projects}/${activeRun.total_projects} projects`
        : `Run #${activeRun.id} · ${activeRun.status.replaceAll("_", " ")}`
    : latest
      ? `Run #${latest.id} · ${latest.status.replaceAll("_", " ")} · ${latest.findings_count} findings in run`
      : "No scan runs";
  return (
    <div className="view">
      <ScreenHeader signal="Configuration posture" title="Drift made visible." copy="Real GitLab configuration scan runs and their reported policy findings." primary="Run or manage scans" primaryHref="/config-scan/operator" secondary={runEvidence} />
      {activeRun && latest && activeRun.id !== latest.id && <div className="run-context" role="status"><i /><span><strong>Config run #{activeRun.id} is {activeRun.status.replaceAll("_", " ")}</strong><small>{activeRun.product ?? activeRun.scope_path ?? "Configuration scope"} · {activeRun.scanned_projects} of {activeRun.total_projects} projects observed</small></span><a href={activeRun.href}>Open active run ↗</a></div>}
      {!latest && <EmptyState state={state} title="No configuration scan yet" copy="Start the first scan from the Config Guard workbench; its findings will appear here." onRetry={retry} />}
      {latest && <>
      <section className="guard-grid">
        <article className="guard-score">
          <div className="surface-heading"><div><span>POSTURE</span><h2>Scan completion</h2></div><Status value={latest.status} /></div>
          <div className="score-dial" style={{ "--score": `${scanProgress}%` } as React.CSSProperties}><div><strong>{scanProgress}</strong><small>/ 100</small></div><i /><i /><i /></div>
          <div className="score-breakdown"><span><b>{latest.scanned_projects}</b><small>scanned in run</small></span><span><b className="amber-text">{latest.findings_count}</b><small>reported in run</small></span><span><b className="red-text">{data?.summary.open_findings ?? 0}</b><small>open · product baselines</small></span></div>
        </article>
        <article className="baseline-panel">
          <div className="surface-heading"><div><span>RUN PROGRESS</span><h2>{latest.product ?? latest.scope_path ?? "Configuration scope"}</h2></div><a href={latest.href}>Details ↗</a></div>
          {[["Projects discovered", latest.total_projects, latest.total_projects], ["Projects scanned", latest.scanned_projects, latest.total_projects], ["Findings reported", latest.findings_count, Math.max(latest.scanned_projects, 1)], ["Critical / high · retained", openSevere, Math.max(data?.summary.retained_findings ?? 0, 1)]].map(([name, value, total]) => {
            const percent = Math.min(100, Number(value) / Math.max(Number(total), 1) * 100);
            return <div className="policy-layer" key={String(name)}><span><strong>{name}</strong><small>{value} observed</small></span><i><b style={{ width: `${percent}%` }} /></i><em>{Math.round(percent)}%</em></div>;
          })}
        </article>
        <article className="diff-panel">
          <div className="surface-heading"><div><span>SELECTED RUN FINDING</span><h2>{selectedFinding?.title ?? noFindingTitle}</h2></div><code>{selectedFinding?.project_path ?? latest.scope_path ?? "—"}</code></div>
          <div className="diff-code">
            <span><b>01</b><code>rule: {selectedFinding?.rule_id ?? "none"}</code></span>
            <span className={selectedFinding ? "removed" : ""}><b>02</b><code tabIndex={0} aria-label="Finding diagnostic description">{selectedFinding?.description ?? noFindingDescription}</code></span>
            <span className={selectedFinding?.recommendation || !selectedRunComplete ? "added" : ""}><b>03</b><code>{selectedFinding?.recommendation ?? noFindingRecommendation}</code></span>
            <span><b>04</b><code>run_status: {latest.status}</code></span>
            <span><b>05</b><code>duration: {durationLabel(latest.duration_seconds)}</code></span>
          </div>
        </article>
      </section>
      <section className="finding-table">
        <div className="surface-heading"><div><span>RETAINED DRIFT · RECENT RUNS</span><h2>Needs review</h2></div><b>{data?.pagination?.start ?? 0}–{data?.pagination?.end ?? 0} of {data?.summary.retained_findings ?? 0}</b></div>
        {findings.map((finding) => <a href={finding.href} key={finding.id}><span className={`finding-severity ${finding.severity}`}><i /></span><strong>{finding.title}</strong><span>{finding.project_path}</span><code>{finding.rule_id}</code><Status value={finding.severity === "critical" || finding.severity === "high" ? "failed" : "partial"} /><b>›</b></a>)}
        <PaginationBar
          ariaLabel="Config Guard findings"
          page={data?.pagination?.page ?? filters.page}
          totalPages={data?.pagination?.total_pages ?? 1}
          pageSize={data?.pagination?.page_size ?? filters.pageSize}
          start={data?.pagination?.start ?? 0}
          end={data?.pagination?.end ?? 0}
          total={data?.summary.retained_findings ?? 0}
          onPageChange={(page) => applyFilters({ page })}
          onPageSizeChange={(pageSize) => applyFilters({ pageSize, page: 1 })}
          embedded
        />
      </section>
      </>}
    </div>
  );
}

function IaCScanning() {
  type Run = { id: number; status: string; target: string; scanner: string; scanned_repositories: number; uploaded_repositories: number; findings_count: number; duration_seconds: number | null; href: string };
  type RepositoryRow = { id: number; run_id: number; project_path: string; scan_status: string; upload_status: string; findings_count: number; critical: number; high: number; medium: number; low: number; href: string };
  type Finding = { id: number; run_id: number; project_path: string; severity: string; rule_id: string; title: string; file_path: string | null; line: number | null; href: string };
  type Summary = { repositories: number; findings: number; critical: number; high: number; medium: number; low: number; info: number };
  type Payload = { active_run_id?: number | null; latest_run_id?: number | null; selected_run_id: number | null; summary: Summary | null; runs: Run[]; repositories: RepositoryRow[]; findings: Finding[] };
  const { data, state, retry } = useLiveData<Payload>("/api/ui/iac");
  const latest = data?.runs.find((run) => run.id === data.selected_run_id) ?? data?.runs[0] ?? null;
  const activeRun = data?.runs.find((run) => run.id === data.active_run_id)
    ?? data?.runs.find((run) => ["queued", "requested", "registered", "running", "polling", "processing", "in_progress"].includes(run.status.toLowerCase()))
    ?? null;
  const latestActivity = data?.runs.find((run) => run.id === data.latest_run_id) ?? data?.runs[0] ?? null;
  const selectedRunId = data?.selected_run_id ?? latest?.id ?? null;
  const repositories = (data?.repositories ?? []).filter((repository) => !selectedRunId || repository.run_id === selectedRunId);
  const findings = (data?.findings ?? []).filter((finding) => !selectedRunId || finding.run_id === selectedRunId);
  const summary = data?.summary;
  const selectedIsActive = Boolean(activeRun && latest?.id === activeRun.id);
  const hasEvidence = repositories.length > 0 || findings.length > 0 || (summary?.repositories ?? 0) > 0 || (summary?.findings ?? 0) > 0;
  const modules = repositories.slice(0, 5).map((repository, index) => ({
    ...repository,
    name: repository.project_path.split("/").at(-1) ?? repository.project_path,
    x: [16, 44, 70, 28, 63][index],
    y: [24, 17, 31, 66, 72][index],
    state: repository.critical ? "risk" : repository.high || repository.medium ? "warn" : "ok",
  }));
  const critical = summary?.critical ?? 0;
  const high = summary?.high ?? 0;
  const medium = summary?.medium ?? 0;
  const low = summary?.low ?? 0;
  const info = summary?.info ?? 0;
  const hasSevereEvidence = critical + high > 0;
  const selectedEvidenceIncomplete = Boolean(
    latest && ["partial", "failed", "cancelled"].includes(latest.status),
  );
  const headline = activeRun
    ? "Infrastructure, scanning."
    : latestActivity?.status === "failed" || latestActivity?.status === "partial" || hasSevereEvidence
      ? "Infrastructure needs attention."
      : latestActivity?.status === "cancelled"
        ? "Infrastructure scan cancelled."
        : latest
          ? "Infrastructure, resolved."
          : "Infrastructure, waiting.";
  const contextRun = activeRun ?? latestActivity ?? latest;
  return (
    <div className="view">
      <ScreenHeader signal="Infrastructure as code" title={headline} copy="Checkov repository scans, upload state and real policy findings across the selected GitLab scope." primary="Run or manage scans" primaryHref="/iac/operator" secondary="All repositories" secondaryHref="/iac/repositories" />
      {!latest && <EmptyState state={state} title="No IaC scan yet" copy="Run Checkov from the IaC workbench to populate repository and finding state." onRetry={retry} />}
      {activeRun && latest && activeRun.id !== latest.id && <div className="run-context" role="status"><i /><span><strong>IaC run #{activeRun.id} is {activeRun.status.replaceAll("_", " ")}</strong><small>Showing verified evidence from completed run #{latest.id} below</small></span><a href={activeRun.href}>Open active run ↗</a></div>}
      {!activeRun && latestActivity && latest && latestActivity.id !== latest.id && <div className="run-context is-muted" role="status"><i /><span><strong>Latest run #{latestActivity.id} was {latestActivity.status.replaceAll("_", " ")}</strong><small>Showing the latest authoritative evidence from run #{latest.id}</small></span><a href={latestActivity.href}>Open latest run ↗</a></div>}
      {latest && selectedIsActive && !hasEvidence && <div className="live-state live-state-progress" role="status">
        <Aperture small />
        <strong>IaC run #{latest.id} is in progress</strong>
        <span>Checkov has not persisted repository or finding evidence yet. This view will populate as the active run reports results.</span>
      </div>}
      {latest && (!selectedIsActive || hasEvidence) && <>
      <section className="iac-grid">
        <article className="module-map">
          <div className="surface-heading"><div><span>{selectedIsActive ? "REPOSITORY GRAPH · IN PROGRESS" : `REPOSITORY GRAPH · ${modules.length} OF ${summary?.repositories ?? repositories.length} SHOWN`}</span><h2>{latest.target}</h2></div><Status value={latest.status} /></div>
          <div className="blueprint-grid">
            <svg className="blueprint-lines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">{modules.map((module) => <line x1="50" y1="50" x2={module.x} y2={module.y} key={module.id} />)}</svg>
            {modules.map((module) => <a href={module.href} className={`module-node ${module.state}`} style={{ left: `${module.x}%`, top: `${module.y}%` }} key={module.id}><i /><strong>{module.name}</strong><small>{module.findings_count} findings</small></a>)}
            <div className="graph-core"><Aperture small /><span>root</span></div>
          </div>
          <div className="map-legend"><span><i className="clear" />{repositories.filter((row) => !row.findings_count).length} clear</span><span><i className="warning" />{repositories.filter((row) => row.findings_count && !row.critical).length} review</span><span><i className="critical" />{repositories.filter((row) => row.critical).length} critical</span></div>
        </article>
        <aside className="risk-stack">
          <div className="surface-heading"><div><span>{selectedIsActive ? "RISK DENSITY · PROVISIONAL" : "RISK DENSITY · SELECTED RUN"}</span><h2>{summary?.findings ?? latest.findings_count} findings</h2></div><b>{summary?.repositories ?? latest.scanned_repositories} repos</b></div>
          <div className="severity-columns"><span><i style={{ height: `${Math.min(100, Math.max(8, critical))}%` }} /><b>{critical}</b><small>Critical</small></span><span><i style={{ height: `${Math.min(100, Math.max(8, high))}%` }} /><b>{high}</b><small>High</small></span><span><i style={{ height: `${Math.min(100, Math.max(8, medium))}%` }} /><b>{medium}</b><small>Medium</small></span><span><i style={{ height: `${Math.min(100, Math.max(8, low))}%` }} /><b>{low}</b><small>Low</small></span><span><i style={{ height: `${Math.min(100, Math.max(8, info))}%` }} /><b>{info}</b><small>Info</small></span></div>
          <div className="risk-callout"><span>HIGHEST IMPACT</span><strong>{findings[0]?.title ?? (selectedIsActive ? "No findings reported yet" : selectedEvidenceIncomplete ? "Finding evidence incomplete" : "No findings")}</strong><p>{findings[0] ? `${findings[0].rule_id} · ${findings[0].file_path ?? findings[0].project_path}${findings[0].line ? `:${findings[0].line}` : ""}` : selectedIsActive ? "The active run is still collecting policy evidence." : selectedEvidenceIncomplete ? `Run ${latest?.status ?? "failed"} did not persist a complete policy result.` : "The latest scan did not report a policy violation."}</p>{findings[0] && <a href={findings[0].href}>Inspect finding →</a>}</div>
        </aside>
      </section>
      <section className="iac-findings">
        <div className="surface-heading"><div><span>{selectedIsActive ? "POLICY FINDINGS · IN PROGRESS" : "POLICY FINDINGS"}</span><h2>{selectedIsActive ? "Evidence observed so far" : "Latest Checkov evidence"}</h2></div><a href="/iac/findings">All findings ↗</a></div>
        {findings.slice(0, 8).map((finding) => <a href={finding.href} key={finding.id}><code title={finding.rule_id}>{finding.rule_id}</code><span><strong>{finding.title}</strong><small>{finding.project_path}</small></span><Status value={finding.severity === "critical" || finding.severity === "high" ? "failed" : "partial"} /><b>{finding.file_path ?? "repository"}</b><em>›</em></a>)}
        {!findings.length && <div className="ledger-empty">{selectedIsActive ? "No policy findings have been persisted for this active run yet." : selectedEvidenceIncomplete ? "This run did not persist complete policy evidence." : "This run did not report policy findings."}</div>}
      </section>
      </>}
    </div>
  );
}

function PTAI() {
  type Scan = { id: number; repository: string | null; product: string | null; branch: string | null; status: string; canonical_status?: string; failure_source?: string | null; outcome_reason?: string | null; trigger_failed: boolean; is_active: boolean; ptai_stage: string | null; progress: number | null; has_errors: boolean | null; error: string | null; created_at: string; updated_at: string | null; finished_at: string | null; job_web_url: string | null; asoc_repository_url: string | null; ptai_result_url: string | null; href: string };
  type Filters = { status: string; repo: string; branch: string; problem: boolean; page: number; pageSize: PageSize };
  type Payload = {
    rows: Scan[];
    summary: { total: number; returned: number; active: number };
    matching: number;
    pagination: { page: number; page_size: number; total_pages: number; start: number; end: number };
    filters: Filters;
  };
  const readFilters = (): Filters => {
    const params = new URLSearchParams(window.location.search);
    const status = (params.get("status") ?? "all").toUpperCase();
    return {
      status: ["ALL", "PENDING", "IN_PROGRESS", "SUCCESS", "PARTIAL_SUCCESS", "ERROR"].includes(status) ? status : "ALL",
      repo: params.get("repo") ?? "",
      branch: params.get("branch") ?? "",
      problem: params.get("problem") === "true",
      page: Math.max(1, Number(params.get("page")) || 1),
      pageSize: pageSizeFrom(params.get("page_size")),
    };
  };
  const [filters, setFilters] = useState<Filters>(readFilters);
  const [repoDraft, setRepoDraft] = useState(filters.repo);
  const [branchDraft, setBranchDraft] = useState(filters.branch);
  const query = useMemo(() => {
    const params = new URLSearchParams({ page: String(filters.page), page_size: String(filters.pageSize) });
    if (filters.status !== "ALL") params.set("status", filters.status.toLowerCase());
    if (filters.repo) params.set("repo", filters.repo);
    if (filters.branch) params.set("branch", filters.branch);
    if (filters.problem) params.set("problem", "true");
    return params.toString();
  }, [filters]);
  const { data, state, retry } = useLiveData<Payload>(`/api/ui/ptai?${query}`);
  const scans = data?.rows ?? [];
  const ptaiRepositoryOptions = Array.from(new Set(
    scans
      .map((scan) => scan.repository)
      .filter((value): value is string => Boolean(value)),
  )).map((value) => ({ value, label: value }));
  const ptaiBranchOptions = Array.from(new Set(
    scans
      .map((scan) => scan.branch)
      .filter((value): value is string => Boolean(value)),
  )).map((value) => ({ value, label: value }));
  const summary = data?.summary ?? { total: scans.length, returned: scans.length, active: scans.filter((scan) => scan.is_active).length };
  const matching = data?.matching ?? scans.length;
  const shortRepository = (value: string | null, product: string | null, id: number) => {
    const full = value ?? product ?? `Scan #${id}`;
    const parts = full.split('/').filter(Boolean);
    return { full, name: parts.at(-1) ?? full, scope: parts.length > 1 ? parts.slice(0, -1).join('/') : null };
  };
  const scanOutcome = (scan: Scan) => {
    if (scan.canonical_status) {
      if (scan.canonical_status === "ERROR") return { label: "Needs attention", statusLabel: "Failed", description: scan.outcome_reason ?? "The scan could not complete.", tone: "failed", terminal: true, queueLabel: "Alert" };
      if (scan.canonical_status === "PARTIAL_SUCCESS") return { label: "Completed with warnings", statusLabel: "Warning", description: scan.outcome_reason ?? "The scan completed with an incomplete result.", tone: "partial", terminal: true, queueLabel: "Alert" };
      if (scan.canonical_status === "SUCCESS") return { label: "Completed successfully", statusLabel: "Completed", description: "The scan completed successfully.", tone: "succeeded", terminal: true, queueLabel: "Done" };
      if (scan.canonical_status === "IN_PROGRESS") return { label: "Scan in progress", statusLabel: "Running", description: "ScanOps is still collecting scan status.", tone: "running", terminal: false, queueLabel: scan.progress == null ? "Running" : `${scan.progress}%` };
    }
    const engineStage = scan.ptai_stage?.toLowerCase();
    const asocStatus = scan.status.toUpperCase();
    if (scan.trigger_failed || asocStatus === "TRIGGER_FAILED") {
      return { label: "Trigger failed", statusLabel: "Failed", description: "ASOC did not accept the scan request. No PT AI result is available.", tone: "failed", terminal: true, queueLabel: "Start" };
    }
    if (engineStage === "failed" || engineStage === "aborted" || asocStatus === "ERROR") {
      return { label: "Needs attention", statusLabel: "Failed", description: "The scan could not complete. Open the evidence to identify the failed system step.", tone: "failed", terminal: true, queueLabel: "Alert" };
    }
    if (scan.has_errors && !scan.is_active || asocStatus === "PARTIAL_SUCCESS") {
      return { label: "Completed with warnings", statusLabel: "Warning", description: "PT AI finished, but it reported an issue that may affect completeness of the result.", tone: "partial", terminal: true, queueLabel: "Alert" };
    }
    if (engineStage === "done" || asocStatus === "SUCCESS" || Boolean(scan.finished_at)) {
      return { label: "Completed successfully", statusLabel: "Completed", description: "GitLab, ASOC and PT AI have reported a completed scan with no known engine errors.", tone: "succeeded", terminal: true, queueLabel: "Done" };
    }
    if (scan.is_active || asocStatus === "IN_PROGRESS") {
      return { label: "Scan in progress", statusLabel: "Running", description: "ScanOps is still collecting status from ASOC and the PT AI engine.", tone: "running", terminal: false, queueLabel: scan.progress == null ? "Running" : `${scan.progress}%` };
    }
    return { label: "Waiting for status", statusLabel: "Waiting", description: "The trigger was captured, but a final scan status has not been reported yet.", tone: "running", terminal: false, queueLabel: "Waiting" };
  };
  const isCompleted = (scan: Scan) => {
    const engineStage = scan.ptai_stage?.toLowerCase();
    const asocStatus = scan.status.toUpperCase();
    const failed = scan.trigger_failed || asocStatus === "TRIGGER_FAILED" || asocStatus === "ERROR" || engineStage === "failed" || engineStage === "aborted";
    return !scan.is_active && !failed
      && (engineStage === "done" || asocStatus === "SUCCESS" || asocStatus === "PARTIAL_SUCCESS");
  };
  const attentionCount = scans.filter((scan) => {
    const tone = scanOutcome(scan).tone;
    return tone === "failed" || tone === "partial";
  }).length;

  const applyFilters = (next: Partial<Filters>, replace = false) => {
    const updated = { ...filters, ...next };
    const url = new URL(window.location.href);
    if (updated.status === "ALL") url.searchParams.delete("status");
    else url.searchParams.set("status", updated.status.toLowerCase());
    if (updated.repo) url.searchParams.set("repo", updated.repo);
    else url.searchParams.delete("repo");
    if (updated.branch) url.searchParams.set("branch", updated.branch);
    else url.searchParams.delete("branch");
    if (updated.problem) url.searchParams.set("problem", "true");
    else url.searchParams.delete("problem");
    if (updated.page > 1) url.searchParams.set("page", String(updated.page));
    else url.searchParams.delete("page");
    if (updated.pageSize !== 25) url.searchParams.set("page_size", String(updated.pageSize));
    else url.searchParams.delete("page_size");
    window.history[replace ? "replaceState" : "pushState"]({}, "", url);
    setFilters(updated);
  };

  const resetFilters = () => {
    setRepoDraft("");
    setBranchDraft("");
    applyFilters({ status: "ALL", repo: "", branch: "", problem: false, page: 1 });
  };

  useEffect(() => {
    const onPopState = () => {
      const next = readFilters();
      setFilters(next);
      setRepoDraft(next.repo);
      setBranchDraft(next.branch);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const effectivePage = data?.pagination?.page;
    if (!effectivePage || effectivePage === filters.page) return;
    const url = new URL(window.location.href);
    if (effectivePage > 1) url.searchParams.set("page", String(effectivePage));
    else url.searchParams.delete("page");
    window.history.replaceState({}, "", url);
    setFilters((current) => ({ ...current, page: effectivePage }));
  }, [data?.pagination?.page, filters.page]);

  useEffect(() => {
    const repo = repoDraft.trim();
    const branch = branchDraft.trim();
    if (repo === filters.repo && branch === filters.branch) return;
    const timeout = window.setTimeout(() => {
      applyFilters({ repo, branch, page: 1 }, true);
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [repoDraft, branchDraft, filters.repo, filters.branch]);

  const hasActiveFilters = filters.status !== "ALL"
    || Boolean(repoDraft.trim())
    || Boolean(branchDraft.trim())
    || filters.problem;
  return (
    <div className="view">
      <ScreenHeader signal="Application analysis" title="PT AI scans" copy="Find a scan, see its outcome, and open the evidence only when it needs attention." primary={null} secondary={`${matching} matching · ${summary.total} total`} />
      <div className="filter-surface ptai-filter-bar" aria-label="PT AI scan filters">
        <label className="filter-field"><span>Status</span><SignalSelect ariaLabel="PT AI status" value={filters.status} options={[{ value: "ALL", label: "All states" }, { value: "PENDING", label: "Pending" }, { value: "IN_PROGRESS", label: "In progress" }, { value: "SUCCESS", label: "Success" }, { value: "PARTIAL_SUCCESS", label: "Partial success" }, { value: "ERROR", label: "Error" }]} onChange={(value) => applyFilters({ status: value, page: 1 })} /></label>
        <label className="filter-field"><span>Repository</span><SearchableSelect ariaLabel="PT AI repository" value={repoDraft} options={ptaiRepositoryOptions} onChange={setRepoDraft} placeholder="Repository path" commitOnEnter={false} /></label>
        <label className="filter-field"><span>Branch</span><SearchableSelect ariaLabel="PT AI branch" value={branchDraft} options={ptaiBranchOptions} onChange={setBranchDraft} placeholder="Branch" commitOnEnter={false} /></label>
        <label className="filter-field filter-toggle-field"><span>Attention</span><span className="filter-toggle"><input type="checkbox" checked={filters.problem} onChange={(event) => applyFilters({ problem: event.target.checked, page: 1 })} /><b>Problems only</b></span></label>
        {hasActiveFilters && <button className="filter-clear ptai-clear-filters" type="button" onClick={resetFilters}>Clear filters</button>}
      </div>
      <div className="ptai-register-summary" aria-label="PT AI scan summary"><span><b>{matching}</b> scans match</span><span><b>{summary.active}</b> active</span><span className={attentionCount ? "has-attention" : ""}><b>{attentionCount}</b> need review on this page</span></div>
      {!scans.length && <EmptyState state={state} title="No PT AI scans" copy="PT AI scan telemetry will appear after a GitLab ptai_scan job is ingested." onRetry={retry} />}
      {!!scans.length && <section className="scan-queue ptai-register ptai-register-full" aria-label="PT AI scan register">
          <div className="surface-heading"><div><span>SCAN REGISTER</span><h2>Recent scans</h2></div><b>{summary.returned} shown</b></div>
          <div className="ptai-register-columns" aria-hidden="true"><span>Status</span><span>Target</span><span>GitLab</span><span>ASOC</span><span>PT AI</span><span>Progress</span><span>Updated</span></div>
          <div className="scan-queue-list">
            {scans.map((scan) => {
              const outcome = scanOutcome(scan);
              const rowProgress = isCompleted(scan) ? 100 : scan.progress;
              const target = shortRepository(scan.repository, scan.product, scan.id);
              return <article className="ptai-register-row" key={scan.id}>
                <Status value={outcome.statusLabel} />
                <a className="ptai-scan-select" href={scan.href} title={target.full}><strong>{target.name}</strong><small>{target.scope ?? "PT AI scan"} · {scan.branch ?? "no branch"} · #{scan.id}</small></a>
                <span className="ptai-status-link ptai-status-link-gitlab"><span className="ptai-mobile-label">GitLab</span>{scan.job_web_url ? <a className="ptai-status-anchor" href={scan.job_web_url} target="_blank" rel="noopener noreferrer" aria-label={`Open GitLab job for scan ${scan.id}`}><Status value={scan.trigger_failed ? "failed" : "accepted"} /></a> : <span>—</span>}</span>
                <span className="ptai-status-link ptai-status-link-asoc"><span className="ptai-mobile-label">ASOC</span>{scan.asoc_repository_url ? <a className="ptai-status-anchor" href={scan.asoc_repository_url} target="_blank" rel="noopener noreferrer" aria-label={`Open ASOC repository for scan ${scan.id}`}><Status value={scan.status} /></a> : <Status value={scan.status} />}</span>
                <span className="ptai-status-link ptai-status-link-engine"><span className="ptai-mobile-label">PT AI</span>{scan.ptai_result_url ? <a className="ptai-status-anchor" href={scan.ptai_result_url} target="_blank" rel="noopener noreferrer" aria-label={`Open PT AI result for scan ${scan.id}`}><Status value={scan.ptai_stage ?? "not reported"} /></a> : <Status value={scan.ptai_stage ?? "not reported"} />}</span>
                <span className="ptai-progress-cell"><b>{rowProgress == null ? "—" : `${rowProgress}%`}</b><small>{outcome.queueLabel === "Done" ? relativeTime(scan.finished_at ?? scan.updated_at ?? scan.created_at) : outcome.queueLabel}</small></span>
                <time className="ptai-updated-cell" dateTime={scan.updated_at ?? scan.created_at}>{relativeTime(scan.updated_at ?? scan.created_at)}</time>
              </article>;
            })}
          </div>
          <PaginationBar
            ariaLabel="PT AI scans"
            page={data?.pagination?.page ?? filters.page}
            totalPages={data?.pagination?.total_pages ?? 1}
            pageSize={pageSizeFrom(String(data?.pagination?.page_size ?? filters.pageSize))}
            start={data?.pagination?.start ?? 0}
            end={data?.pagination?.end ?? 0}
            total={matching}
            onPageChange={(page) => applyFilters({ page })}
            onPageSizeChange={(pageSize) => applyFilters({ pageSize, page: 1 })}
            embedded
          />
        </section>}
    </div>
  );
}

function Coverage() {
  type Row = { product_id: number; product: string; repositories: number; audit_covered: number; audit_total: number; controls_expected: number; controls_configured: number; controls_missing: number; last_scanned_at: string | null; href: string };
  type Payload = { summary: { covered: number; pipeline_no_sec: number; no_pipelines: number; inaccessible: number; excluded: number; controls_expected: number; controls_configured: number; control_rate: number }; rows: Row[] };
  const { data, state, retry } = useLiveData<Payload>("/api/ui/coverage");
  const rows = data?.rows ?? [];
  const priority = [...rows]
    .filter((row) => row.controls_missing > 0)
    .sort((a, b) => b.controls_missing - a.controls_missing)[0]
    ?? null;
  const rate = data?.summary.control_rate ?? 0;
  const auditCovered = rows.reduce((total, row) => total + row.audit_covered, 0);
  const auditTotal = rows.reduce((total, row) => total + row.audit_total, 0);
  const auditGaps = Math.max(auditTotal - auditCovered, 0);
  return (
    <div className="view">
      <ScreenHeader signal="Security posture" title="What is protected." copy="Audit observations and configured security-control bindings across every real product." primary="Run coverage audit" primaryHref="/coverage/operator" secondary={`${rows.length} products`} />
      {!rows.length && <EmptyState state={state} title="No coverage evidence yet" copy="Coverage will resolve after products, repositories and audit observations are available." onRetry={retry} />}
      {!!rows.length && <>
      <section className="coverage-top">
        <article className="coverage-score">
          <span>CONTROL COVERAGE</span><strong>{rate}<small>%</small></strong><p>{data?.summary.controls_configured ?? 0} of {data?.summary.controls_expected ?? 0} required control bindings are configured.</p><div><i style={{ width: `${rate}%` }} /></div>
        </article>
        <article className="coverage-pulse"><div className="surface-heading"><div><span>REPOSITORY AUDIT EVIDENCE</span><h2>{auditCovered} of {auditTotal} repositories observed</h2></div><b>{auditGaps} without confirmed evidence</b></div><div className="mini-trend">{rows.slice(0, 15).map((row) => <i title={`${row.product}: ${row.audit_covered} of ${row.audit_total} repositories observed`} style={{ height: `${row.audit_total ? Math.max(8, row.audit_covered / row.audit_total * 100) : 8}%` }} key={row.product_id} />)}</div></article>
        <aside className="coverage-gap"><span>PRIORITY GAP</span><strong>{priority?.product ?? "No product gaps"}</strong><p>{priority ? `${priority.controls_missing} required bindings are not configured across ${priority.repositories} repositories.` : "All expected controls are configured."}</p>{priority && <a href={priority.href}>Investigate →</a>}</aside>
      </section>
      <section className="coverage-matrix">
        <div className="coverage-matrix-heading"><div><span>PRODUCT COVERAGE</span><strong>Repository audit and control bindings</strong></div><p>Audit is counted per repository. Configured and missing are counted per required control binding.</p></div>
        <div className="matrix-head"><span>Product</span><span>Audit evidence</span><span>Configured controls</span><span>Control gaps</span><span>Repositories</span><span>Freshness</span><span>Binding coverage</span></div>
        {rows.map((row) => {
          const controlRate = row.controls_expected ? Math.round(row.controls_configured / row.controls_expected * 100) : 0;
          return <a href={row.href} key={row.product_id}><strong>{row.product}</strong><span className={`matrix-state ${row.audit_total && row.audit_covered === row.audit_total ? "healthy" : row.audit_covered ? "warning" : "missing"}`}><i />{row.audit_covered} / {row.audit_total} repos</span><span className="matrix-state healthy"><i />{row.controls_configured} / {row.controls_expected} bindings</span><span className={`matrix-state ${row.controls_missing ? "missing" : "healthy"}`}><i />{row.controls_missing} bindings</span><span>{row.repositories} repos</span><span>{relativeTime(row.last_scanned_at)}</span><b>{controlRate}%</b></a>;
        })}
        <div className="matrix-legend"><span><i className="healthy" />complete / configured</span><span><i className="warning" />partial repository audit</span><span><i className="missing" />missing evidence or binding</span></div>
      </section>
      </>}
    </div>
  );
}

function Repositories() {
  type RepositoryRow = { id: number; path: string; product_id: number; product: string | null; status: string; last_report_at: string | null; jobs_total: number; jobs_failed: number; success_rate: number; job_health_healthy: number; job_health_broken: number; job_health_total: number; coverage: { expected: number; configured: number; missing: number; excluded: number }; href: string };
  type RepositoryFilters = { q: string; product: string; page: number; pageSize: PageSize };
  type RepositoryPayload = {
    rows: RepositoryRow[];
    total: number;
    matching: number;
    returned: number;
    pagination: { page: number; page_size: PageSize; total_pages: number; start: number; end: number };
    filters: { q: string; product: number | null };
  };
  const readFilters = (): RepositoryFilters => {
    const params = new URLSearchParams(window.location.search);
    const productValue = params.get("product") ?? "";
    return {
      q: params.get("q") ?? "",
      product: /^\d+$/.test(productValue) && Number(productValue) > 0 ? String(Number(productValue)) : "",
      page: Math.max(1, Number(params.get("page")) || 1),
      pageSize: pageSizeFrom(params.get("page_size")),
    };
  };
  const [filters, setFilters] = useState<RepositoryFilters>(readFilters);
  const [searchDraft, setSearchDraft] = useState(filters.q);
  const query = useMemo(() => {
    const params = new URLSearchParams({
      page: String(filters.page),
      page_size: String(filters.pageSize),
    });
    if (filters.q) params.set("q", filters.q);
    if (filters.product) params.set("product", filters.product);
    return params.toString();
  }, [filters]);
  const { data, state, retry } = useLiveData<RepositoryPayload>(`/api/ui/repositories?${query}`, 60_000);
  const rows = data?.rows ?? [];
  const repositorySearchOptions = Array.from(new Set(
    rows.flatMap((row) => [row.path, row.product].filter((value): value is string => Boolean(value))),
  )).map((value) => ({ value, label: value }));

  useEffect(() => {
    const effectivePage = data?.pagination?.page;
    if (!effectivePage || effectivePage === filters.page) return;
    const url = new URL(window.location.href);
    if (effectivePage > 1) url.searchParams.set("page", String(effectivePage));
    else url.searchParams.delete("page");
    window.history.replaceState({}, "", url);
    setFilters((current) => ({ ...current, page: effectivePage }));
  }, [data?.pagination?.page, filters.page]);

  useEffect(() => {
    const onPopState = () => {
      const next = readFilters();
      setFilters(next);
      setSearchDraft(next.q);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const applyFilters = (next: Partial<RepositoryFilters>) => {
    const updated = { ...filters, ...next };
    const url = new URL(window.location.href);
    url.searchParams.set("level", "repository");
    if (updated.q) url.searchParams.set("q", updated.q);
    else url.searchParams.delete("q");
    if (updated.product) url.searchParams.set("product", updated.product);
    else url.searchParams.delete("product");
    if (updated.page > 1) url.searchParams.set("page", String(updated.page));
    else url.searchParams.delete("page");
    if (updated.pageSize === 25) url.searchParams.delete("page_size");
    else url.searchParams.set("page_size", String(updated.pageSize));
    url.searchParams.delete("repository");
    window.history.pushState({}, "", url);
    setFilters(updated);
  };

  return (
    <div className="view">
      <ScreenHeader signal="Repository intelligence" title="Every repository, one story." copy="Ownership, job health, required control bindings and the latest evidence signal in one inventory." primary={null} secondary={`${data?.matching ?? "—"} matching · ${data?.total ?? "—"} total`} />
      <section className="repository-registry">
          <div className="surface-heading repository-heading"><div><span>REPOSITORIES · STATUS = UNRESOLVED JOB HEALTH</span><h2>Production targets</h2></div><span className="scope-total"><strong>{data?.returned ?? "—"} shown</strong><small>{data?.pagination?.start ?? 0}–{data?.pagination?.end ?? 0} of {data?.matching ?? 0} matching</small></span></div>
          <form className="repository-filter-bar" onSubmit={(event) => {
            event.preventDefault();
            applyFilters({ q: searchDraft.trim(), page: 1 });
          }}>
            <label className="target-search"><span aria-hidden="true">⌕</span><SearchableSelect ariaLabel="Search repositories" value={searchDraft} options={repositorySearchOptions} onChange={setSearchDraft} placeholder="Path or product" commitOnEnter={false} /></label>
            <button type="submit">Apply</button>
            {filters.q && <button type="button" onClick={() => {
              setSearchDraft("");
              applyFilters({ q: "", page: 1 });
            }}>Clear</button>}
          </form>
          {state === "loading" && <EmptyState state={state} title="Loading repositories" copy="Reading the selected inventory page." />}
          {state === "error" && <EmptyState state={state} title="Repository inventory unavailable" copy="The last request could not be completed." onRetry={retry} />}
          {state === "ready" && !rows.length && <EmptyState state={state} title={data?.total ? "No repositories match" : "No repositories discovered"} copy={data?.total ? "Adjust the search while keeping the current product scope." : "Repositories appear after the first GitLab project signal is ingested."} />}
          {!!rows.length && <div className="repository-table">
          <div className="repository-table-head"><span>Repository</span><span>Product</span><span>Current job-key health</span><span>Control bindings</span><span>Last signal</span><span>Health</span><span>Actions</span></div>
          {rows.map((item) => {
            const coverage = item.coverage.expected ? Math.round(item.coverage.configured / item.coverage.expected * 100) : 0;
            const tone = item.job_health_broken ? "failed" : item.job_health_total ? "healthy" : "unknown";
            return <div className="repository-row" key={item.id}>
              <a className="repository-identity" href={item.href}><span className={`target-beacon ${tone}`}><i /></span><span><strong>{item.path}</strong><small>Repository #{item.id}</small></span></a>
              <a className="repository-product" href={`/targets/products/${item.product_id}`}>{item.product ?? "Unassigned product"}</a>
              <span className="repository-stat"><strong>{item.job_health_total ? `${item.success_rate}%` : "—"}</strong><small>{item.job_health_healthy} healthy · {item.job_health_broken} unresolved failures</small>{item.job_health_broken > 0 && <a href={`/repos/${item.id}/jobs?only_failed=true&status=failed&timeframe=all`}>View unresolved jobs ↗</a>}</span>
              <span className="repository-stat"><strong>{item.coverage.configured} / {item.coverage.expected}</strong><small>{item.coverage.missing} missing · {coverage}%</small></span>
              <time dateTime={item.last_report_at ?? undefined}>{relativeTime(item.last_report_at)}</time>
              <Status
                value={tone}
                label={tone === "failed" ? "Needs review" : undefined}
                title={tone === "failed" ? "At least one job and ref has an unresolved failure; this does not mean the latest pipeline failed." : undefined}
              />
              <span className="repository-actions"><a href={item.href}>Repository 360</a><a href={`/repos/${item.id}/jobs`}>Jobs</a><a href={`/executions?repository=${item.id}`}>Executions</a></span>
            </div>;
          })}
          </div>}
          <PaginationBar
            ariaLabel="Repositories"
            page={data?.pagination?.page ?? filters.page}
            totalPages={data?.pagination?.total_pages ?? 1}
            pageSize={data?.pagination?.page_size ?? filters.pageSize}
            start={data?.pagination?.start ?? 0}
            end={data?.pagination?.end ?? 0}
            total={data?.matching ?? 0}
            onPageChange={(page) => applyFilters({ page })}
            onPageSizeChange={(pageSize) => applyFilters({ pageSize, page: 1 })}
            embedded
          />
      </section>
    </div>
  );
}

function ScannerFleet() {
  type ConnectorRow = { id: number; key: string; name: string; type: string; endpoint: string | null; enabled: boolean; status: string; reason: string | null; last_seen_at: string | null; instances: number };
  type InstanceRow = { id: number; connector_id: number; key: string; name: string; type: string; endpoint: string | null; version: string | null; capabilities: string[]; status: string; reason: string | null; last_seen_at: string | null };
  type WorkerRow = { name: string; last_beat_at: string; status: string; stale: boolean; age_seconds: number };
  type RuntimeNode = { id: number | string; name: string; type: string; status: string; version: string | null; capabilities: string[]; last_seen_at: string | null; stale: boolean; source: "instance" | "worker"; connectorKey?: string };
  type Payload = { summary: { source: string; total: number; returned: number; healthy: number }; connectors: ConnectorRow[]; instances: InstanceRow[]; workers: WorkerRow[] };
  const { data, state, retry } = useLiveData<Payload>("/api/ui/scanner-fleet");
  const instances = data?.instances ?? [];
  const workers = data?.workers ?? [];
  const representedWorkers = new Set(
    instances
      .filter((instance) => instance.type === "worker")
      .map((instance) => instance.key.replace(/^worker:/, "")),
  );
  const supplementalWorkers: RuntimeNode[] = workers
    .filter((worker) => !representedWorkers.has(worker.name))
    .map((worker) => ({ id: `worker:${worker.name}`, name: worker.name, type: "worker", status: worker.stale ? "stale" : worker.status, version: null, capabilities: [], last_seen_at: worker.last_beat_at, stale: worker.stale, source: "worker" }));
  const connectorById = new Map((data?.connectors ?? []).map((connector) => [connector.id, connector]));
  const gitlabConnector = (data?.connectors ?? []).find((connector) => connector.key === "gitlab");
  const instanceNodes: RuntimeNode[] = instances.map((instance) => ({
    ...instance,
    connectorKey: connectorById.get(instance.connector_id)?.key,
    stale: statusTone(instance.status) === "partial",
    source: "instance",
  }));
  const allNodes = [...supplementalWorkers, ...instanceNodes];
  const topologyNodes = allNodes.slice(0, 6);
  const topologySignature = JSON.stringify(topologyNodes.map((node) => node.id));
  const fleetFieldRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const field = fleetFieldRef.current;
    if (!field || !topologyNodes.length) return undefined;

    const syncScanGeometry = () => {
      const fieldRect = field.getBoundingClientRect();
      const centerX = fieldRect.left + fieldRect.width / 2;
      const centerY = fieldRect.top + fieldRect.height / 2;
      const links = field.querySelectorAll<HTMLElement>(".fleet-links > i");
      let furthestSignal = 0;

      field.querySelectorAll<HTMLElement>(".fleet-node").forEach((node, index) => {
        const signal = node.querySelector<HTMLElement>(":scope > i");
        if (!signal) return;
        const signalRect = signal.getBoundingClientRect();
        const deltaX = signalRect.left + signalRect.width / 2 - centerX;
        const deltaY = signalRect.top + signalRect.height / 2 - centerY;
        const angle = (Math.atan2(deltaX, -deltaY) * 180 / Math.PI + 360) % 360;
        const leadingEdgeDelay = ((angle - 22.5 + 360) % 360) / 45;
        node.style.setProperty("--fleet-scan-delay", `${leadingEdgeDelay.toFixed(4)}s`);
        furthestSignal = Math.max(furthestSignal, Math.hypot(deltaX, deltaY));
        const link = links[index];
        if (link) {
          link.style.width = `${Math.hypot(deltaX, deltaY).toFixed(1)}px`;
          link.style.transform = `rotate(${Math.atan2(deltaY, deltaX) * 180 / Math.PI}deg)`;
        }
      });

      field.style.setProperty(
        "--fleet-sweep-diameter",
        `${Math.max(fieldRect.width, (furthestSignal + 24) * 2).toFixed(1)}px`,
      );
    };

    field.classList.remove("fleet-scan-ready");
    field.classList.add("fleet-scan-reset");
    syncScanGeometry();
    void field.offsetWidth;
    field.classList.remove("fleet-scan-reset");
    field.classList.add("fleet-scan-ready");
    const observer = new ResizeObserver(syncScanGeometry);
    observer.observe(field);
    return () => observer.disconnect();
  }, [topologySignature]);
  const instanceTotal = data?.summary.source === "scanner_instances"
    ? data.summary.total
    : instances.length;
  const instanceHealthy = data?.summary.source === "scanner_instances"
    ? data.summary.healthy
    : instances.filter((instance) => statusTone(instance.status) === "succeeded").length;
  const runtimeTotal = instanceTotal + supplementalWorkers.length;
  const healthy = instanceHealthy
    + supplementalWorkers.filter((worker) => !worker.stale && statusTone(worker.status) === "succeeded").length;
  const shownHealthy = topologyNodes.filter((node) => statusTone(node.status) === "succeeded").length;
  const runtimeLabel = "runtimes";
  const freshHeartbeats = workers.filter((worker) => !worker.stale).length;
  return (
    <div className="view">
      <ScreenHeader signal="Execution infrastructure" title="Compute at the ready." copy="Registered scanner adapters, live instances, versions, capabilities and worker heartbeats." primary="Adapter runtime details" primaryHref="/scanners/operator" secondary={`${runtimeTotal} ${runtimeLabel}`} />
      {!allNodes.length && <EmptyState state={state} title="No scanner runtime observed" copy="Configure a connector or start a worker to populate the fleet projection." onRetry={retry} />}
      {!!allNodes.length && <>
      <section className="fleet-grid">
        <article className="fleet-map">
          <div className="surface-heading"><div><span>FLEET TOPOLOGY</span><h2>{topologyNodes.length} of {runtimeTotal} {runtimeLabel} mapped</h2></div><Status value={healthy === runtimeTotal ? "healthy" : "partial"} /></div>
          <div className="fleet-field" ref={fleetFieldRef}>
            <div className="fleet-sweep" aria-hidden="true" />
            <div className="fleet-core"><Aperture /><strong>Control plane</strong><span className="fleet-core-meta"><small><b>{data?.connectors.length ?? 0}</b> connectors</small><i /><small><b>{runtimeTotal}</b> {runtimeLabel}</small></span></div>
            {topologyNodes.map((worker, index) => <div className={`fleet-node node-${index + 1} ${statusTone(worker.status)}`} key={worker.id}><i /><span><strong>{worker.name}</strong><small>{worker.type}</small></span></div>)}
            <div className="fleet-links" aria-hidden="true">{topologyNodes.map((worker) => <i key={worker.id} />)}</div>
          </div>
          <div className="fleet-summary"><span><b>{healthy}</b><small>healthy · all</small></span><span><b>{runtimeTotal}</b><small>{runtimeLabel} · all</small></span><span><b>{freshHeartbeats}</b><small>fresh heartbeats</small></span><span><b>{Math.max(runtimeTotal - healthy, 0)}</b><small>need review · all</small></span></div>
        </article>
        <aside className="capacity-panel">
          <div className="surface-heading"><div><span>RUNTIME SIGNAL · TOPOLOGY SAMPLE</span><h2>Observation freshness</h2></div><b>{shownHealthy === topologyNodes.length ? "normal" : "review"}</b></div>
          <div className="capacity-wave">{topologyNodes.flatMap((node, index) => [28 + index * 6, statusTone(node.status) === "succeeded" ? 74 : 34, 46 + index * 4]).map((height, index) => <i style={{ height: `${Math.min(height, 92)}%` }} key={index} />)}</div>
          <dl>
            <div><dt>Configured connectors</dt><dd>{data?.connectors.length ?? 0}</dd></div>
            <div><dt>Enabled connectors</dt><dd>{data?.connectors.filter((item) => item.enabled).length ?? 0}</dd></div>
            <div><dt>Scanner instances</dt><dd>{instances.length}</dd></div>
            <div><dt>Worker heartbeats</dt><dd>{workers.length}</dd></div>
            <div><dt title="Latest persisted job observation from a successful GitLab pipeline poll">GitLab job observation</dt><dd><time dateTime={gitlabConnector?.last_seen_at ?? undefined}>{gitlabConnector?.last_seen_at ? relativeTime(gitlabConnector.last_seen_at) : "not recorded"}</time></dd></div>
          </dl>
        </aside>
      </section>
      <section className="worker-table">
        <div className="surface-heading"><div><span>RUNTIME REGISTRY</span><h2>All {runtimeTotal} observed {runtimeLabel}</h2></div><a href="/integrations">Manage connectors</a></div>
        <div className="worker-head"><span>Runtime</span><span>Type</span><span>Health</span><span>Signal</span><span>Version / signal time</span></div>
        {allNodes.map((worker) => {
          const signalLabel = worker.source === "worker"
            ? worker.stale ? "stale heartbeat" : "fresh heartbeat"
            : worker.connectorKey === "gitlab"
              ? worker.last_seen_at ? "connection test" : "no connection test"
              : worker.last_seen_at ? "instance check" : "no signal";
          const signalAge = worker.connectorKey === "gitlab" && !worker.last_seen_at
            ? "not recorded"
            : relativeTime(worker.last_seen_at);
          return <div className={`runtime-row runtime-${statusTone(worker.status)}`} key={worker.id}><strong><i />{worker.name}</strong><span>{worker.type}</span><Status value={worker.status} /><span className="worker-meter"><i><b style={{ width: worker.source === "instance" ? `${Math.min(100, Math.max(12, worker.capabilities.length * 20))}%` : worker.stale ? "24%" : "100%" }} /></i>{worker.source === "instance" ? `${worker.capabilities.length} capabilities` : worker.stale ? "stale heartbeat" : "fresh heartbeat"}</span><span className="runtime-signal"><code>{worker.version ?? "—"}</code><small><time dateTime={worker.last_seen_at ?? undefined}>{signalLabel} · {signalAge}</time></small></span></div>;
        })}
      </section>
      </>}
    </div>
  );
}

function Integrations() {
  type IntegrationField = {
    name: string;
    label: string;
    kind: string;
    secret: boolean;
    value: string;
    configured: boolean;
  };
  type IntegrationInstance = {
    id: string;
    name: string;
    host: string;
    api_url: string;
    token_configured: boolean;
    enabled: boolean;
    polling_enabled: boolean;
    tested_at: string | null;
    test_status: string | null;
    test_message: string | null;
    last_success_at: string | null;
    last_success_message: string | null;
    last_successful_check: { at: string | null; message: string | null } | null;
    credential_ref: string;
    health_label: string;
  };
  type Connector = {
    key: string;
    name: string;
    role: string;
    detail: string;
    direction: "inbound" | "outbound" | "both";
    canonical_direction: string;
    configured: boolean;
    enabled: boolean;
    source: string;
    credential_ref: string;
    updated_at: string | null;
    updated_by: string | null;
    tested_at: string | null;
    test_status: string | null;
    test_message: string | null;
    health_status: string;
    health_reason: string;
    connection_summary: {
      total: number;
      configured: number;
      healthy: number;
      failed: number;
      untested: number;
      disabled: number;
    };
    last_successful_check: { at: string | null; message: string | null } | null;
    error: string | null;
    fields: IntegrationField[];
    instances: IntegrationInstance[];
    href: string;
  };
  type Payload = {
    management: {
      available: boolean;
      auth_ready: boolean;
      credential_store_ready: boolean;
    };
    summary: {
      integration_types: number;
      connections: number;
      configured: number;
      healthy: number;
      failed: number;
      untested: number;
      disabled: number;
    };
    rows: Connector[];
  };
  type Feedback = { tone: "success" | "error" | "warning"; message: string };
  const { data, state, retry } = useLiveData<Payload>("/api/ui/integrations", 60_000);
  const connectors = data?.rows ?? [];
  const summary = data?.summary ?? {
    integration_types: connectors.length,
    connections: connectors.reduce((total, connector) => total + (connector.connection_summary?.total ?? 1), 0),
    configured: connectors.reduce((total, connector) => total + (connector.connection_summary?.configured ?? Number(connector.configured)), 0),
    healthy: connectors.reduce((total, connector) => total + (connector.connection_summary?.healthy ?? 0), 0),
    failed: connectors.reduce((total, connector) => total + (connector.connection_summary?.failed ?? 0), 0),
    untested: connectors.reduce((total, connector) => total + (connector.connection_summary?.untested ?? 0), 0),
    disabled: connectors.reduce((total, connector) => total + (connector.connection_summary?.disabled ?? 0), 0),
  };
  const [selectedKey, setSelectedKey] = useState<string | null>(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("integration") ?? params.get("focus");
  });
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(() => new URLSearchParams(window.location.search).get("instance"));
  const [operation, setOperation] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  const feedbackRef = useRef<HTMLDivElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const formDirty = useRef(false);
  const openedFromWorkspace = useRef(false);
  const selectionUrl = useRef(window.location.href);
  const selected = connectors.find((connector) => connector.key === selectedKey) ?? null;
  const selectedInstance = selected?.key === "gitlab"
    ? selected.instances.find((instance) => instance.id === selectedInstanceId) ?? null
    : null;
  const creatingGitLab = selected?.key === "gitlab" && selectedInstanceId === "new";
  const canManage = data?.management.available ?? false;

  const exactSignalTime = (value?: string | null) => {
    if (!value) return "No signal";
    const timestamp = new Date(value);
    if (Number.isNaN(timestamp.getTime())) return "Invalid timestamp";
    return `${timestamp.toISOString().slice(0, 19).replace("T", " ")} UTC`;
  };

  const renderEvidence = (at?: string | null, message?: string | null) => (
    <dd className="integration-evidence-value">
      <time dateTime={at ?? undefined}>{exactSignalTime(at)}</time>
      {message && <small>{message}</small>}
    </dd>
  );

  const connectorState = (connector: Connector) => {
    if (connector.health_status) return connector.health_status;
    if (!connector.enabled) return "disabled";
    if (connector.error || connector.test_status === "failed") return "failed";
    if (connector.test_status === "healthy") return "healthy";
    return connector.configured ? "partial" : "unknown";
  };

  const instanceState = (instance: IntegrationInstance) => {
    if (!instance.enabled) return "disabled";
    if (instance.test_status === "failed") return "failed";
    if (instance.test_status === "healthy") return "healthy";
    return instance.token_configured ? "partial" : "unknown";
  };

  const writeSelection = (key: string | null, instance: string | null = null, replace = false) => {
    const url = new URL(window.location.href);
    url.searchParams.delete("focus");
    if (key) url.searchParams.set("integration", key);
    else url.searchParams.delete("integration");
    if (key === "gitlab" && instance) url.searchParams.set("instance", instance);
    else url.searchParams.delete("instance");
    const nextState = {
      ...(window.history.state ?? {}),
      scanopsIntegration: Boolean(key),
    };
    window.history[replace ? "replaceState" : "pushState"](nextState, "", url);
    if (key) selectionUrl.current = url.href;
  };

  useEffect(() => {
    const url = new URL(window.location.href);
    const legacyFocus = url.searchParams.get("focus");
    if (!legacyFocus) return;
    if (!url.searchParams.has("integration")) {
      url.searchParams.set("integration", legacyFocus);
    }
    url.searchParams.delete("focus");
    window.history.replaceState(window.history.state ?? {}, "", url);
    selectionUrl.current = url.href;
  }, []);

  const openSettings = (connector: Connector) => {
    if (formDirty.current && !window.confirm("Discard unsaved integration changes?")) return;
    formDirty.current = false;
    setHasUnsavedChanges(false);
    returnFocus.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const instance = connector.key === "gitlab" ? connector.instances[0]?.id ?? "new" : null;
    openedFromWorkspace.current = true;
    setSelectedKey(connector.key);
    setSelectedInstanceId(instance);
    setFeedback(null);
    writeSelection(connector.key, instance);
  };

  const chooseInstance = (instance: string) => {
    if (formDirty.current && !window.confirm("Discard unsaved GitLab changes?")) return;
    formDirty.current = false;
    setHasUnsavedChanges(false);
    setSelectedInstanceId(instance);
    setFeedback(null);
    writeSelection("gitlab", instance, true);
  };

  const closeSettings = () => {
    if (formDirty.current && !window.confirm("Discard unsaved integration changes?")) return;
    formDirty.current = false;
    setHasUnsavedChanges(false);
    setFeedback(null);
    if (openedFromWorkspace.current) {
      openedFromWorkspace.current = false;
      window.history.back();
      return;
    }
    setSelectedKey(null);
    setSelectedInstanceId(null);
    writeSelection(null, null, true);
  };

  const responseMessage = async (response: Response, fallback: string) => {
    if (!response.ok) {
      const contentType = response.headers.get("content-type") ?? "";
      if (contentType.includes("application/json")) {
        const payload = await response.json() as { detail?: string };
        throw new Error(payload.detail || fallback);
      }
      throw new Error(fallback);
    }
    const finalUrl = new URL(response.url);
    return {
      message: finalUrl.searchParams.get("notice") || fallback,
      tone: (finalUrl.searchParams.get("tone") || "success") as Feedback["tone"],
      focus: finalUrl.searchParams.get("focus"),
      instance: finalUrl.searchParams.get("instance"),
    };
  };

  const submitAction = async (
    id: string,
    path: string,
    fallback: string,
    body?: FormData,
  ) => {
    setOperation(id);
    setFeedback(null);
    try {
      const response = await fetch(path, {
        method: "POST",
        body,
        headers: { Accept: "text/html" },
      });
      const result = await responseMessage(response, fallback);
      setFeedback(result);
      if (result.focus === "gitlab" && result.instance) {
        setSelectedInstanceId(result.instance);
        writeSelection("gitlab", result.instance, true);
      } else if (id.startsWith("delete-gitlab-") && selected?.key === "gitlab") {
        const deletedId = id.replace("delete-gitlab-", "");
        const nextInstance = selected.instances.find((instance) => instance.id !== deletedId)?.id ?? "new";
        setSelectedInstanceId(nextInstance);
        writeSelection("gitlab", nextInstance, true);
      }
      if (
        result.tone !== "error"
        && (id.startsWith("save-") || id.startsWith("create-"))
      ) {
        document
          .querySelectorAll<HTMLInputElement>(
            ".integration-settings-drawer input[type='password']",
          )
          .forEach((input) => {
            input.value = "";
          });
        formDirty.current = false;
        setHasUnsavedChanges(false);
      }
      retry();
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "The integration action failed",
      });
    } finally {
      setOperation(null);
    }
  };

  useEffect(() => {
    if (!selected?.key) return;
    const drawer = drawerRef.current;
    if (!drawer) return;
    const appShell = document.querySelector<HTMLElement>(".app-shell");
    const previousAriaHidden = appShell?.getAttribute("aria-hidden");
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeSettings();
        return;
      }
      if (event.key !== "Tab" || !drawer) return;
      const focusable = [...drawer.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])",
      )].filter((element) => element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    drawer.querySelector<HTMLElement>("button")?.focus({ preventScroll: true });
    appShell?.setAttribute("inert", "");
    appShell?.setAttribute("aria-hidden", "true");
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      appShell?.removeAttribute("inert");
      if (previousAriaHidden === null || previousAriaHidden === undefined) {
        appShell?.removeAttribute("aria-hidden");
      } else {
        appShell?.setAttribute("aria-hidden", previousAriaHidden);
      }
      window.removeEventListener("keydown", onKeyDown);
      returnFocus.current?.focus({ preventScroll: true });
    };
  }, [selected?.key]);

  useEffect(() => {
    const onPopState = (event: PopStateEvent) => {
      const params = new URLSearchParams(window.location.search);
      const legacyFocus = params.get("focus");
      const nextKey = params.get("integration") ?? legacyFocus;
      const nextInstance = params.get("instance");
      if (legacyFocus) {
        const canonical = new URL(window.location.href);
        if (!canonical.searchParams.has("integration")) {
          canonical.searchParams.set("integration", legacyFocus);
        }
        canonical.searchParams.delete("focus");
        window.history.replaceState(event.state ?? {}, "", canonical);
        selectionUrl.current = canonical.href;
      }
      if (
        formDirty.current
        && !window.confirm("Discard unsaved integration changes?")
      ) {
        window.history.pushState(
          {
            ...(window.history.state ?? {}),
            scanopsIntegration: true,
          },
          "",
          selectionUrl.current,
        );
        openedFromWorkspace.current = true;
        return;
      }
      openedFromWorkspace.current = Boolean(
        nextKey && event.state?.scanopsIntegration,
      );
      setSelectedKey(nextKey);
      setSelectedInstanceId(nextInstance);
      setFeedback(null);
      formDirty.current = false;
      setHasUnsavedChanges(false);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [selectedInstanceId, selectedKey]);

  useEffect(() => {
    if (!selected || selected.key !== "gitlab") return;
    const validInstance = selectedInstanceId === "new"
      || selected.instances.some((instance) => instance.id === selectedInstanceId);
    if (validInstance) return;
    const fallbackInstance = selected.instances[0]?.id ?? "new";
    setSelectedInstanceId(fallbackInstance);
    if (selectedInstanceId !== null) {
      writeSelection("gitlab", null, true);
    }
  }, [selected, selectedInstanceId]);

  useEffect(() => {
    if (state !== "ready" || !selectedKey || selected) return;
    setSelectedKey(null);
    setSelectedInstanceId(null);
    writeSelection(null, null, true);
  }, [selected, selectedKey, state]);

  useEffect(() => {
    if (!feedback) return;
    window.requestAnimationFrame(() => feedbackRef.current?.focus({ preventScroll: true }));
  }, [feedback]);

  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasUnsavedChanges]);

  const fieldHint = (field: IntegrationField) => {
    if (field.secret && field.configured) return "Stored securely. Leave blank to keep the current credential.";
    if (field.name === "webhook_url") return "Paste the incoming webhook URL created in Mattermost.";
    if (field.kind === "url" || field.kind === "url_optional") return "Use an HTTPS endpoint reachable from the ScanOps runtime.";
    if (field.name === "verify_ssl") return "Keep certificate verification enabled outside isolated development environments.";
    return null;
  };

  const renderGenericSettings = (connector: Connector) => (
    <form
      className="integration-config-form"
      key={connector.key}
      onChange={() => {
        formDirty.current = true;
        setHasUnsavedChanges(true);
      }}
      onSubmit={(event) => {
        event.preventDefault();
        void submitAction(
          `save-${connector.key}`,
          `/integrations/${connector.key}/save`,
          `${connector.name} configuration saved`,
          new FormData(event.currentTarget),
        );
      }}
    >
      <div className="integration-form-section">
        <div className="integration-form-heading">
          <span>CONNECTION PROFILE</span>
          <strong>Endpoint and credential</strong>
          <small>Secrets are write-only and never returned to this workspace.</small>
        </div>
        <div className="integration-field-grid">
          {connector.fields.map((field) => {
            const hint = fieldHint(field);
            return (
              <label className={`signal-field ${field.kind === "url" || field.kind === "url_optional" ? "wide" : ""}`} key={field.name}>
                <span>{field.label}{field.secret && field.configured && <b>stored</b>}</span>
                {field.kind === "boolean"
                  ? <select name={field.name} defaultValue={field.value} disabled={!canManage || operation !== null}><option value="true">Yes</option><option value="false">No</option></select>
                  : <input
                      name={field.name}
                      type={field.secret ? "password" : field.kind.includes("url") ? "url" : "text"}
                      defaultValue={field.value}
                      placeholder={field.secret ? field.configured ? "Configured — leave blank to keep" : "Enter credential" : ""}
                      autoComplete={field.secret ? "new-password" : "off"}
                      required={field.secret ? !field.configured : field.kind === "url"}
                      disabled={!canManage || operation !== null}
                    />}
                {hint && <small>{hint}</small>}
              </label>
            );
          })}
        </div>
      </div>
      <div className="integration-action-bar">
        <span>{hasUnsavedChanges
          ? "Unsaved changes · save before running a connection test"
          : connector.key === "mattermost"
            ? "Testing sends one visible message to the configured Mattermost channel."
            : connector.source === "database"
              ? "Encrypted database custody"
              : "Environment fallback · saving imports this configuration"}</span>
        <div>
          <button className="integration-button secondary" type="button" onClick={() => void submitAction(`test-${connector.key}`, `/integrations/${connector.key}/test`, "Connection test completed")} disabled={!canManage || !connector.configured || !connector.enabled || hasUnsavedChanges || operation !== null} title={hasUnsavedChanges ? "Save changes before testing this connection" : connector.key === "mattermost" ? "Sends one test message to the configured Mattermost channel" : undefined}>{operation === `test-${connector.key}` ? "Testing…" : "Test connection"}</button>
          <button className="integration-button primary" type="submit" disabled={!canManage || operation !== null}>{operation === `save-${connector.key}` ? "Saving…" : connector.enabled ? "Save configuration" : "Save & enable"}</button>
        </div>
      </div>
      <div className="integration-danger-row">
        <div><strong>Pause this integration</strong><span>Credentials remain encrypted, but ScanOps stops using the connector.</span></div>
        <button type="button" disabled={!canManage || !connector.enabled || operation !== null} onClick={() => {
          if (window.confirm(`Disable ${connector.name}? Stored credentials will be preserved.`)) {
            void submitAction(`disable-${connector.key}`, `/integrations/${connector.key}/disable`, `${connector.name} disabled`);
          }
        }}>{operation === `disable-${connector.key}` ? "Disabling…" : connector.enabled ? "Disable integration" : "Integration disabled"}</button>
      </div>
    </form>
  );

  const renderGitLabEditor = (connector: Connector) => {
    const instance = selectedInstance;
    const isNew = creatingGitLab;
    if (!instance && !isNew) return null;
    const action = isNew
      ? "/integrations/gitlab/instances"
      : `/integrations/gitlab/instances/${instance!.id}/save`;
    const actionId = isNew ? "create-gitlab" : `save-gitlab-${instance!.id}`;
    return (
      <form
        className="integration-config-form gitlab-editor"
        key={isNew ? "new" : instance!.id}
        onChange={() => {
          formDirty.current = true;
          setHasUnsavedChanges(true);
        }}
        onSubmit={(event) => {
          event.preventDefault();
          void submitAction(
            actionId,
            action,
            isNew ? "GitLab connection created" : `${instance!.name} saved`,
            new FormData(event.currentTarget),
          );
        }}
      >
        {connector.source === "environment" && <input type="hidden" name="adopt_environment" value="true" />}
        <div className="integration-form-section">
          <div className="integration-form-heading">
            <span>{isNew ? "NEW CONNECTION" : "CONNECTION PROFILE"}</span>
            <strong>{isNew ? "Add a GitLab instance" : instance!.name}</strong>
            <small>Each credential is managed and tested independently.</small>
          </div>
          <div className="integration-field-grid">
            <label className="signal-field">
              <span>Display name</span>
              <input name="name" type="text" defaultValue={isNew ? "" : instance!.name} placeholder="Production GitLab" autoComplete="off" required disabled={!canManage || operation !== null} />
            </label>
            <label className="signal-field wide">
              <span>API URL</span>
              <input name="api_url" type="url" defaultValue={isNew ? "" : instance!.api_url} placeholder="https://gitlab.example/api/v4" autoComplete="off" required disabled={!canManage || operation !== null} />
              <small>Use the REST endpoint ending in <code>/api/v4</code>.</small>
            </label>
            <label className="signal-field">
              <span>Access token{!isNew && instance!.token_configured && <b>stored</b>}</span>
              <input name="token" type="password" placeholder={!isNew && instance!.token_configured ? "Stored — leave blank to keep" : "Enter access token"} autoComplete="new-password" required={isNew || !instance?.token_configured} disabled={!canManage || operation !== null} />
              <small>Connection test verifies authentication and project discovery. Config Guard CI Lint is project-scoped.</small>
            </label>
            <label className="signal-field">
              <span>Connection</span>
              <select name="enabled" defaultValue={isNew || instance!.enabled ? "true" : "false"} disabled={!canManage || operation !== null}><option value="true">Enabled</option><option value="false">Disabled</option></select>
              <small>Controls whether this credential can be used.</small>
            </label>
            <label className="signal-field">
              <span>Pipeline polling</span>
              <select name="polling_enabled" defaultValue={isNew || instance!.polling_enabled ? "true" : "false"} disabled={!canManage || operation !== null}><option value="true">Enabled</option><option value="false">Disabled</option></select>
              <small>Only one credential per host may poll pipelines.</small>
            </label>
          </div>
        </div>
        <div className="integration-action-bar">
          <span>{hasUnsavedChanges ? "Unsaved changes · save before running a connection test" : isNew ? "The connection can be tested after its first save." : connector.source === "environment" ? "Saving imports this environment credential into encrypted storage." : "Stored values are ready for a connection test."}</span>
          <div>
            {!isNew && <button className="integration-button secondary" type="button" onClick={() => void submitAction(`test-gitlab-${instance!.id}`, `/integrations/gitlab/instances/${instance!.id}/test`, "GitLab connection test completed")} disabled={!canManage || !instance!.token_configured || !instance!.enabled || hasUnsavedChanges || operation !== null} title={hasUnsavedChanges ? "Save changes before testing this connection" : undefined}>{operation === `test-gitlab-${instance!.id}` ? "Testing…" : "Test connection"}</button>}
            <button className="integration-button primary" type="submit" disabled={!canManage || operation !== null}>{operation === actionId ? isNew ? "Creating…" : "Saving…" : isNew ? "Create GitLab" : connector.source === "environment" ? "Import & save" : "Save changes"}</button>
          </div>
        </div>
        {!isNew && <div className="integration-danger-row">
          <div><strong>Remove this credential</strong><span>Audit events remain available after the stored connection is removed.</span></div>
          <button type="button" disabled={!canManage || operation !== null} onClick={() => {
            if (window.confirm(`Remove ${instance!.name}? This cannot be undone.`)) {
              void submitAction(`delete-gitlab-${instance!.id}`, `/integrations/gitlab/instances/${instance!.id}/delete`, `${instance!.name} removed`);
            }
          }}>{operation === `delete-gitlab-${instance!.id}` ? "Removing…" : "Remove connection"}</button>
        </div>}
      </form>
    );
  };

  return (
    <div className="view">
      <header className="page-title integration-page-title">
        <div><p>Connection registry</p><h1>The control plane, connected.</h1><span>Health, direction and credential custody across every external system.</span></div>
        <div className="header-actions integration-header-actions"><span className="quiet-button is-static">{summary.configured} of {summary.connections} connections configured</span><button className="primary-button" onClick={() => connectors[0] && openSettings(connectors[0])} disabled={!connectors.length}>Manage connections</button></div>
      </header>
      {state === "error" && connectors.length > 0 && <div className="integration-stale-note" role="alert"><span><i /> Last-known connection registry</span><p>Refresh failed. Status and credential metadata may be stale.</p><button type="button" onClick={retry}>Retry now</button></div>}
      {!connectors.length && <EmptyState state={state} title="No integration contracts available" copy="The connector registry could not be read." onRetry={retry} />}
      {!!connectors.length && <>
      <section className="integration-canvas">
        <div className="integration-radar">
          <div className="radar-stage">
            <div className="radar-orbit radar-orbit-a" /><div className="radar-orbit radar-orbit-b" /><div className="radar-sweep" />
            <div className="core-node"><Aperture /><strong>ScanOps</strong><span>control plane</span><i /></div>
            {connectors.map((connector, index) => <button className={`radar-connector radar-connector-${index + 1} ${connectorState(connector)}`} type="button" onClick={() => openSettings(connector)} aria-label={`Configure ${connector.name}`} key={connector.key}><i /><span>{connector.name}</span></button>)}
          </div>
        </div>
        <div className="connector-list">
          <div className="connector-head"><span>{summary.integration_types} integration types · {summary.connections} connections</span><b className={summary.failed ? "has-failures" : ""}><i /> {summary.healthy} healthy · {summary.failed} failed</b></div>
          {connectors.map((connector, index) => (
            <button className={`connector-row ${selectedKey === connector.key ? "selected" : ""}`} type="button" onClick={() => openSettings(connector)} aria-expanded={selectedKey === connector.key} key={connector.name} style={{ "--row": index } as React.CSSProperties}>
              <span className="connector-port"><i /></span>
              <span className="connector-name"><strong>{connector.name}</strong><small>{connector.role}</small></span>
              <span className="connector-endpoint"><small>CREDENTIAL SOURCE</small><code>{connector.source}</code></span>
              <span className="connector-direction">{connector.direction === "both" ? "↔" : connector.direction === "inbound" ? "←" : "→"}<small>{connector.direction}</small></span>
              <Status value={connectorState(connector)} />
              <span className="connector-lag">{relativeTime(connector.tested_at ?? connector.updated_at)}<small>{connector.tested_at ? "tested" : connector.updated_at ? "updated" : "no test"}</small></span>
              <span className="connector-chevron">›</span>
            </button>
          ))}
        </div>
      </section>
      </>}
      {selected && createPortal(<div className="integration-settings-backdrop" onMouseDown={closeSettings}>
        <section className={`integration-settings-drawer ${selected.key === "gitlab" ? "is-gitlab" : ""}`} role="dialog" aria-modal="true" aria-labelledby="integration-settings-title" onMouseDown={(event) => event.stopPropagation()} ref={drawerRef}>
          <header className="integration-settings-header">
            <div className="integration-settings-mark"><span>{selected.direction === "both" ? "↔" : selected.direction === "inbound" ? "←" : "→"}</span><i /></div>
            <div>
              <Status value={connectorState(selected)} />
              <h2 id="integration-settings-title">{selected.name}</h2>
              <p>{selected.detail}</p>
            </div>
            <button className="integration-close" type="button" onClick={closeSettings} aria-label={`Close ${selected.name} settings`}><span>Close</span> ×</button>
          </header>
          <div className="integration-settings-scroll">
            <section className="integration-context-strip" aria-label="Integration context">
              <div><span>Direction</span><strong>{selected.canonical_direction}</strong></div>
              <div><span>Credential custody</span><strong>{selected.source}</strong></div>
              <div><span>Last test</span><strong>{relativeTime(selected.tested_at)}</strong></div>
              <div><span>Last success</span><strong>{relativeTime(selected.last_successful_check?.at)}</strong></div>
            </section>
            {!canManage && <div className="integration-management-lock" role="status"><span>⌁</span><div><strong>Configuration is read-only</strong><p>{!data?.management.auth_ready ? "Enable UI authentication to allow credential changes." : "Set INTEGRATION_CREDENTIALS_KEY to enable encrypted credential storage."}</p></div></div>}
            {selected.error && <div className="integration-feedback error" role="alert"><strong>Credential state needs attention</strong><span>{selected.error}</span></div>}
            {feedback && <div className={`integration-feedback ${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"} tabIndex={-1} ref={feedbackRef}><strong>{feedback.tone === "error" ? "Action failed" : feedback.tone === "warning" ? "Configuration updated" : "Action complete"}</strong><span>{feedback.message}</span></div>}
            {selected.key === "gitlab"
              ? <div className="gitlab-settings-workspace">
                  <aside className="gitlab-instance-rail" aria-label="GitLab connections">
                    <div><span>GITLAB CONNECTIONS</span><b>{selected.instances.length}</b></div>
                    {selected.instances.map((instance) => (
                      <button type="button" className={selectedInstanceId === instance.id ? "selected" : ""} aria-pressed={selectedInstanceId === instance.id} aria-controls="gitlab-editor-stage" onClick={() => chooseInstance(instance.id)} key={instance.id}>
                        <span className={`instance-beacon ${instanceState(instance)}`}><i /></span>
                        <span><strong>{instance.name}</strong><small>{instance.host}</small></span>
                        <Status value={instanceState(instance)} />
                      </button>
                    ))}
                    <button className={`gitlab-add ${creatingGitLab ? "selected" : ""}`} type="button" aria-pressed={creatingGitLab} aria-controls="gitlab-editor-stage" onClick={() => chooseInstance("new")} disabled={!canManage}><span>＋</span><span><strong>Add connection</strong><small>Independent credential</small></span></button>
                  </aside>
                  <div className="gitlab-editor-stage" id="gitlab-editor-stage">
                    {!creatingGitLab && selectedInstance && <section className="integration-instance-signal">
                      <div><span>CONNECTION SIGNAL</span><strong>{selectedInstance.test_message || selectedInstance.health_label}</strong></div>
                      <dl>
                        <div><dt>Credential</dt><dd title={selectedInstance.credential_ref}>{selectedInstance.credential_ref}</dd></div>
                        <div><dt>Latest check</dt>{renderEvidence(selectedInstance.tested_at, selectedInstance.test_message)}</div>
                        <div><dt>Last success</dt>{renderEvidence(
                          selectedInstance.last_successful_check?.at ?? selectedInstance.last_success_at,
                          selectedInstance.last_successful_check?.message ?? selectedInstance.last_success_message,
                        )}</div>
                      </dl>
                    </section>}
                    {renderGitLabEditor(selected)}
                  </div>
                </div>
              : <>
                  <section className="integration-instance-signal">
                    <div><span>CONNECTION SIGNAL</span><strong>{selected.test_message || selected.health_reason || (selected.configured ? "Configured · connection test pending" : "Credential setup required")}</strong></div>
                    <dl>
                      <div><dt>Credential</dt><dd title={selected.credential_ref}>{selected.credential_ref}</dd></div>
                      <div><dt>Latest check</dt>{renderEvidence(selected.tested_at, selected.test_message)}</div>
                      <div><dt>Last success</dt>{renderEvidence(selected.last_successful_check?.at, selected.last_successful_check?.message)}</div>
                      <div><dt>Updated</dt><dd>{relativeTime(selected.updated_at)}{selected.updated_by ? ` · ${selected.updated_by}` : ""}</dd></div>
                    </dl>
                  </section>
                  {renderGenericSettings(selected)}
                </>}
          </div>
        </section>
      </div>, document.body)}
    </div>
  );
}

export default function Home() {
  const readView = () => {
    const pathname = window.location.pathname.replace(/\/+$/, "") || "/";
    const params = new URLSearchParams(window.location.search);
    if (pathname === "/") return "overview";
    if (pathname === "/dashboard/duration") return "duration";
    if (pathname === "/executions") return "executions";
    if (pathname === "/logs") {
      return new URLSearchParams(window.location.search).get("status") === "failed"
        ? "failed"
        : "jobs";
    }
    if (pathname === "/pipelines" || /^\/pipelines\/\d+$/.test(pathname)) return "pipelines";
    if (pathname === "/config-scan") return "config";
    if (pathname === "/iac") return "iac";
    if (pathname === "/ptai-scans") return "ptai";
    if (pathname === "/coverage") return "coverage";
    if (pathname === "/targets") return params.get("level") === "repository" ? "repositories" : "products";
    if (pathname.startsWith("/targets/products/")) return "products";
    if (pathname === "/scanners") return "scanners";
    if (pathname === "/integrations") return "integrations";
    const candidate = params.get("view") as View | null;
    return views.some((item) => item.id === candidate) ? candidate as View : "overview";
  };
  const [view, setView] = useState<View>(readView);
  const [pathname, setPathname] = useState(() => window.location.pathname);
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandQuery, setCommandQuery] = useState("");
  const [commandIndex, setCommandIndex] = useState(0);
  const [liveDataIssue, setLiveDataIssue] = useState<LiveDataStatus | null>(null);
  const commandButtonRef = useRef<HTMLButtonElement>(null);
  const commandDialogRef = useRef<HTMLElement>(null);
  const commandInputRef = useRef<HTMLInputElement>(null);
  const health = useLiveData<HealthData>("/api/health", 15_000);
  const current = useMemo(() => views.find((item) => item.id === view) ?? views[0], [view]);
  const commandViews = useMemo(
    () => views.filter((item) => !item.hidden).filter(
      (item) => item.label.toLowerCase().includes(
        commandQuery.trim().toLowerCase(),
      ),
    ),
    [commandQuery],
  );
  const reportLiveDataStatus = useCallback((status: LiveDataStatus) => {
    setLiveDataIssue((currentIssue) => {
      if (status.status === "error") return status;
      return currentIssue?.path === status.path ? null : currentIssue;
    });
  }, []);
  const operationalStatus = health.state === "error"
    ? "unavailable"
    : health.data?.operational_status ?? "checking";
  const healthTone = operationalStatus === "checking"
    ? "partial"
    : operationalStatus === "unavailable"
      ? "failed"
      : statusTone(operationalStatus);
  const healthIsLive = healthTone === "succeeded";
  const rawHealthReason = health.data?.degraded_reasons?.[0] ?? null;
  const healthReason = rawHealthReason?.toLowerCase().startsWith("worker heartbeat stale:")
    ? `${rawHealthReason.split(":").slice(1).join(":").trim().replaceAll("_", " ")} heartbeat stale`
    : rawHealthReason;
  const operationalLabel = healthIsLive ? "operational" : operationalStatus.replaceAll("_", " ");
  const environmentLabel = healthIsLive ? "LIVE" : operationalStatus.replaceAll("_", " ").toUpperCase();
  const healthTitle = healthReason ?? `Operational status: ${operationalLabel}`;

  const navigate = (next: View, replace = false) => {
    const canonical = viewRoutes[next];
    const url = new URL(canonical, window.location.origin);
    window.history[replace ? "replaceState" : "pushState"]({}, "", url);
    setPathname(url.pathname);
    setView(next);
    window.dispatchEvent(new PopStateEvent("popstate"));
    setCommandOpen(false);
    setCommandQuery("");
  };

  const handleNavigationClick = (
    event: React.MouseEvent<HTMLAnchorElement>,
    next: View,
  ) => {
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) return;
    event.preventDefault();
    navigate(next);
  };

  useEffect(() => {
    setCommandIndex(0);
  }, [commandQuery]);

  useEffect(() => {
    if (!commandOpen) return;
    const appShell = document.querySelector<HTMLElement>(".app-shell");
    const previousAriaHidden = appShell?.getAttribute("aria-hidden");
    const previousInert = appShell?.inert ?? false;
    const previousBodyOverflow = document.body.style.overflow;
    appShell?.setAttribute("aria-hidden", "true");
    if (appShell) appShell.inert = true;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => commandInputRef.current?.focus());

    return () => {
      if (appShell) appShell.inert = previousInert;
      document.body.style.overflow = previousBodyOverflow;
      if (previousAriaHidden === null) appShell?.removeAttribute("aria-hidden");
      else if (previousAriaHidden !== undefined) {
        appShell?.setAttribute("aria-hidden", previousAriaHidden);
      }
      window.requestAnimationFrame(() => commandButtonRef.current?.focus());
    };
  }, [commandOpen]);

  useEffect(() => {
    if (!window.matchMedia("(max-width: 780px)").matches) return;
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLAnchorElement>(".sidebar nav a.active")
        ?.scrollIntoView({ block: "nearest", inline: "center" });
    });
  }, [view]);

  useEffect(() => {
    const onPopState = () => {
      setPathname(window.location.pathname);
      setView(readView());
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((value) => !value);
      }
      if (event.key === "Escape") setCommandOpen(false);
    };
    window.addEventListener("popstate", onPopState);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  const closeCommand = () => setCommandOpen(false);
  const handleCommandKeyDown = (
    event: React.KeyboardEvent<HTMLElement>,
  ) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeCommand();
      return;
    }
    if (event.key === "ArrowDown" && commandViews.length) {
      event.preventDefault();
      setCommandIndex((value) => (value + 1) % commandViews.length);
      commandInputRef.current?.focus();
      return;
    }
    if (event.key === "ArrowUp" && commandViews.length) {
      event.preventDefault();
      setCommandIndex(
        (value) => (value - 1 + commandViews.length) % commandViews.length,
      );
      commandInputRef.current?.focus();
      return;
    }
    if (
      event.key === "Enter"
      && event.target === commandInputRef.current
      && commandViews[commandIndex]
    ) {
      event.preventDefault();
      navigate(commandViews[commandIndex].id);
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      commandDialogRef.current?.querySelectorAll<HTMLElement>(
        'input, button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <LiveDataStatusContext.Provider value={reportLiveDataStatus}>
    <HealthContext.Provider value={health}>
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><Aperture /><span><strong>ScanOps</strong><small>signal control</small></span></div>
        <nav aria-label="Primary">
          {(["OPERATE", "UNDERSTAND", "PLATFORM"] as const).map((section) => {
            const sectionItems = views.filter((item) => item.section === section && !item.hidden);
            return (
              <div className="nav-group" key={section}>
                <span className="nav-section">{section}</span>
                {sectionItems.filter((item) => !item.parent).map((item) => {
                  const children = sectionItems.filter((candidate) => candidate.parent === item.id);
                  const familyActive = children.some((child) => child.id === view);
                  return (
                    <div className={`nav-family${children.length ? " has-children" : ""}${familyActive ? " is-active-family" : ""}`} key={item.id}>
                      <a href={viewRoutes[item.id]} aria-current={view === item.id ? "page" : undefined} aria-label={item.label} className={`${view === item.id ? "active" : ""}${children.length ? " nav-parent" : ""}`} onClick={(event) => handleNavigationClick(event, item.id)}>
                        <span>{item.label}</span>
                      </a>
                      {children.length > 0 && (
                        <div className="nav-children" role="group" aria-label={`${item.label} views`}>
                          {children.map((child) => (
                            <a href={viewRoutes[child.id]} key={child.id} aria-current={view === child.id ? "page" : undefined} aria-label={child.label} className={`nav-sub${view === child.id ? " active" : ""}`} onClick={(event) => handleNavigationClick(event, child.id)}>
                              <span>{child.label}</span>
                            </a>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </nav>
        <div className={`runtime runtime-${healthTone}`}>
          <div title={healthTitle}><span className="health-dot" /><strong>ScanOps {operationalLabel}</strong></div>
          <span><small>Runtime</small><b className="health-detail" title={healthTitle}>{healthReason ?? (healthIsLive ? "Live ScanOps models" : environmentLabel.toLowerCase())}</b></span>
          <span><small>Workspace</small><b>Signal Control</b></span>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="breadcrumbs"><span>ScanOps</span><b>/</b><span>{current.section === "PLATFORM" ? "Infrastructure" : current.section === "UNDERSTAND" ? "Security posture" : "Operations"}</span><b>/</b><strong>{current.label}</strong></div>
          <div className="top-actions"><button className="command-button" aria-label="Quick action" aria-haspopup="dialog" aria-expanded={commandOpen} onClick={() => setCommandOpen(true)} ref={commandButtonRef}>⌘ <span>Quick action</span><kbd>K</kbd></button><span className={`environment environment-${healthTone}`} title={healthTitle}><i /> <span>INTERNAL · </span>{environmentLabel}</span></div>
        </header>
        {liveDataIssue && (
          <div className="live-refresh-alert" role="alert">
            <span><i /><strong>Live refresh paused</strong></span>
            <p>
              {liveDataIssue.lastSuccessfulAt
                ? `Showing data last confirmed ${relativeTime(new Date(liveDataIssue.lastSuccessfulAt).toISOString())}.`
                : "No successful response has been received for this view."}
            </p>
            <button type="button" onClick={liveDataIssue.retry}>Retry now</button>
          </div>
        )}
        <div className="view-frame" key={`${view}:${pathname}`}>
          {view === "overview" && <Overview />}
          {view === "duration" && <ExecutionDuration />}
          {view === "executions" && <Executions />}
          {view === "jobs" && <JobJournal />}
          {view === "pipelines" && (pathname.startsWith("/pipelines/") ? <PipelineDetail /> : <ActivePipelines />)}
          {view === "config" && <ConfigGuard />}
          {view === "iac" && <IaCScanning />}
          {view === "ptai" && <PTAI />}
          {view === "failed" && <JobJournal />}
          {view === "coverage" && <Coverage />}
          {view === "products" && (pathname.startsWith("/targets/products/") ? <ProductDetail /> : <ProductsView />)}
          {view === "repositories" && <Repositories />}
          {view === "scanners" && <ScannerFleet />}
          {view === "integrations" && <Integrations />}
        </div>
      </section>
    </main>
      {commandOpen && createPortal(
        <div className="command-backdrop" onMouseDown={closeCommand}>
          <section
            className="command-palette"
            role="dialog"
            aria-modal="true"
            aria-label="Quick action"
            onKeyDown={handleCommandKeyDown}
            onMouseDown={(event) => event.stopPropagation()}
            ref={commandDialogRef}
          >
            <div className="command-input">
              <span aria-hidden="true">⌕</span>
              <input
                ref={commandInputRef}
                role="combobox"
                aria-label="Filter quick actions"
                aria-controls="quick-action-results"
                aria-expanded="true"
                aria-activedescendant={
                  commandViews[commandIndex]
                    ? `quick-action-${commandViews[commandIndex].id}`
                    : undefined
                }
                autoComplete="off"
                value={commandQuery}
                onChange={(event) => setCommandQuery(event.target.value)}
                placeholder="Go to a ScanOps section…"
              />
              <kbd>esc</kbd>
            </div>
            <div
              className="command-results"
              id="quick-action-results"
              role="listbox"
              aria-label="ScanOps sections"
            >
              {commandViews.map((item, index) => (
                <button
                  id={`quick-action-${item.id}`}
                  role="option"
                  aria-selected={commandIndex === index}
                  className={commandIndex === index ? "selected" : ""}
                  tabIndex={commandIndex === index ? 0 : -1}
                  onMouseEnter={() => setCommandIndex(index)}
                  onClick={() => navigate(item.id)}
                  key={item.id}
                >
                  <span className="nav-glyph">{item.glyph}</span>
                  <span>
                    <strong>{item.label}</strong>
                    <small>{item.section === "PLATFORM" ? "Infrastructure" : item.section === "UNDERSTAND" ? "Security posture" : "Operations"}</small>
                  </span>
                  <b>↵</b>
                </button>
              ))}
              {!commandViews.length && <span className="command-empty">No matching section</span>}
            </div>
          </section>
        </div>,
        document.body,
      )}
    </HealthContext.Provider>
    </LiveDataStatusContext.Provider>
  );
}
