"""Provider-neutral report destinations with encrypted authentication."""
from uuid import uuid4
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func
from .database import Base


class AsocDestination(Base):
    __tablename__ = "asoc_destinations"
    id = Column(Integer, primary_key=True)
    config_version = Column(String(36), nullable=False, default=lambda: str(uuid4()))
    name = Column(String(120), nullable=False)
    provider = Column(String(32), nullable=False)
    api_base_url = Column(String(2048), nullable=False)
    settings_json = Column(Text, nullable=False, default="{}")
    secrets_encrypted = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_by = Column(String(128), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AsocReportDelivery(Base):
    __tablename__ = "asoc_report_deliveries"
    id = Column(Integer, primary_key=True)
    request_id = Column(String(80), nullable=False, unique=True)
    destination_id = Column(Integer, ForeignKey("asoc_destinations.id", ondelete="RESTRICT"), nullable=False, index=True)
    config_version = Column(String(36), nullable=False)
    target_json = Column(Text, nullable=False)
    provider = Column(String(32), nullable=False)
    report_format = Column(String(32), nullable=False)
    digest_sha256 = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False)
    remote_reference = Column(String(128))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
