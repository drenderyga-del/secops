from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base


class ConfigScanRun(Base):
    __tablename__ = "config_scan_runs"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="queued")
    trigger = Column(String(32), nullable=False, default="poller")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    total_projects = Column(Integer, default=0, nullable=False)
    scanned_projects = Column(Integer, default=0, nullable=False)
    findings_count = Column(Integer, default=0, nullable=False)
    summary_json = Column(Text, nullable=True)
    report_json_path = Column(String, nullable=True)
    report_html_path = Column(String, nullable=True)
    scope_path = Column(String, nullable=True)
    error_text = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product", backref="config_scan_runs")
    findings = relationship("ConfigScanFinding", back_populates="run", cascade="all, delete-orphan")


class ConfigScanFinding(Base):
    __tablename__ = "config_scan_findings"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("config_scan_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    repository_id = Column(Integer, ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    project_path = Column(String, nullable=False)
    project_url = Column(String, nullable=True)
    severity = Column(String(16), nullable=False, index=True)
    rule_id = Column(String(100), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    recommendation = Column(Text, nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    run = relationship("ConfigScanRun", back_populates="findings")
    product = relationship("Product")
    repository = relationship("Repository")
