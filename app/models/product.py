from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    path = Column(String, nullable=False)
    gitlab_base_url = Column(String, nullable=False)
    asoc_product_slug = Column(String, nullable=True)
    asoc_product_id = Column(String, nullable=True)
    asoc_sync_status = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    repositories = relationship("Repository", back_populates="product", cascade="all, delete-orphan")
    active_pipelines = relationship("ActivePipeline", back_populates="product", cascade="all, delete-orphan")
