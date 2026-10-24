-- A terminal pipeline is successful only when none of its retained tracked
-- jobs failed or was cancelled. Older poller versions stored every terminal
-- set as COMPLETED. Retained pipeline_jobs can contain attempts superseded by
-- a successful retry, so only the newest GitLab job id for each currently
-- tracked job name is authoritative for this backfill.
UPDATE active_pipelines AS pipeline
SET
    status = 'FAILED',
    error_message = COALESCE(
        NULLIF(BTRIM(pipeline.error_message), ''),
        'Tracked job failed or was cancelled'
    ),
    updated_at = NOW()
WHERE pipeline.status = 'COMPLETED'
  AND (
      NULLIF(BTRIM(pipeline.error_message), '') IS NOT NULL
      OR EXISTS (
          SELECT 1
          FROM pipeline_jobs AS job
          WHERE job.active_pipeline_id = pipeline.id
            AND LOWER(BTRIM(job.status)) IN ('failed', 'canceled', 'cancelled')
            AND (
                CASE
                    WHEN pipeline.tracked_jobs IS NULL
                      OR JSONB_TYPEOF(pipeline.tracked_jobs) <> 'array'
                    THEN TRUE
                    WHEN JSONB_ARRAY_LENGTH(pipeline.tracked_jobs) = 0
                    THEN TRUE
                    ELSE EXISTS (
                        SELECT 1
                        FROM JSONB_ARRAY_ELEMENTS_TEXT(pipeline.tracked_jobs) AS tracked(name)
                        WHERE LOWER(BTRIM(tracked.name)) = LOWER(BTRIM(job.name))
                    )
                END
            )
            AND NOT EXISTS (
                SELECT 1
                FROM pipeline_jobs AS newer
                WHERE newer.active_pipeline_id = job.active_pipeline_id
                  AND LOWER(BTRIM(newer.name)) = LOWER(BTRIM(job.name))
                  AND newer.gitlab_job_id > job.gitlab_job_id
            )
      )
  );

-- Keep the scanner-neutral lifecycle projection consistent with the corrected
-- terminal pipeline outcome. The legacy writer could poison Execution with
-- the same retained-attempt bug, including a FAILED row for a successful
-- retry.
UPDATE executions AS execution
SET
    status = CASE
        WHEN pipeline.status = 'FAILED' THEN 'failed'
        ELSE 'succeeded'
    END,
    failure_category = CASE
        WHEN pipeline.status = 'COMPLETED' THEN NULL
        ELSE execution.failure_category
    END,
    failure_reason = CASE
        WHEN pipeline.status = 'COMPLETED' THEN NULL
        ELSE execution.failure_reason
    END,
    finished_at = COALESCE(
        pipeline.completed_at,
        pipeline.finished_at,
        execution.finished_at,
        pipeline.updated_at,
        pipeline.created_at,
        NOW()
    ),
    updated_at = NOW()
FROM active_pipelines AS pipeline
WHERE execution.source = 'gitlab_pipeline'
  AND execution.source_scope = 'default'
  AND execution.correlation_key = (
      'gitlab_pipeline:'
      || CAST(pipeline.project_id AS TEXT)
      || ':'
      || CAST(pipeline.pipeline_id AS TEXT)
  )
  AND pipeline.status IN ('COMPLETED', 'FAILED')
  AND execution.status <> CASE
      WHEN pipeline.status = 'FAILED' THEN 'failed'
      ELSE 'succeeded'
  END;
