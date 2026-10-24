from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
from .database import Base

class RepositoryStatus(enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"

class Repository(Base):
    __tablename__ = "repositories"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    path = Column(String, nullable=False, index=True)
    last_report_at = Column(DateTime(timezone=True))
    overall_status = Column(Enum(RepositoryStatus), default=RepositoryStatus.UNKNOWN)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    product = relationship("Product", back_populates="repositories")
    control_bindings = relationship("RepositoryControlBinding", back_populates="repository", cascade="all, delete-orphan")
    job_reports = relationship("JobReport", back_populates="repository", cascade="all, delete-orphan")
    active_pipelines = relationship("ActivePipeline", back_populates="repository", cascade="all, delete-orphan")
