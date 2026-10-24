from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import DatabaseService
from app.models import Base, JobReport, JobStatus, Product, Repository


def test_product_stats_query_count_does_not_grow_with_inventory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    for index in range(25):
        product = Product(
            name=f"Product {index}",
            path=f"qa/product-{index}",
            gitlab_base_url="https://gitlab.example.test",
        )
        repository = Repository(
            product=product,
            path=f"qa/product-{index}/service",
            last_report_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )
        session.add(
            JobReport(
                repository=repository,
                job_name="sast_scan",
                job_status=JobStatus.SUCCESS,
                ref="main",
                finished_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            )
        )
    session.commit()

    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        rows = DatabaseService(session).get_products_with_stats()
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
        session.close()

    assert len(rows) == 25
    assert all(row["job_stats"]["success_rate"] == 100 for row in rows)
    assert statements <= 6


def test_repository_health_keeps_job_key_denominator_separate_from_raw_runs():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    product = Product(
        name="Health contract",
        path="qa/health-contract",
        gitlab_base_url="https://gitlab.example.test",
    )
    repository = Repository(
        product=product,
        path="qa/health-contract/service",
        last_report_at=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
    )
    session.add_all(
        [
            JobReport(
                repository=repository,
                job_name="sast_scan",
                job_status=JobStatus.SUCCESS,
                ref="main",
                finished_at=datetime(2026, 7, 29, hour, tzinfo=timezone.utc),
            )
            for hour in (9, 10, 11)
        ]
        + [
            JobReport(
                repository=repository,
                job_name="sca_scan",
                job_status=JobStatus.FAILED,
                ref="main",
                finished_at=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
            )
        ]
    )
    session.commit()

    service = DatabaseService(session)
    direct = service.get_repository_stats(repository.id)
    batched = service.get_repository_stats_batch([repository.id])[repository.id]

    for stats in (direct, batched):
        assert stats["total_jobs"] == 4
        assert stats["successful_jobs"] == 3
        assert stats["failed_jobs"] == 1
        assert stats["job_health_healthy"] == 1
        assert stats["job_health_broken"] == 1
        assert stats["job_health_total"] == 2
        assert stats["success_rate"] == 50

    session.close()


@pytest.mark.parametrize("job_status", [JobStatus.CANCELLED, JobStatus.SKIPPED])
def test_repository_health_is_empty_for_nonterminal_outcomes(job_status):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    product = Product(
        name="No terminal outcome",
        path="qa/no-terminal-outcome",
        gitlab_base_url="https://gitlab.example.test",
    )
    repository = Repository(
        product=product,
        path="qa/no-terminal-outcome/service",
        last_report_at=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
    )
    session.add(
        JobReport(
            repository=repository,
            job_name="sast_scan",
            job_status=job_status,
            ref="main",
            finished_at=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )
    )
    session.commit()

    stats = DatabaseService(session).get_repository_stats(repository.id)

    assert stats["job_health_healthy"] == 0
    assert stats["job_health_broken"] == 0
    assert stats["job_health_total"] == 0
    assert stats["success_rate"] == 0
    session.close()
