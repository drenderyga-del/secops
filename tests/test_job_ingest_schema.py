from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import JobIngestRequest


_MISSING = object()


def _request(pipeline_url: object = _MISSING) -> JobIngestRequest:
    gitlab = {
        "url": "https://gitlab.example.com/platform/service",
        "project": {"path": "platform/service"},
    }
    if pipeline_url is not _MISSING:
        gitlab["pipeline_url"] = pipeline_url
    return JobIngestRequest(
        gitlab=gitlab,
        job={"name": "sast", "status": "success"},
    )


@pytest.mark.parametrize(
    ("pipeline_url", "expected"),
    [
        (
            "  HTTPS://GITLAB.EXAMPLE.COM:443/platform/service/-/pipelines/42  ",
            "https://gitlab.example.com/platform/service/-/pipelines/42",
        ),
        (
            "http://gitlab.internal/platform/service/-/pipelines/42",
            "http://gitlab.internal/platform/service/-/pipelines/42",
        ),
    ],
)
def test_pipeline_url_is_normalized_as_an_absolute_http_url(pipeline_url, expected):
    request = _request(pipeline_url)

    assert request.gitlab["pipeline_url"] == expected


@pytest.mark.parametrize("pipeline_url", [_MISSING, None, "", "   "])
def test_pipeline_url_remains_optional(pipeline_url):
    request = _request(pipeline_url)

    if pipeline_url is _MISSING:
        assert "pipeline_url" not in request.gitlab
    else:
        assert request.gitlab["pipeline_url"] is None


@pytest.mark.parametrize(
    "pipeline_url",
    [
        "javascript:alert(document.domain)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://gitlab.example.com/platform/service/-/pipelines/42",
        "//gitlab.example.com/platform/service/-/pipelines/42",
        "https:///platform/service/-/pipelines/42",
        "http:gitlab.example.com/platform/service/-/pipelines/42",
        "https://gitlab.example.com/platform/ser vice/-/pipelines/42",
        "https://gitlab.example.com\\@attacker.example/pipeline",
    ],
)
def test_pipeline_url_rejects_non_http_or_malformed_values(pipeline_url):
    with pytest.raises(ValidationError, match="absolute HTTP or HTTPS URL"):
        _request(pipeline_url)


def test_pipeline_url_rejects_embedded_credentials():
    with pytest.raises(ValidationError, match="must not contain credentials"):
        _request("https://ci-user:secret@gitlab.example.com/platform/-/pipelines/42")


def test_pipeline_url_rejects_non_string_values():
    with pytest.raises(ValidationError, match="must be a string or null"):
        _request(42)


def test_pipeline_url_rejects_values_that_cannot_fit_persisted_contract():
    oversized_url = "https://gitlab.example.com/" + ("a" * 230)

    with pytest.raises(ValidationError, match="at most 255 characters"):
        _request(oversized_url)
