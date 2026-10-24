export type RegistryMode = "new" | "total";
export type StatusFilter = "all" | "synced" | "attention" | "removed";
export type ScopeFilter = "all" | "internal" | "external" | "unclassified" | "conflict";

export type ProductStatus =
  | "synced"
  | "partial"
  | "conflict"
  | "group_not_monitored"
  | "no_gitlab_link"
  | "pending_sync"
  | "removed";

export type ProductGroup = {
  id: number;
  name: string;
  path: string;
  url: string | null;
  href: string;
  operator_href: string;
  pipelines_href: string;
};

export type RepositoryCounts = {
  total: number;
  scanned: number;
  not_scanned: number;
  inactive: number;
  unknown: number;
  not_audited: number;
  excluded: number;
  monitored: number;
  audited: boolean;
  audited_at: string | null;
};

export type PipelineCounts = {
  completed: number;
  failed: number;
  success_rate: number | null;
  repos_with_pipelines: number;
  repos_green: number;
  last_success_at: string | null;
  window_days: number;
};

export type ProductRow = {
  key: string;
  asoc_id: string;
  bi_id: string | null;
  name: string;
  asoc_name: string;
  asoc_url: string | null;
  scope: string;
  status: ProductStatus;
  status_group: Exclude<StatusFilter, "all">;
  status_label: string;
  status_hint: string;
  connected_at: string | null;
  connected_quarter: string | null;
  linked_at: string | null;
  linked_before_tracking: boolean;
  waiting_days: number | null;
  overdue: boolean;
  is_active: boolean;
  removed_at: string | null;
  links_total: number;
  links_matched: number;
  groups: ProductGroup[];
  repositories: RepositoryCounts;
  pipelines: PipelineCounts;
  href: string;
};

export type GroupRow = ProductGroup & {
  key: string;
  repositories: RepositoryCounts;
  pipelines: PipelineCounts;
  asoc_conflict?: boolean;
};

export type SyncAttempt = {
  trigger: "scheduler" | "operator";
  status: "ok" | "needs_confirmation" | "failed";
  message: string;
  at: string;
};

export type SyncState = {
  state: "synced" | "stale" | "not_synced" | "not_configured";
  configured: boolean;
  last_synced: string | null;
  total_cards: number;
  removed_cards: number;
  interval_minutes: number;
  last_attempt: SyncAttempt | null;
};

export type RegistryPayload = {
  query: {
    period: string | null;
    period_label: string | null;
    mode: RegistryMode;
    scope: ScopeFilter;
    status: StatusFilter;
    q: string;
  };
  periods: { key: string; label: string }[];
  rows: ProductRow[];
  summary: {
    total: number;
    synced: number;
    attention: number;
    removed: number;
    overdue: number;
    undated: number;
    grace_days: number;
    new_in_period: number | null;
    repositories: Partial<Record<"total" | "scanned" | "not_scanned" | "inactive" | "unknown" | "not_audited", number>>;
    pipelines: { completed: number; failed: number; success_rate: number | null };
  };
  unregistered_groups: GroupRow[];
  sync: SyncState;
  export_href: string;
  timezone: string;
  generated_at: string;
};

export type RepositoryStatus = "scanned" | "not_scanned" | "inactive" | "unknown" | "excluded" | "not_audited";

export type DetailRepository = {
  path: string;
  url: string | null;
  group_id: number;
  group_name: string | null;
  status: RepositoryStatus;
  status_label: string;
  audit_reason: string | null;
  audited_at: string | null;
  last_activity_at: string | null;
  scanops_repository_id: number | null;
  href: string | null;
  jobs_href: string | null;
  open_failed_jobs: number;
  pipelines: {
    completed: number;
    failed: number;
    success_rate: number | null;
    last_status: string | null;
    last_at: string | null;
    last_url: string | null;
  };
};

export type DescriptionLink = {
  url: string | null;
  kind: "gitlab" | "reference";
  state: "matched" | "not_monitored" | "ambiguous" | "reference";
  group: ProductGroup | null;
};

export type ProductDetailPayload = {
  kind: "asoc" | "group";
  focus_group_id: number | null;
  product: ProductRow | GroupRow;
  links: DescriptionLink[];
  claimed_by: { asoc_id: string; name: string; bi_id: string | null; href: string; state: "conflict" | "pending_sync" | "unresolved"; status_label: string }[];
  repositories: DetailRepository[];
  open_failed_jobs: number;
  deliveries: {
    summary: { total: number; confirmed: number; failed: number; pending: number };
    rows: { id: number; execution_id: number; source: string; status: string; failure_reason: string | null; attempted_at: string | null; confirmed_at: string | null; repository_id: number | null }[];
  };
  sync: SyncState;
  timezone: string;
};
