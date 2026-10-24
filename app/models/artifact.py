"""Scanner-neutral artifacts, deliveries and producer provenance."""

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("execution_id", "source", "external_id", name="uq_artifact_execution_source_external"),
        CheckConstraint("size_bytes > 0", name="ck_artifact_non_empty"),
    )

    id = Column(Integer, primary_key=True)
    execution_id = Column(Integer, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(64), nullable=False)
    external_id = Column(String(255), nullable=False)
    kind = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="created")
    uri = Column(Text, nullable=False)
    digest_sha256 = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    schema_version = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    execution = relationship("Execution", back_populates="artifacts")
    deliveries = relationship("Delivery", back_populates="artifact", cascade="all, delete-orphan")
    provenance = relationship(
        "ArtifactProvenance",
        back_populates="artifact",
        cascade="all, delete-orphan",
        uselist=False,
    )


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("artifact_id", "destination", "attempt_key", name="uq_delivery_artifact_attempt"),
        CheckConstraint(
            "status IN ('requested','running','confirmed','failed','skipped')",
            name="ck_delivery_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    execution_id = Column(Integer, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_id = Column(Integer, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(64), nullable=False)
    destination = Column(String(255), nullable=False)
    attempt_key = Column(String(255), nullable=False, default="initial")
    external_id = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, index=True)
    failure_category = Column(String(32), nullable=True)
    failure_reason = Column(String(64), nullable=True)
    error_text = Column(Text, nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    artifact = relationship("Artifact", back_populates="deliveries")


class ArtifactProvenance(Base):
    __tablename__ = "artifact_provenance"
    __table_args__ = (UniqueConstraint("artifact_id", name="uq_artifact_provenance_artifact"),)

    id = Column(Integer, primary_key=True)
    artifact_id = Column(Integer, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_id = Column(Integer, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_step_id = Column(Integer, ForeignKey("execution_steps.id", ondelete="SET NULL"), nullable=True)
    source_revision = Column(String(255), nullable=True)
    source_ref = Column(String(255), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    context_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    artifact = relationship("Artifact", back_populates="provenance")
