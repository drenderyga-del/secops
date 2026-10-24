"""Persisted integration configuration and its operator audit trail."""
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from .database import Base


class IntegrationConfig(Base):
    __tablename__ = "integration_configs"

    connector = Column(String(32), primary_key=True)
    values_json = Column(Text, nullable=False, default="{}")
    secrets_encrypted = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_by = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    tested_at = Column(DateTime(timezone=True), nullable=True)
    test_status = Column(String(24), nullable=True)
    test_message = Column(Text, nullable=True)


class IntegrationConfigEvent(Base):
    __tablename__ = "integration_config_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    connector = Column(String(32), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    actor = Column(String(128), nullable=True)
    changed_fields_json = Column(Text, nullable=False, default="[]")
    success = Column(Boolean, nullable=False, default=True)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IntegrationInstanceTestState(Base):
    """Latest and last-successful health state for one connector instance."""

    __tablename__ = "integration_instance_test_states"

    connector = Column(String(32), primary_key=True)
    instance_id = Column(String(64), primary_key=True)
    tested_at = Column(DateTime(timezone=True), nullable=False)
    test_status = Column(String(24), nullable=False)
    test_message = Column(Text, nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_success_message = Column(Text, nullable=True)
