import { useState } from "react";

import { relativeTime } from "../shared";
import { formatDateTime } from "./format";
import type { SyncState } from "./types";

type Confirmation = {
  code: string;
  message: string;
  active_count: number;
  incoming_count: number;
  removal_count: number;
  registry_version: string;
};

type SyncResponse = {
  detail?: string | Confirmation;
  result?: Record<string, number>;
};

function syncSummary(result: Record<string, number> = {}) {
  const synced = (result.mapped ?? 0) + (result.partial ?? 0);
  const attention = (result.conflict ?? 0) + (result.missing_target ?? 0) + (result.no_repositories ?? 0);
  return `${result.total ?? 0} ASOC products: ${synced} synced with ScanOps, ${attention} need attention, ${result.removed ?? 0} removed.`;
}

export function SyncControl({ sync, timezone, onSynced }: { sync: SyncState; timezone: string; onSynced: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);

  const run = async (body: Record<string, unknown>) => {
    setBusy(true);
    setMessage(null);
    try {
      const response = await fetch("/api/ui/asoc-reporting/sync", {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json() as SyncResponse;
      if (response.status === 409 && typeof payload.detail === "object") {
        setConfirmation(payload.detail);
        return;
      }
      if (!response.ok) {
        const detail = payload.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message || "ASOC synchronization failed");
      }
      setConfirmation(null);
      setMessage({ tone: "success", text: syncSummary(payload.result) });
      onSynced();
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof Error ? error.message : "ASOC synchronization failed" });
    } finally {
      setBusy(false);
    }
  };

  const attempt = sync.last_attempt;
  const attemptIsNewer = attempt && (!sync.last_synced || new Date(attempt.at) > new Date(sync.last_synced));
  const blocked = attempt && attemptIsNewer && attempt.status !== "ok";
  const stateLabel = !sync.configured
    ? "ASOC is not connected"
    : sync.last_synced
      ? `Synced ${relativeTime(sync.last_synced)}`
      : "Not synced yet";
  const cadence = sync.interval_minutes
    ? `automatic every ${sync.interval_minutes >= 60 && sync.interval_minutes % 60 === 0 ? `${sync.interval_minutes / 60} h` : `${sync.interval_minutes} min`}`
    : "automatic sync off";

  return (
    <div className="pr-sync">
      <div className={`pr-sync-state state-${blocked ? "blocked" : sync.state}`} title={sync.last_synced ? formatDateTime(sync.last_synced, timezone) : undefined}>
        <i aria-hidden="true" />
        <span><strong>{stateLabel}</strong><small>{sync.configured ? cadence : "Configure ASOC in Integrations"}</small></span>
      </div>
      {sync.configured
        ? <button type="button" className="quiet-button" onClick={() => void run({})} disabled={busy}>{busy ? "Synchronizing…" : "Synchronize ASOC"}</button>
        : <a className="quiet-button" href="/integrations?integration=asoc">Connect ASOC</a>}
      {blocked && !confirmation && (
        <p className="pr-sync-message error" role="status">
          {attempt.trigger === "scheduler" ? "Automatic sync paused: " : ""}{attempt.message}
        </p>
      )}
      {message && <p className={`pr-sync-message ${message.tone}`} role="status">{message.text}</p>}
      {confirmation && (
        <div className="pr-sync-confirm" role="alertdialog" aria-label="Confirm ASOC product removal">
          <p>
            <strong>ASOC returned {confirmation.incoming_count} products while {confirmation.active_count} are active.</strong>
            {" "}{confirmation.removal_count} product(s) will be marked as removed. They stay in quarterly history.
          </p>
          <div>
            <button type="button" className="quiet-button" onClick={() => setConfirmation(null)} disabled={busy}>Cancel</button>
            <button
              type="button"
              className="primary-button"
              disabled={busy}
              onClick={() => void run({
                confirm_mass_removal: true,
                expected_active_count: confirmation.active_count,
                expected_incoming_count: confirmation.incoming_count,
                expected_registry_version: confirmation.registry_version,
              })}
            >
              Remove {confirmation.removal_count} and sync
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
