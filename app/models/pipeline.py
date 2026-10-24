import enum
from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    String,
    Enum,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base


class PipelineStatus(str, enum.Enum):
    REGISTERED = "registered"
    POLLING = "polling"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"


class ActivePipeline(Base):
    __tablename__ = "active_pipelines"
    __table_args__ = (
        UniqueConstraint("project_id", "pipeline_id", name="uq_active_pipeline_project"),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, nullable=False)
    pipeline_id = Column(Integer, nullable=False)
    repository_id = Column(Integer, ForeignKey("repositories.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    status = Column(Enum(PipelineStatus, native_enum=False, length=16), nullable=False, default=PipelineStatus.REGISTERED)
    commit_sha = Column(String(64), nullable=True)
    git_ref = Column(String(120), nullable=True)
    tracked_jobs = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    web_url = Column(String(255), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    last_polled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    stale_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)

    repository = relationship("Repository", back_populates="active_pipelines")
    product = relationship("Product", back_populates="active_pipelines")
    jobs = relationship("PipelineJob", back_populates="active_pipeline", cascade="all, delete-orphan")


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"
    __table_args__ = (
        UniqueConstraint("active_pipeline_id", "gitlab_job_id", name="uq_pipeline_job_gitlab"),
    )

    id = Column(Integer, primary_key=True)
    active_pipeline_id = Column(Integer, ForeignKey("active_pipelines.id", ondelete="CASCADE"), nullable=False)
    gitlab_job_id = Column(Integer, nullable=False)
    name = Column(String(120), nullable=False)
    stage = Column(String(80), nullable=True)
    status = Column(String(32), nullable=False)
    ref = Column(String(120), nullable=True)
    web_url = Column(String(255), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_long_running_alert_at = Column(DateTime(timezone=True), nullable=True)

    active_pipeline = relationship("ActivePipeline", back_populates="jobs")
