import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import bindparam, text

from app.models import engine as application_engine


def migration_sql() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "20260730_truthful_pipeline_outcome.sql"
    ).read_text(encoding="utf-8")


@pytest.mark.needs_db
def test_truthful_pipeline_outcome_migration_uses_latest_tracked_attempt():
    suffix = uuid4().hex
    with application_engine.connect() as connection:
        transaction = connection.begin()
        try:
            product_id = connection.execute(
                text(
                    """
                    INSERT INTO products (name, path, gitlab_base_url)
                    VALUES (:name, :path, 'https://gitlab.example')
                    RETURNING id
                    """
                ),
                {"name": f"Migration test {suffix}", "path": f"migration/{suffix}"},
            ).scalar_one()
            repository_id = connection.execute(
                text(
                    """
                    INSERT INTO repositories (product_id, path, overall_status)
                    VALUES (:product_id, :path, 'UNKNOWN')
                    RETURNING id
                    """
                ),
                {
                    "product_id": product_id,
                    "path": f"migration/{suffix}/service",
                },
            ).scalar_one()

            pipeline_ids = []
            pipeline_cases = (
                (["sast"], None),
                (["sast"], None),
                (["sast"], None),
                (["sast"], "Pipeline unavailable"),
                (None, None),
            )
            for offset, (tracked_jobs, error_message) in enumerate(pipeline_cases):
                pipeline_ids.append(
                    connection.execute(
                        text(
                            """
                            INSERT INTO active_pipelines (
                                project_id,
                                pipeline_id,
                                repository_id,
                                product_id,
                                status,
                                tracked_jobs,
                                error_message
                            )
                            VALUES (
                                :project_id,
                                :pipeline_id,
                                :repository_id,
                                :product_id,
                                'COMPLETED',
                                CAST(:tracked_jobs AS JSONB),
                                :error_message
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "project_id": 900000 + offset,
                            "pipeline_id": 910000 + offset,
                            "repository_id": repository_id,
                            "product_id": product_id,
                            "tracked_jobs": json.dumps(tracked_jobs),
                            "error_message": error_message,
                        },
                    ).scalar_one()
                )

            connection.execute(
                text(
                    """
                    INSERT INTO pipeline_jobs (
                        active_pipeline_id,
                        gitlab_job_id,
                        name,
                        status
                    )
                    VALUES
                        (:failed_id, 920001, 'sast', 'success'),
                        (:failed_id, 920002, 'sast', 'failed'),
                        (:retry_id, 920003, 'sast', 'failed'),
                        (:retry_id, 920004, 'sast', 'success'),
                        (:tracked_change_id, 920005, 'legacy_scan', 'failed'),
                        (:tracked_change_id, 920006, 'sast', 'success'),
                        (:default_tracking_id, 920007, 'container_scan', 'failed')
                    """
                ),
                {
                    "failed_id": pipeline_ids[0],
                    "retry_id": pipeline_ids[1],
                    "tracked_change_id": pipeline_ids[2],
                    "default_tracking_id": pipeline_ids[4],
                },
            )
            # Reproduce the legacy writer's poisoned aggregate for a successful
            # retry. The migration must ignore it and use current-attempt facts.
            connection.execute(
                text(
                    """
                    INSERT INTO executions (
                        repository_id,
                        product_id,
                        source,
                        source_scope,
                        correlation_key,
                        status,
                        context_json
                    )
                    VALUES
                        (
                            :repository_id,
                            :product_id,
                            'gitlab_pipeline',
                            'default',
                            'gitlab_pipeline:900000:910000',
                            'succeeded',
                            '{}'
                        ),
                        (
                            :repository_id,
                            :product_id,
                            'gitlab_pipeline',
                            'default',
                            'gitlab_pipeline:900001:910001',
                            'failed',
                            '{}'
                        )
                    """
                ),
                {
                    "repository_id": repository_id,
                    "product_id": product_id,
                },
            )

            connection.execute(text(migration_sql()))
            rows = connection.execute(
                text(
                    """
                    SELECT id, status, error_message
                    FROM active_pipelines
                    WHERE id IN :pipeline_ids
                    ORDER BY id
                    """
                ).bindparams(bindparam("pipeline_ids", expanding=True)),
                {"pipeline_ids": pipeline_ids},
            ).mappings().all()

            assert [row["status"] for row in rows] == [
                "FAILED",
                "COMPLETED",
                "COMPLETED",
                "FAILED",
                "FAILED",
            ]
            assert rows[0]["error_message"] == "Tracked job failed or was cancelled"
            assert rows[1]["error_message"] is None
            assert rows[2]["error_message"] is None
            assert rows[3]["error_message"] == "Pipeline unavailable"
            assert rows[4]["error_message"] == "Tracked job failed or was cancelled"
            execution_rows = connection.execute(
                text(
                    """
                    SELECT correlation_key, status
                    FROM executions
                    WHERE correlation_key IN (
                        'gitlab_pipeline:900000:910000',
                        'gitlab_pipeline:900001:910001'
                    )
                    ORDER BY correlation_key
                    """
                )
            ).mappings().all()
            assert [row["status"] for row in execution_rows] == [
                "failed",
                "succeeded",
            ]
        finally:
            transaction.rollback()
