import asyncio
import threading
from datetime import timedelta

import pytest

from app.workers import pipeline


def _run(coro):
    return asyncio.run(coro)


def test_blocking_step_keeps_the_heartbeat_then_stops_after_the_limit(monkeypatch):
    beats = []
    release = threading.Event()
    monkeypatch.setattr(pipeline, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(pipeline, "beat", lambda name: beats.append(name))

    async def scenario():
        task = asyncio.create_task(pipeline._run_while_beating(release.wait, limit=timedelta(seconds=0.2)))
        await asyncio.sleep(0.1)
        while_running = len(beats)
        await asyncio.sleep(0.3)
        after_limit = len(beats)
        await asyncio.sleep(0.1)
        release.set()
        await task
        return while_running, after_limit, len(beats)

    while_running, after_limit, final = _run(scenario())

    assert while_running >= 3
    # Past the limit the worker stops claiming to be alive.
    assert final == after_limit


def test_blocking_step_errors_reach_the_caller(monkeypatch):
    monkeypatch.setattr(pipeline, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(pipeline, "beat", lambda _name: None)

    def fail():
        raise RuntimeError("sync failed")

    with pytest.raises(RuntimeError, match="sync failed"):
        _run(pipeline._run_while_beating(fail))
