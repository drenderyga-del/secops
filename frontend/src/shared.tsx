import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

export type LoadState = "loading" | "ready" | "stale" | "error";

export type LiveDataStatus = {
  path: string;
  status: "ready" | "error" | "clear";
  lastSuccessfulAt: number | null;
  retry: () => void;
};

export const LiveDataStatusContext = createContext<(status: LiveDataStatus) => void>(
  () => undefined,
);

export function useLiveData<T>(path: string | null, refreshMs: number | null = 30_000) {
  const [data, setData] = useState<T | null>(null);
  const [dataPath, setDataPath] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [retry, setRetry] = useState(0);
  const dataRef = useRef<T | null>(null);
  const dataPathRef = useRef<string | null>(null);
  const lastSuccessfulAtRef = useRef<number | null>(null);
  const requestSequenceRef = useRef(0);
  const reportStatus = useContext(LiveDataStatusContext);
  const retryNow = useCallback(
    () => setRetry((value) => value + 1),
    [],
  );

  useEffect(() => {
    if (!path) {
      setData(null);
      setDataPath(null);
      setState("loading");
      dataRef.current = null;
      dataPathRef.current = null;
      lastSuccessfulAtRef.current = null;
      requestSequenceRef.current += 1;
      return;
    }
    if (dataPathRef.current !== path) {
      setData(null);
      setState("loading");
      dataRef.current = null;
      dataPathRef.current = null;
      lastSuccessfulAtRef.current = null;
    }
    let active = true;
    const load = async () => {
      const requestSequence = requestSequenceRef.current + 1;
      requestSequenceRef.current = requestSequence;
      try {
        const response = await fetch(path, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`${path}: ${response.status}`);
        const payload = await response.json() as T;
        if (active && requestSequence === requestSequenceRef.current) {
          const loadedAt = Date.now();
          setData(payload);
          setDataPath(path);
          setState("ready");
          dataRef.current = payload;
          dataPathRef.current = path;
          lastSuccessfulAtRef.current = loadedAt;
          reportStatus({
            path,
            status: "ready",
            lastSuccessfulAt: loadedAt,
            retry: retryNow,
          });
        }
      } catch {
        if (active && requestSequence === requestSequenceRef.current) {
          const hasLastKnownData = (
            dataPathRef.current === path && dataRef.current !== null
          );
          setState(hasLastKnownData ? "stale" : "error");
          reportStatus({
            path,
            status: "error",
            lastSuccessfulAt: lastSuccessfulAtRef.current,
            retry: retryNow,
          });
        }
      }
    };
    void load();
    const timer = refreshMs == null ? null : window.setInterval(load, refreshMs);
    window.addEventListener("online", load);
    return () => {
      active = false;
      requestSequenceRef.current += 1;
      if (timer !== null) window.clearInterval(timer);
      window.removeEventListener("online", load);
      reportStatus({
        path,
        status: "clear",
        lastSuccessfulAt: lastSuccessfulAtRef.current,
        retry: retryNow,
      });
    };
  }, [path, refreshMs, retry, reportStatus, retryNow]);

  const currentData = dataPath === path ? data : null;
  const currentState = dataPath === path
    ? state
    : state === "error"
      ? "error"
      : "loading";
  return {
    data: currentData,
    state: currentState,
    lastSuccessfulAt: lastSuccessfulAtRef.current,
    retry: retryNow,
  };
}


export function EmptyState({ state, title, copy, onRetry }: { state: LoadState; title: string; copy: string; onRetry?: () => void }) {
  return (
    <div className={`live-state live-state-${state}`} role={state === "error" ? "alert" : "status"}>
      <Aperture small />
      <strong>{state === "loading" ? "Reading live ScanOps state…" : title}</strong>
      <span>{state === "loading" ? "The view will update without reloading the workspace." : copy}</span>
      {state === "error" && onRetry && <button onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function relativeTime(value?: string | null) {
  if (!value) return "No signal";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${Math.max(seconds, 1)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}


export function statusTone(value: string) {
  const normalized = value.toLowerCase();
  if (["success", "succeeded", "completed", "confirmed", "healthy", "done", "mapped"].includes(normalized)) return "succeeded";
  if (["failed", "error", "critical", "trigger_failed", "unhealthy", "unavailable", "missing_target", "missing_asoc", "no_repositories", "conflict"].includes(normalized)) return "failed";
  if (["running", "polling", "queued", "registered", "pending", "requested"].includes(normalized)) return "running";
  if (["partial", "partial_success", "stale", "degraded", "warning", "unknown", "missing_bi_id", "not_synced", "not_configured", "removed", "canceled", "cancelled"].includes(normalized)) return "partial";
  return normalized;
}

export function Aperture({ small = false }: { small?: boolean }) {
  return (
    <span className={`aperture ${small ? "aperture-small" : ""}`} aria-hidden="true">
      <img src="/static/scanops-mark.svg?v=20260814-scanops-mark-2" alt="" draggable="false" />
    </span>
  );
}

export function Status({ value }: { value: string }) {
  return <span className={`status status-${statusTone(value)}`}><i />{value.replaceAll("_", " ").toLowerCase()}</span>;
}
