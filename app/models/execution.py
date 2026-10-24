"""Scanner-neutral durable execution and step lifecycle."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("source", "source_scope", "correlation_key", name="uq_execution_source_scope_correlation"),
    )

    id = Column(Integer, primary_key=True)
    repository_id = Column(Integer, ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    source = Column(String(64), nullable=False, index=True)
    source_scope = Column(String(255), nullable=False, default="default")
    correlation_key = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, index=True)
    failure_category = Column(String(32), nullable=True)
    failure_reason = Column(String(64), nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    context_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    steps = relationship("ExecutionStep", back_populates="execution", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="execution", cascade="all, delete-orphan")
    events = relationship("ExecutionEvent", back_populates="execution")


class ExecutionStep(Base):
    __tablename__ = "execution_steps"
    __table_args__ = (UniqueConstraint("execution_id", "source", "external_id", name="uq_execution_step_observation"),)

    id = Column(Integer, primary_key=True)
    execution_id = Column(Integer, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(64), nullable=False)
    external_id = Column(String(255), nullable=False)
    name = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False, index=True)
    failure_category = Column(String(32), nullable=True)
    failure_reason = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    context_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    execution = relationship("Execution", back_populates="steps")


class ExecutionEvent(Base):
    """Immutable accepted telemetry envelope and its projection link."""

    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("source", "source_scope", "event_id", name="uq_execution_event_source_scope_id"),
    )

    id = Column(Integer, primary_key=True)
    execution_id = Column(Integer, ForeignKey("executions.id", ondelete="SET NULL"), nullable=True, index=True)
    schema_version = Column(String(16), nullable=False)
    event_id = Column(String(255), nullable=False)
    event_type = Column(String(64), nullable=False, index=True)
    source = Column(String(64), nullable=False, index=True)
    source_scope = Column(String(255), nullable=False, default="default")
    correlation_key = Column(String(255), nullable=False, index=True)
    reason = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)

    execution = relationship("Execution", back_populates="events")
