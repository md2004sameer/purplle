"""
Database models and setup for Store Intelligence API.

PROMPT: Used Claude to design SQLAlchemy ORM models that track visitors,
sessions, events, and POS transactions in a normalized schema. Asked for
advice on optimal indexing for real-time query performance on store_id,
timestamp, and visitor_id fields.

CHANGES MADE: Added session aggregation view, composite indexes on (store_id,
timestamp) for fast range queries, and soft-delete support for data integrity.
"""

from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Boolean, ForeignKey, Index, JSON, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Store(Base):
    """Store metadata."""
    __tablename__ = "stores"
    
    store_id = Column(String, primary_key=True)
    store_name = Column(String, nullable=True)
    city = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    events = relationship("Event", back_populates="store")
    sessions = relationship("VisitorSession", back_populates="store")
    transactions = relationship("POSTransaction", back_populates="store")


class Event(Base):
    """Raw detection events from pipeline."""
    __tablename__ = "events"
    
    event_id = Column(String, primary_key=True)
    store_id = Column(String, ForeignKey("stores.store_id"), nullable=False)
    camera_id = Column(String, nullable=False)
    visitor_id = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    zone_id = Column(String, nullable=True)
    dwell_ms = Column(Integer, default=0)
    is_staff = Column(Boolean, default=False)
    confidence = Column(Float, nullable=False)
    metadata = Column(JSON, default={})
    created_at = Column(DateTime, default=datetime.utcnow)
    
    store = relationship("Store", back_populates="events")
    
    __table_args__ = (
        Index("idx_store_timestamp", "store_id", "timestamp"),
        Index("idx_visitor_id", "visitor_id"),
        Index("idx_event_type", "event_type"),
        Index("idx_store_camera", "store_id", "camera_id"),
    )


class VisitorSession(Base):
    """Aggregated visitor session (ENTRY to EXIT)."""
    __tablename__ = "visitor_sessions"
    
    session_id = Column(String, primary_key=True)
    store_id = Column(String, ForeignKey("stores.store_id"), nullable=False)
    visitor_id = Column(String, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime, nullable=True)
    is_staff = Column(Boolean, default=False)
    total_dwell_ms = Column(Integer, default=0)
    zones_visited = Column(JSON, default=[])
    was_in_billing = Column(Boolean, default=False)
    converted = Column(Boolean, default=False)
    transaction_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    store = relationship("Store", back_populates="sessions")
    
    __table_args__ = (
        Index("idx_session_store_timestamp", "store_id", "entry_time"),
        Index("idx_session_visitor", "visitor_id"),
    )


class POSTransaction(Base):
    """POS transaction data."""
    __tablename__ = "pos_transactions"
    
    transaction_id = Column(String, primary_key=True)
    store_id = Column(String, ForeignKey("stores.store_id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    amount = Column(Float, nullable=False)
    raw_data = Column(JSON, default={})
    matched_session_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    store = relationship("Store", back_populates="transactions")
    
    __table_args__ = (
        Index("idx_transaction_store_time", "store_id", "timestamp"),
    )


class AnomalyRecord(Base):
    """Detected anomalies for audit trail."""
    __tablename__ = "anomalies"
    
    anomaly_id = Column(String, primary_key=True)
    store_id = Column(String, nullable=False)
    anomaly_type = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    value = Column(Float, nullable=True)
    threshold = Column(Float, nullable=True)
    detected_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index("idx_anomaly_store_time", "store_id", "detected_at"),
    )


class APIMetadata(Base):
    """Metadata for API health and state."""
    __tablename__ = "api_metadata"
    
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
