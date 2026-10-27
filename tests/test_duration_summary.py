"""Exact all-history summaries keep historical ORM payloads out of application memory."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import DatabaseService
from app.models import ActivePipeline, JobReport, JobStatus, PipelineStatus, Product, Repository, engine


@pytest.fixture(params=['sqlite', pytest.param('postgres', marks=pytest.mark.needs_db)])
def duration_db(request):
    backend = create_engine('sqlite://') if request.param == 'sqlite' else engine
    if request.param == 'sqlite':
        for model in (Product, Repository, ActivePipeline, JobReport):
            model.__table__.create(backend)
    with backend.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection) as session:
            yield session
        if transaction.is_active:
            transaction.rollback()
    if request.param == 'sqlite':
        backend.dispose()


@pytest.mark.parametrize('values,expected', [
    ([], (0, None, None, None, None)),
    ([7], (1, 7, 7, 7, 7)),
    ([2, 8], (2, 5, 5, 8, 8)),
    ([3, 3, 9], (3, 5, 3, 9, 9)),
    (list(range(1, 201)), (200, 100.5, 100.5, 190, 200)),
])
def test_all_history_duration_summaries_are_exact_and_do_not_load_orm_rows(duration_db, values, expected):
    db = duration_db
    product = Product(name='Duration fixture', path='qa/duration-fixture', gitlab_base_url='https://gitlab.example')
    db.add(product); db.flush()
    repository = Repository(product_id=product.id, path='qa/duration-fixture/app')
    other = Repository(product_id=product.id, path='qa/duration-fixture/other')
    db.add_all([repository, other]); db.flush()
    repository_id = repository.id
    now = datetime.now(timezone.utc)
    for index, value in enumerate([*values, -1, None]):
        finished = now + timedelta(seconds=index)
        db.add(JobReport(repository_id=repository_id, job_name='semgrep_scan',
                         job_status=JobStatus.SUCCESS, finished_at=finished, duration_seconds=value))
        db.add(ActivePipeline(project_id=987654321, pipeline_id=index + 1,
                              repository_id=repository_id, product_id=product.id,
                              status=PipelineStatus.COMPLETED, finished_at=finished, duration_seconds=value))
    db.add(JobReport(repository_id=other.id, job_name='semgrep_scan',
                     job_status=JobStatus.SUCCESS, finished_at=now, duration_seconds=999))
    db.add(ActivePipeline(project_id=987654321, pipeline_id=999999,
                          repository_id=other.id, product_id=product.id,
                          status=PipelineStatus.COMPLETED, finished_at=now, duration_seconds=999))
    db.flush(); db.expunge_all()
    loaded = []
    def observed(row, context):
        loaded.append(row.id)
    for model in (JobReport, ActivePipeline):
        event.listen(model, 'load', observed)
    try:
        service = DatabaseService(db)
        results = [service.get_job_duration_summary(repository_id=repository_id, timeframe='all'),
                   service.get_pipeline_duration_summary(repository_id=repository_id, timeframe='all')]
    finally:
        for model in (JobReport, ActivePipeline):
            event.remove(model, 'load', observed)
    assert loaded == []
    for summary in results:
        assert tuple(summary[key] for key in ('count', 'avg', 'median', 'p95', 'latest')) == expected
