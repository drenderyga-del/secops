from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import DatabaseService
from app.models import Base, PtaiScan
from app.ptai_outcome import resolve_ptai_outcome
from app.routers.web.ptai import _ptai_summary


def _scan(row_id: int, **values) -> PtaiScan:
    defaults = {
        "gitlab_project_id": row_id,
        "gitlab_pipeline_id": row_id,
        "gitlab_job_id": row_id,
        "asoc_status": "UNKNOWN",
        "trigger_failed": False,
    }
    defaults.update(values)
    return PtaiScan(**defaults)


def test_ptai_status_filters_match_engine_fallback_precedence_and_summary():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    scans = [
        _scan(1),  # unknown ASOC and engine state -> pending fallback
        _scan(2, ptai_stage="Scan"),
        _scan(3, ptai_stage="Done", ptai_has_errors=True),
        _scan(4, asoc_status="SUCCESS", ptai_stage="running"),
        _scan(5, asoc_status="SUCCESS", ptai_stage="running", ptai_has_errors=True),
        _scan(6, asoc_status="PENDING", ptai_stage="Done", ptai_has_errors=True),
        _scan(7, asoc_status="IN_PROGRESS", ptai_stage="Failed"),
        _scan(8, asoc_status="PARTIAL_SUCCESS"),
        _scan(9, asoc_status="ERROR"),
        _scan(10, asoc_status="  queued ", ptai_stage="Done"),
        _scan(11, asoc_status="SUCCESS", ptai_stage="Done", ptai_error=""),
    ]
    with Session(engine) as db:
        db.add_all(scans)
        db.commit()

        expected = {
            scan.id: resolve_ptai_outcome(scan).canonical_status
            for scan in scans
        }
        actual = {}
        for status in ("ERROR", "PARTIAL_SUCCESS", "SUCCESS", "IN_PROGRESS", "PENDING"):
            rows, _ = DatabaseService(db).list_ptai_scans(status=status)
            for row in rows:
                assert row.id not in actual
                actual[row.id] = status

        assert actual == expected
        summary = _ptai_summary(db)
        assert summary["total"] == len(scans)
        assert summary["pending"] == sum(value == "PENDING" for value in expected.values())
        assert summary["running"] == sum(value == "IN_PROGRESS" for value in expected.values())
        assert summary["success"] == sum(value == "SUCCESS" for value in expected.values())
        assert summary["problems"] == sum(
            value in {"ERROR", "PARTIAL_SUCCESS"} for value in expected.values()
        )
