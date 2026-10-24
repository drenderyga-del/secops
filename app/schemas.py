import re
from datetime import datetime
from typing import Any, Literal, Optional, List

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .models.repository import RepositoryStatus
from .models.job_report import JobStatus

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)
_MAX_PIPELINE_URL_LENGTH = 255


# Request schemas
class GitLabJobData(BaseModel):
    gitlab: dict
    job: dict

class JobIngestRequest(BaseModel):
    gitlab: dict
    job: dict

    @field_validator("gitlab")
    @classmethod
    def normalize_pipeline_url(cls, value: dict) -> dict:
        if "pipeline_url" not in value or value["pipeline_url"] is None:
            return value

        raw_url = value["pipeline_url"]
        if not isinstance(raw_url, str):
            raise ValueError("gitlab.pipeline_url must be a string or null")

        candidate = raw_url.strip()
        normalized_gitlab = dict(value)
        if not candidate:
            normalized_gitlab["pipeline_url"] = None
            return normalized_gitlab

        if (
            re.match(r"^https?://[^/?#\\\s]", candidate, re.IGNORECASE) is None
            or "\\" in candidate
            or any(char.isspace() for char in candidate)
        ):
            raise ValueError("gitlab.pipeline_url must be an absolute HTTP or HTTPS URL")

        try:
            pipeline_url = _HTTP_URL_ADAPTER.validate_python(candidate)
        except ValidationError as exc:
            raise ValueError("gitlab.pipeline_url must be an absolute HTTP or HTTPS URL") from exc

        if pipeline_url.username is not None or pipeline_url.password is not None:
            raise ValueError("gitlab.pipeline_url must not contain credentials")

        normalized_url = str(pipeline_url)
        if len(normalized_url) > _MAX_PIPELINE_URL_LENGTH:
            raise ValueError(
                f"gitlab.pipeline_url must be at most {_MAX_PIPELINE_URL_LENGTH} characters"
            )

        normalized_gitlab["pipeline_url"] = normalized_url
        return normalized_gitlab


class PipelineRegisterRequest(BaseModel):
    project_id: int
    pipeline_id: int
    project_path: str
    project_url: Optional[HttpUrl] = None
    ref: Optional[str] = None
    commit_sha: Optional[str] = None
    tracked_jobs: Optional[List[str]] = None


class SecurityControlUpsertRequest(BaseModel):
    control_key: str
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True


class RepositoryControlBindingUpsertRequest(BaseModel):
    status: str
    scope: str = "*"
    source: Optional[str] = None
    evidence: Optional[str] = None
    reason: Optional[str] = None


class ExecutionEventIngestRequest(BaseModel):
    """Version 1 telemetry envelope shared by scanner and delivery adapters."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    event_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._:/-]+$")
    event_type: Literal[
        "execution.transition",
        "step.transition",
        "artifact.created",
        "delivery.transition",
    ]
    source: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    source_scope: str = Field(default="default", min_length=1, max_length=255)
    correlation_key: str = Field(min_length=1, max_length=255)
    observed_at: datetime
    repository_id: Optional[int] = Field(default=None, gt=0)
    product_id: Optional[int] = Field(default=None, gt=0)
    reason: Optional[str] = Field(default=None, max_length=2000)
    data: dict[str, Any]

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value

    @field_validator("data")
    @classmethod
    def data_contains_no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        sensitive = {"authorization", "token", "password", "secret", "api_key", "access_token", "private_token"}

        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if str(key).lower() in sensitive:
                        raise ValueError(f"data contains sensitive key: {key}")
                    inspect(nested)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested)

        inspect(value)
        return value

    @model_validator(mode="after")
    def content_contains_no_secret_values(self):
        patterns = (
            re.compile(r"\bauthorization\s*:\s*(?:bearer|basic)\s+\S+", re.IGNORECASE),
            re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
            re.compile(r"[?&](?:access_?token|token|api_?key|password|secret)=[^&#\s]+", re.IGNORECASE),
            re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
            re.compile(r"\b(?:glpat-|gh[pousr]_)[A-Za-z0-9_-]{8,}", re.IGNORECASE),
            re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        )

        def inspect(item: Any) -> None:
            if isinstance(item, str) and any(pattern.search(item) for pattern in patterns):
                raise ValueError("event contains a sensitive value")
            if isinstance(item, dict):
                for nested in item.values():
                    inspect(nested)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested)

        inspect(self.reason)
        inspect(self.data)
        return self

# Response schemas
class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    path: str
    gitlab_base_url: str
    created_at: datetime

class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    path: str
    last_report_at: Optional[datetime]
    overall_status: RepositoryStatus
    created_at: datetime

class JobReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    pipeline_url: Optional[str] = None
    job_name: str
    job_status: JobStatus
    duration_seconds: Optional[float] = None
    finished_at: datetime
    ref: Optional[str] = None
    created_at: datetime

class ProductWithRepositories(ProductResponse):
    repositories: List[RepositoryResponse]

class RepositoryWithJobs(RepositoryResponse):
    job_reports: List[JobReportResponse]

class DashboardStats(BaseModel):
    total_products: int
    total_repositories: int
    total_jobs: int
    success_rate: float
    failed_jobs_count: int
