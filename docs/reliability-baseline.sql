-- Reliability baseline для пилота ScanOps (задача «Снять reliability baseline»).
-- Запуск: docker compose exec postgres psql -U postgres -d security_dashboard -f /path/to/reliability-baseline.sql
-- Окно ретроспективы по умолчанию 28 дней; freshness-окна 7/30 дней.

-- ===== KPI 1–2. Coverage по последнему audit-снапшоту =====
WITH latest AS (
    SELECT DISTINCT ON (s.audit_repository_id)
           s.audit_repository_id, s.status, s.scanned_at, s.last_pipeline_at
    FROM audit_snapshots s
    ORDER BY s.audit_repository_id, s.scanned_at DESC
)
SELECT l.status,
       COUNT(*) AS repos,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM latest l
JOIN audit_repositories r ON r.id = l.audit_repository_id
WHERE NOT r.archived AND NOT r.excluded AND NOT r.empty_repo
GROUP BY l.status
ORDER BY repos DESC;

-- ===== KPI 1. Stale coverage: covered-репы без свежего security-pipeline =====
WITH latest AS (
    SELECT DISTINCT ON (s.audit_repository_id)
           s.audit_repository_id, s.status, s.last_pipeline_at
    FROM audit_snapshots s
    ORDER BY s.audit_repository_id, s.scanned_at DESC
)
SELECT COUNT(*)                                                                    AS covered_total,
       COUNT(*) FILTER (WHERE l.last_pipeline_at < now() - interval '7 days')     AS stale_7d,
       COUNT(*) FILTER (WHERE l.last_pipeline_at < now() - interval '30 days')    AS stale_30d,
       ROUND(100.0 * COUNT(*) FILTER (WHERE l.last_pipeline_at < now() - interval '7 days') / NULLIF(COUNT(*), 0), 1)  AS stale_7d_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE l.last_pipeline_at < now() - interval '30 days') / NULLIF(COUNT(*), 0), 1) AS stale_30d_pct
FROM latest l
JOIN audit_repositories r ON r.id = l.audit_repository_id
WHERE l.status = 'covered'
  AND NOT r.archived AND NOT r.excluded AND NOT r.empty_repo;

-- ===== KPI 5. PT AI: исходы сканов за окно =====
SELECT asoc_status, COUNT(*) AS scans,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM ptai_scans
WHERE created_at >= now() - interval '28 days'
GROUP BY asoc_status
ORDER BY scans DESC;

-- ===== KPI 6. PT AI stuck: не-терминальные и не поллившиеся =====
SELECT COUNT(*) FILTER (WHERE created_at < now() - interval '24 hours')      AS stuck_over_24h,
       COUNT(*) FILTER (WHERE last_polled_at < now() - interval '6 hours')   AS not_polled_6h,
       COUNT(*)                                                              AS non_terminal_total
FROM ptai_scans
WHERE NOT trigger_failed
  AND asoc_status NOT IN ('ERROR', 'SUCCESS', 'PARTIAL_SUCCESS');

-- ===== KPI 5. IaC: delivery rate (скан успешен -> SARIF доехал в ASOC) =====
SELECT COUNT(*) FILTER (WHERE rep.scan_status = 'succeeded')                                              AS scanned_ok,
       COUNT(*) FILTER (WHERE rep.asoc_upload_status = 'uploaded')                                        AS uploaded,
       COUNT(*) FILTER (WHERE rep.scan_status = 'succeeded' AND rep.asoc_upload_status <> 'uploaded'
                          AND rep.asoc_upload_status <> 'skipped')                                        AS scanned_not_delivered,
       ROUND(100.0 * COUNT(*) FILTER (WHERE rep.asoc_upload_status = 'uploaded')
             / NULLIF(COUNT(*) FILTER (WHERE rep.scan_status = 'succeeded'), 0), 1)                       AS delivery_pct
FROM iac_scan_repositories rep
JOIN iac_scan_runs run ON run.id = rep.run_id
WHERE run.created_at >= now() - interval '28 days';

-- ===== Latency. IaC: queue time и длительность рана =====
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (started_at - created_at)))  AS p50_queue_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (started_at - created_at))) AS p95_queue_s,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at)))  AS p50_run_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))) AS p95_run_s
FROM iac_scan_runs
WHERE created_at >= now() - interval '28 days'
  AND started_at IS NOT NULL;

-- ===== Проксия silent failure window: PT AI от завершения с ошибкой до алерта =====
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (last_alert_at - finished_at)) / 3600)  AS p50_detect_h,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (last_alert_at - finished_at)) / 3600) AS p95_detect_h,
       COUNT(*)                                                                                               AS failed_with_alert
FROM ptai_scans
WHERE asoc_status IN ('ERROR', 'PARTIAL_SUCCESS')
  AND finished_at IS NOT NULL AND last_alert_at IS NOT NULL
  AND created_at >= now() - interval '28 days';

-- ===== Справочно: worker fleet =====
SELECT * FROM worker_heartbeats ORDER BY 1;
