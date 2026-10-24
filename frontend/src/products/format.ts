import type { ProductStatus, RepositoryStatus } from "./types";

const dateFormatters = new Map<string, Intl.DateTimeFormat>();

/** Calendar date in the operator timezone used by quarterly reports. */
export function formatDate(value: string | null | undefined, timeZone: string) {
  if (!value) return "—";
  const key = `date:${timeZone}`;
  let formatter = dateFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", timeZone });
    dateFormatters.set(key, formatter);
  }
  return formatter.format(new Date(value));
}

export function formatDateTime(value: string | null | undefined, timeZone: string) {
  if (!value) return "—";
  const key = `datetime:${timeZone}`;
  let formatter = dateFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone,
    });
    dateFormatters.set(key, formatter);
  }
  return formatter.format(new Date(value));
}

export function percent(part: number, total: number) {
  return total > 0 ? Math.round(part / total * 100) : 0;
}

export const statusTones: Record<ProductStatus, "succeeded" | "partial" | "failed" | "muted"> = {
  synced: "succeeded",
  partial: "partial",
  conflict: "failed",
  group_not_monitored: "failed",
  no_gitlab_link: "failed",
  pending_sync: "partial",
  removed: "muted",
};

export const repositoryTones: Record<RepositoryStatus, "succeeded" | "partial" | "failed" | "muted"> = {
  scanned: "succeeded",
  not_scanned: "failed",
  unknown: "partial",
  not_audited: "partial",
  inactive: "muted",
  excluded: "muted",
};

export const scopeLabels: Record<string, string> = {
  internal: "Internal",
  external: "External",
  unclassified: "Audience not set",
  conflict: "Audience conflict",
};

export function pluralize(count: number, singular: string, plural = `${singular}s`) {
  return `${count.toLocaleString()} ${count === 1 ? singular : plural}`;
}
