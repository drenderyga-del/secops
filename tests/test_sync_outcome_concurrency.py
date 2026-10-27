"""An older ASOC attempt must not overwrite a newer committed outcome."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Event
import time
from uuid import uuid4
import pytest
from sqlalchemy import text
from app import asoc_products
from app.models import AppState, SessionLocal, engine


@pytest.mark.needs_db
def test_blocked_older_outcome_rechecks_after_newer_attempt_commits(monkeypatch):
    if engine.dialect.name != 'postgresql':
        pytest.skip('row-lock serialization requires PostgreSQL')
    key = 'outcome-concurrency-' + uuid4().hex
    monkeypatch.setattr(asoc_products, 'ASOC_SYNC_STATE_KEY', key)
    baseline = datetime(2026, 10, 26, tzinfo=timezone.utc)
    with SessionLocal() as db:
        asoc_products.record_sync_outcome(db, trigger='fixture', status='ok', message='initial', at=baseline)
    ready = Event(); backend = {}
    def older():
        with SessionLocal() as db:
            backend['pid'] = db.execute(text('SELECT pg_backend_pid()')).scalar_one()
            ready.set()
            asoc_products.record_sync_outcome(db, trigger='scheduler', status='failed',
                                             message='older', at=baseline + timedelta(minutes=1))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with SessionLocal() as writer:
                row = writer.query(AppState).filter_by(key=key).with_for_update().one()
                row.value = json.dumps({'message': 'newer', 'at': (baseline + timedelta(minutes=2)).isoformat()})
                writer.flush()
                future = pool.submit(older)
                try:
                    assert ready.wait(5)
                    # Both the buggy UPDATE and corrected SELECT FOR UPDATE must
                    # reach a server-side lock wait before the writer commits.
                    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as observer:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            waiting = observer.execute(text(
                                'SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid'
                            ), {'pid': backend['pid']}).scalar_one()
                            if waiting == 'Lock':
                                break
                            time.sleep(0.02)
                        else:
                            pytest.fail('older outcome did not reach its database lock wait')
                    assert not future.done()
                finally:
                    writer.commit()
            future.result(timeout=10)
        with SessionLocal() as db:
            assert asoc_products.read_sync_outcome(db)['message'] == 'newer'
    finally:
        with SessionLocal() as db:
            db.query(AppState).filter_by(key=key).delete(); db.commit()
