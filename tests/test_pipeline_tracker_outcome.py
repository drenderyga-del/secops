import asyncio
from types import SimpleNamespace

from app.pipeline_tracker import PipelinePoller


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.endpoint = None

    async def get(self, endpoint):
        self.endpoint = endpoint
        return _Response(self.payload)


class _Service:
    def __init__(self):
        self.recorded = []
        self.poll_state = None

    def record_pipeline_job(self, _pipeline, job_payload):
        self.recorded.append(job_payload)

    def update_pipeline_poll_state(
        self,
        _pipeline,
        *,
        completed,
        error,
        observed_job_statuses=None,
    ):
        self.poll_state = {
            "completed": completed,
            "error": error,
            "observed_job_statuses": list(observed_job_statuses or ()),
        }


def test_pipeline_poller_uses_only_current_non_retried_job_observation(monkeypatch):
    poller = PipelinePoller()
    monkeypatch.setattr(poller, "_capture_ptai_scans", _noop_capture)
    client = _Client(
        [
            {
                "id": 502,
                "name": "sast",
                "status": "success",
                "finished_at": "2026-07-30T16:00:00Z",
            }
        ]
    )
    service = _Service()
    pipeline = SimpleNamespace(
        id=1,
        project_id=42,
        pipeline_id=100,
        tracked_jobs=["sast"],
    )

    asyncio.run(poller._poll_pipeline(client, service, pipeline))

    assert client.endpoint.endswith(
        "/projects/42/pipelines/100/jobs?per_page=100&include_retried=false"
    )
    assert [job["id"] for job in service.recorded] == [502]
    assert service.poll_state == {
        "completed": True,
        "error": None,
        "observed_job_statuses": ["success"],
    }


async def _noop_capture(_client, _service, _pipeline, _jobs):
    return None
