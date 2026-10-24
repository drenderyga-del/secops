from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from app.retention import cleanup_once


def make_iac_runs_table(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE iac_scan_runs (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    gitlab_base_url TEXT NOT NULL,
                    scope_path TEXT NOT NULL,
                    single_project_id INTEGER,
                    cancel_requested_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL
                )
                """
            )
        )


def test_retention_keeps_latest_full_scope_run_not_newer_single_repo_rescan(monkeypatch):
    engine = create_engine("sqlite://")
    make_iac_runs_table(engine)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO iac_scan_runs
                (id, status, gitlab_base_url, scope_path, single_project_id, cancel_requested_at, created_at)
                VALUES
                (1, 'succeeded', 'https://gitlab.example', 'group', NULL, NULL, :old),
                (2, 'succeeded', 'https://gitlab.example', 'group', 42, NULL, :old)
                """
            ),
            {"old": old},
        )
    monkeypatch.setenv("SECPIPE_RETENTION_DAYS", "1")

    cleanup_once(engine)

    with engine.connect() as conn:
        ids = [row[0] for row in conn.execute(text("SELECT id FROM iac_scan_runs ORDER BY id"))]
    assert ids == [1]


def test_retention_never_deletes_active_or_cancel_requested_iac_runs(monkeypatch):
    engine = create_engine("sqlite://")
    make_iac_runs_table(engine)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO iac_scan_runs
                (id, status, gitlab_base_url, scope_path, single_project_id, cancel_requested_at, created_at)
                VALUES
                (1, 'running', 'https://gitlab.example', 'running', NULL, NULL, :old),
                (2, 'running', 'https://gitlab.example', 'cancelling', NULL, :old, :old)
                """
            ),
            {"old": old},
        )
    monkeypatch.setenv("SECPIPE_RETENTION_DAYS", "1")

    cleanup_once(engine)

    with engine.connect() as conn:
        ids = [row[0] for row in conn.execute(text("SELECT id FROM iac_scan_runs ORDER BY id"))]
    assert ids == [1, 2]
