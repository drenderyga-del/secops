from sqlalchemy import Column, DateTime, String
from sqlalchemy.sql import func

from .database import Base


class WorkerHeartbeat(Base):
    """Liveness row per long-running worker; updated every loop iteration."""

    __tablename__ = "worker_heartbeats"

    name = Column(String(64), primary_key=True)
    last_beat_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
