"""PostgreSQL acceptance must serialize with a concurrent context change."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.agent_triage import context_version
from app.models import AgentJobContext, AgentTriageProposal, JobReport, JobStatus, Product, Repository, SessionLocal, engine
from app.routers.agent_api import Review, review


@pytest.mark.needs_db
def test_concurrent_context_change_prevents_acceptance():
    if engine.dialect.name != "postgresql":
        pytest.skip("row-lock serialization requires PostgreSQL")
    marker = uuid4().hex
    with SessionLocal() as db:
        product = Product(name="Concurrency fixture", path=marker, gitlab_base_url="https://gitlab.example")
        db.add(product); db.flush()
        repo = Repository(product_id=product.id, path=f"{marker}/repo")
        db.add(repo); db.flush()
        job = JobReport(repository_id=repo.id, job_name="scanner", job_status=JobStatus.FAILED,
                        finished_at=datetime.now(timezone.utc))
        db.add(job); db.flush()
        context = AgentJobContext(job_report_id=job.id, context_text="Initial context", shared_by="operator")
        context.version = context_version(job, context.context_text)
        db.add(context)
        proposal = AgentTriageProposal(job_report_id=job.id, agent_id="fixture", request_id=marker,
                                       context_version=context.version, payload_json="{}")
        db.add(proposal); db.commit()
        product_id, repo_id, job_id, proposal_id = product.id, repo.id, job.id, proposal.id
    started = Event()
    def accept():
        with SessionLocal() as db:
            started.set()
            try:
                review(proposal_id, Review(decision="accepted"), db=db, actor="operator")
            except HTTPException as exc:
                return exc.status_code
            return 200
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with SessionLocal() as writer:
                locked_job = writer.query(JobReport).filter_by(id=job_id).with_for_update().one()
                context = writer.get(AgentJobContext, job_id)
                context.context_text = "Updated evidence invalidates the old diagnosis"
                context.version = context_version(locked_job, context.context_text)
                writer.flush()
                future = pool.submit(accept)
                assert started.wait(5)
                try:
                    assert not future.done()
                finally:
                    writer.commit()
            assert future.result(timeout=10) == 409
        with SessionLocal() as db:
            assert db.get(AgentTriageProposal, proposal_id).state == "pending"
    finally:
        with SessionLocal() as db:
            db.query(AgentTriageProposal).filter_by(id=proposal_id).delete()
            db.query(AgentJobContext).filter_by(job_report_id=job_id).delete()
            db.query(JobReport).filter_by(id=job_id).delete()
            db.query(Repository).filter_by(id=repo_id).delete()
            db.query(Product).filter_by(id=product_id).delete()
            db.commit()
