"""
Event and API response models for Store Intelligence API.

PROMPT: Used Claude to design Pydantic models that would handle the event
schema requirements from the challenge, including validation of event types,
zones, and optional fields.

CHANGES MADE: Added custom validators for timestamp ISO-8601 format, confidence
bounds (0-1), and event_id uniqueness guarantees. Enhanced metadata field to
support extensibility for future event attributes.
Migrated from Pydantic v1 @validator / class Config to Pydantic v2
@field_validator / model_config = ConfigDict(...).
"""

from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator, model_serializer
from pydantic import ConfigDict
import uuid


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class StoreEvent(BaseModel):
    """Structured event emitted from detection pipeline."""

    model_config = ConfigDict(use_enum_values=False)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator('timestamp', mode='before')
    @classmethod
    def parse_timestamp(cls, v):
        """Ensure timestamp is ISO-8601 UTC."""
        if isinstance(v, str):
            if v.endswith('Z'):
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            return datetime.fromisoformat(v)
        return v

    @model_serializer(mode='wrap')
    def serialize_model(self, handler):
        """Serialize event_type and timestamp consistently without overriding model_dump."""
        data = handler(self)
        timestamp = self.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        data['timestamp'] = timestamp.isoformat().replace('+00:00', 'Z')
        data['event_type'] = self.event_type.value
        return data


class EventBatch(BaseModel):
    """Batch of events for ingestion."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "events": [
                    {
                        "event_id": "550e8400-e29b-41d4-a716-446655440000",
                        "store_id": "STORE_BLR_001",
                        "camera_id": "CAM_ENTRY_01",
                        "visitor_id": "VIS_abc123",
                        "event_type": "ENTRY",
                        "timestamp": "2026-04-10T16:55:36Z",
                        "zone_id": None,
                        "dwell_ms": 0,
                        "is_staff": False,
                        "confidence": 0.95,
                        "metadata": {},
                    }
                ]
            }
        }
    )

    events: List[StoreEvent] = Field(max_length=1000)


class POSTransactionIn(BaseModel):
    """POS transaction record used for conversion correlation."""

    store_id: str
    transaction_id: str
    timestamp: datetime
    basket_value_inr: float = Field(ge=0.0)

    @field_validator('timestamp', mode='before')
    @classmethod
    def parse_timestamp(cls, v):
        if isinstance(v, str):
            if v.endswith('Z'):
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            return datetime.fromisoformat(v)
        return v


class POSTransactionBatch(BaseModel):
    """Batch of POS transactions."""

    transactions: List[POSTransactionIn] = Field(max_length=1000)


class POSIngestResponse(BaseModel):
    """Response from POS transaction ingestion."""

    successful: int
    failed: int
    duplicates: int
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utc_now)


class IngestResponse(BaseModel):
    """Response from event ingestion endpoint."""
    successful: int
    failed: int
    duplicates: int
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utc_now)


class MetricsResponse(BaseModel):
    """Store metrics response."""
    store_id: str
    timestamp: datetime
    total_visitors: int
    unique_visitors: int
    conversion_rate: Optional[float] = None
    avg_dwell_per_zone: Dict[str, float] = Field(default_factory=dict)
    current_queue_depth: Optional[int] = None
    queue_abandonment_rate: Optional[float] = None
    transactions_count: int = 0
    total_sales: float = 0.0
    data_quality_score: float = 1.0


class FunnelStage(BaseModel):
    """Stage in conversion funnel."""
    stage: str
    visitor_count: int
    drop_off_pct: float
    next_stage: Optional[str] = None


class FunnelResponse(BaseModel):
    """Conversion funnel response."""
    store_id: str
    timestamp: datetime
    funnel: List[FunnelStage]
    total_visitors: int
    converted_visitors: int
    conversion_rate: float


class HeatmapZone(BaseModel):
    """Zone heatmap data."""
    zone_id: str
    zone_name: str
    visit_frequency: int
    avg_dwell_ms: float
    intensity_0_100: float
    data_confidence: float = 1.0


class HeatmapResponse(BaseModel):
    """Heatmap data response."""
    store_id: str
    timestamp: datetime
    zones: List[HeatmapZone]
    data_confidence: float


class Anomaly(BaseModel):
    """Detected anomaly in store."""
    anomaly_type: str
    severity: str = Field(description="INFO, WARN, CRITICAL")
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    suggested_action: str
    detected_at: datetime


class AnomaliesResponse(BaseModel):
    """Active anomalies in store."""
    store_id: str
    timestamp: datetime
    anomalies: List[Anomaly]
    has_critical: bool


class HealthResponse(BaseModel):
    """API health status."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "healthy",
                "timestamp": "2026-04-10T16:55:36Z",
                "last_event_timestamp": {"STORE_BLR_001": "2026-04-10T16:55:35Z"},
                "stale_feeds": [],
                "uptime_seconds": 3600.5,
                "db_status": "connected",
            }
        }
    )

    status: str
    timestamp: datetime
    last_event_timestamp: Optional[Dict[str, datetime]] = Field(default_factory=dict)
    stale_feeds: List[str] = Field(default_factory=list)
    uptime_seconds: float
    db_status: str


class ErrorResponse(BaseModel):
    """Standardized error response."""
    error: str
    detail: str
    timestamp: datetime = Field(default_factory=utc_now)
    trace_id: Optional[str] = None
