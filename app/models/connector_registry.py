"""Operational projections for configured connectors and live scan runtimes."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class Connector(Base):
    __tablename__ = "connectors"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False)
    connector_type = Column(String(64), nullable=False, index=True)
    endpoint = Column(String(1024), nullable=True)
    credential_ref = Column(String(256), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    health_status = Column(String(24), nullable=False, default="unknown", index=True)
    health_reason = Column(Text, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    refreshed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    instances = relationship("ScannerInstance", back_populates="connector", cascade="all, delete-orphan")


class ScannerInstance(Base):
    __tablename__ = "scanner_instances"

    id = Column(Integer, primary_key=True)
    connector_id = Column(Integer, ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False, index=True)
    instance_key = Column(String(128), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False)
    instance_type = Column(String(32), nullable=False, default="scanner")
    endpoint = Column(String(1024), nullable=True)
    version = Column(String(128), nullable=True)
    capabilities_json = Column(Text, nullable=False, default="[]")
    health_status = Column(String(24), nullable=False, default="unknown", index=True)
    health_reason = Column(Text, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    connector = relationship("Connector", back_populates="instances")
