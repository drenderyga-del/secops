"""Explicitly shared diagnostic contexts and advisory agent proposals."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func
from .database import Base


class AgentJobContext(Base):
    __tablename__ = "agent_job_contexts"
    job_report_id = Column(Integer, ForeignKey("job_reports.id", ondelete="CASCADE"), primary_key=True)
    context_text = Column(Text, nullable=False)
    version = Column(String(64), nullable=False)
    shared_by = Column(String(128), nullable=False)
    shared_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AgentTriageProposal(Base):
    __tablename__ = "agent_triage_proposals"
    __table_args__ = (UniqueConstraint("agent_id", "request_id", name="uq_agent_triage_request"),)
    id = Column(Integer, primary_key=True)
    job_report_id = Column(Integer, ForeignKey("job_reports.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id = Column(String(80), nullable=False)
    request_id = Column(String(80), nullable=False)
    context_version = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    state = Column(String(16), nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reviewed_by = Column(String(128))
    reviewed_at = Column(DateTime(timezone=True))
    review_note = Column(Text)
