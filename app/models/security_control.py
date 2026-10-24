"""Scanner-neutral security-control vocabulary and repository bindings."""
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class SecurityControl(Base):
    __tablename__ = "security_controls"

    id = Column(Integer, primary_key=True, index=True)
    control_key = Column(String(128), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    category = Column(String(64), nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    bindings = relationship("RepositoryControlBinding", back_populates="security_control")


class RepositoryControlBinding(Base):
    """Expected control for a repository/scope and its observed configuration.

    This is deliberately not tied to a scanner, job, or execution. Adapters can
    later update ``status`` and evidence without changing the domain contract.
    """

    __tablename__ = "repository_control_bindings"
    __table_args__ = (
        UniqueConstraint("repository_id", "security_control_id", "scope", name="uq_repository_control_scope"),
        CheckConstraint(
            "status IN ('configured', 'missing', 'unknown', 'excluded')",
            name="ck_repository_control_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True)
    security_control_id = Column(Integer, ForeignKey("security_controls.id", ondelete="CASCADE"), nullable=False, index=True)
    scope = Column(String(255), nullable=False, default="*")
    status = Column(String(32), nullable=False, default="missing")
    source = Column(String(64), nullable=True)
    evidence = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    repository = relationship("Repository", back_populates="control_bindings")
    security_control = relationship("SecurityControl", back_populates="bindings")
