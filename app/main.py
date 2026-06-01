"""
FastAPI application for Store Intelligence API.

PROMPT: Asked Claude to design a FastAPI structure that handles:
- Event ingestion with idempotency
- Real-time metrics computation
- Funnel analysis with session deduplication
- Anomaly detection
- Health endpoint for monitoring

Got advice on using background tasks for metric aggregation and proper
error handling for edge cases (empty stores, stale feeds, DB unavailable).

CHANGES MADE: Added request/response logging with trace IDs, implemented
idempotent event ingestion via event_id uniqueness check, added graceful
degradation (return 503 on DB issues), and proper anomaly detection logic.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Set, Tuple
from contextlib import asynccontextmanager
import json
import uuid

from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import and_, or_, func, case
from sqlalchemy.orm import Session

from app.database import init_db, get_db, engine, Store, Event, VisitorSession, POSTransaction, AnomalyRecord, APIMetadata
from app.models import (
    StoreEvent, IngestResponse, MetricsResponse,
    FunnelResponse, FunnelStage, HeatmapResponse, HeatmapZone,
    AnomaliesResponse, Anomaly, HealthResponse, ErrorResponse,
    POSTransactionBatch, POSIngestResponse
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global state
APP_START_TIME = time.time()
LAST_EVENT_TIMESTAMP = {}
DEAD_ZONE_BASELINE_MIN_VISITS = 5
DEAD_ZONE_RECENT_STORE_MIN_EVENTS = 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_naive() -> datetime:
    return _utc_now().replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    """Normalize aware datetimes before storing/querying SQLite."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _last_event_metadata_key(store_id: str) -> str:
    return f"last_event_timestamp:{store_id}"


def _set_last_event_timestamp(db: Session, events: List[StoreEvent]) -> None:
    """
    Persist the latest event timestamp per store.

    Multiple events for the same store can arrive in one batch, so collapse the
    batch to one upsert per store before touching APIMetadata's unique key.
    """
    latest_by_store: Dict[str, datetime] = {}
    for event_data in events:
        timestamp = _as_naive_utc(event_data.timestamp)
        existing = latest_by_store.get(event_data.store_id)
        if existing is None or timestamp > existing:
            latest_by_store[event_data.store_id] = timestamp

    if not latest_by_store:
        return

    keys_by_store = {
        store_id: _last_event_metadata_key(store_id)
        for store_id in latest_by_store
    }
    existing_metadata = db.query(APIMetadata).filter(
        APIMetadata.key.in_(keys_by_store.values())
    ).all()
    metadata_by_key = {metadata.key: metadata for metadata in existing_metadata}

    for store_id, timestamp in latest_by_store.items():
        LAST_EVENT_TIMESTAMP[store_id] = timestamp
        key = keys_by_store[store_id]
        value = timestamp.isoformat() + "Z"
        metadata = metadata_by_key.get(key)
        if metadata:
            metadata.value = value
            metadata.updated_at = _utc_now_naive()
        else:
            db.add(APIMetadata(key=key, value=value))


def _converted_visitors_for_store(db: Session, store_id: str) -> Set[str]:
    """
    A visitor converts when they were in billing in the 5-minute window
    before a transaction timestamp for the same store.
    """
    if db.bind and db.bind.dialect.name == "sqlite":
        window_start = func.datetime(POSTransaction.timestamp, "-5 minutes")
    else:
        window_start = POSTransaction.timestamp - timedelta(minutes=5)

    billing_visitors = db.query(Event.visitor_id).join(
        POSTransaction,
        and_(
            POSTransaction.store_id == Event.store_id,
            Event.timestamp >= window_start,
            Event.timestamp <= POSTransaction.timestamp,
        ),
    ).filter(
        and_(
            Event.store_id == store_id,
            Event.is_staff == False,
            or_(
                Event.zone_id == 'BILLING',
                Event.event_type == 'BILLING_QUEUE_JOIN',
            ),
        )
    ).distinct().all()

    return {visitor_id for (visitor_id,) in billing_visitors}


def _data_quality_score(db: Session, store_id: str) -> float:
    """Score ingested data using event confidence and required-field completeness."""
    total_events = db.query(func.count(Event.event_id)).filter(
        Event.store_id == store_id
    ).scalar() or 0
    if total_events == 0:
        return 0.0

    avg_confidence = db.query(func.avg(Event.confidence)).filter(
        Event.store_id == store_id
    ).scalar() or 0.0
    complete_events = db.query(
        func.sum(
            case(
                (
                    and_(
                        Event.camera_id != None,
                        Event.visitor_id != None,
                        Event.event_type != None,
                        Event.timestamp != None,
                    ),
                    1,
                ),
                else_=0,
            )
        )
    ).filter(Event.store_id == store_id).scalar() or 0
    completeness = complete_events / total_events
    return round(float(avg_confidence) * completeness, 3)


def _queue_abandonment_rate(db: Session, store_id: str) -> Optional[float]:
    joins = db.query(func.count(Event.event_id)).filter(
        and_(
            Event.store_id == store_id,
            Event.event_type == 'BILLING_QUEUE_JOIN',
            Event.is_staff == False,
        )
    ).scalar() or 0
    if joins == 0:
        return None

    abandons = db.query(func.count(Event.event_id)).filter(
        and_(
            Event.store_id == store_id,
            Event.event_type == 'BILLING_QUEUE_ABANDON',
            Event.is_staff == False,
        )
    ).scalar() or 0
    return (abandons / joins) * 100


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    logger.info("Starting Store Intelligence API")
    init_db()
    yield
    logger.info("Shutting down Store Intelligence API")


app = FastAPI(
    title="Store Intelligence API",
    description="Real-time analytics for retail store CCTV footage",
    version="1.0.0",
    lifespan=lifespan
)


# Middleware for structured logging
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all requests with trace ID."""
    trace_id = str(uuid.uuid4())
    start_time = time.time()
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    logger.info(
        f"trace_id={trace_id} method={request.method} path={request.url.path} "
        f"status_code={response.status_code} duration_ms={process_time*1000:.2f}"
    )
    
    response.headers["X-Trace-ID"] = trace_id
    return response


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.post("/events/ingest", response_model=IngestResponse)
async def ingest_events(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Ingest batch of events from detection pipeline.
    
    Idempotent by event_id. Validates schema, deduplicates, stores.
    Partial success: malformed events are skipped with error report.
    """
    try:
        payload = await request.json()
        raw_events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(raw_events, list):
            raise HTTPException(status_code=422, detail="Request body must contain an events list")
        if len(raw_events) > 500:
            raise HTTPException(status_code=422, detail="Batch size must be <= 500 events")

        successful = 0
        failed = 0
        duplicates = 0
        errors = []

        validated_events: List[StoreEvent] = []
        for idx, raw_event in enumerate(raw_events):
            try:
                validated_events.append(StoreEvent.model_validate(raw_event))
            except ValidationError as e:
                failed += 1
                errors.append({
                    "index": idx,
                    "event_id": raw_event.get("event_id") if isinstance(raw_event, dict) else None,
                    "error": e.errors(),
                })

        if failed and not validated_events:
            raise HTTPException(status_code=422, detail=errors)

        # Ensure stores exist
        store_ids = set(e.store_id for e in validated_events)
        for store_id in store_ids:
            existing = db.query(Store).filter(Store.store_id == store_id).first()
            if not existing:
                db.add(Store(store_id=store_id))
        db.commit()
        
        stored_events: List[StoreEvent] = []
        for event_data in validated_events:
            try:
                # Check for duplicate
                existing_event = db.query(Event).filter(Event.event_id == event_data.event_id).first()
                if existing_event:
                    duplicates += 1
                    continue
                
                # Create event record
                event = Event(
                    event_id=event_data.event_id,
                    store_id=event_data.store_id,
                    camera_id=event_data.camera_id,
                    visitor_id=event_data.visitor_id,
                    event_type=event_data.event_type.value,
                    timestamp=_as_naive_utc(event_data.timestamp),
                    zone_id=event_data.zone_id,
                    dwell_ms=event_data.dwell_ms,
                    is_staff=event_data.is_staff,
                    confidence=event_data.confidence,
                    event_metadata=event_data.metadata,
                )
                db.add(event)
                successful += 1
                stored_events.append(event_data)
                
            except Exception as e:
                failed += 1
                errors.append({
                    "event_id": event_data.event_id,
                    "error": str(e)
                })

        _set_last_event_timestamp(db, stored_events)
        db.commit()
        
        return IngestResponse(
            successful=successful,
            failed=failed,
            duplicates=duplicates,
            errors=errors,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Event ingestion error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable"
        )


@app.post("/pos/ingest", response_model=POSIngestResponse)
async def ingest_pos_transactions(
    batch: POSTransactionBatch,
    db: Session = Depends(get_db),
):
    """Ingest POS transactions used for store-level conversion correlation."""
    successful = 0
    failed = 0
    duplicates = 0
    errors = []

    try:
        store_ids = {txn.store_id for txn in batch.transactions}
        for store_id in store_ids:
            existing = db.query(Store).filter(Store.store_id == store_id).first()
            if not existing:
                db.add(Store(store_id=store_id))
        db.commit()

        for txn in batch.transactions:
            try:
                existing = db.query(POSTransaction).filter(
                    POSTransaction.transaction_id == txn.transaction_id
                ).first()
                if existing:
                    duplicates += 1
                    continue

                db.add(POSTransaction(
                    transaction_id=txn.transaction_id,
                    store_id=txn.store_id,
                    timestamp=_as_naive_utc(txn.timestamp),
                    amount=txn.basket_value_inr,
                    raw_data={
                        "store_id": txn.store_id,
                        "transaction_id": txn.transaction_id,
                        "timestamp": _as_naive_utc(txn.timestamp).isoformat() + "Z",
                        "basket_value_inr": txn.basket_value_inr,
                    },
                ))
                successful += 1
            except Exception as e:
                failed += 1
                errors.append({
                    "transaction_id": txn.transaction_id,
                    "error": str(e),
                })

        db.commit()
        return POSIngestResponse(
            successful=successful,
            failed=failed,
            duplicates=duplicates,
            errors=errors,
        )
    except Exception as e:
        logger.error(f"POS ingestion error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable"
        )


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def get_metrics(store_id: str, db: Session = Depends(get_db)):
    """
    Get real-time metrics for a store.
    
    Excludes is_staff=true events.
    Handles empty stores gracefully.
    """
    try:
        # Count all entry events (includes re-entries) for total_visitors
        entry_events = db.query(func.count(Event.event_id)).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'ENTRY',
                Event.is_staff == False
            )
        ).scalar() or 0

        # Count distinct visitor_ids for unique_visitors
        unique_visitor_count = db.query(func.count(func.distinct(Event.visitor_id))).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'ENTRY',
                Event.is_staff == False
            )
        ).scalar() or 0
        
        # Aggregate POS totals in the database instead of loading all rows.
        transaction_count, total_sales = db.query(
            func.count(POSTransaction.transaction_id),
            func.coalesce(func.sum(POSTransaction.amount), 0.0),
        ).filter(
            POSTransaction.store_id == store_id
        ).one()
        converted_visitors = _converted_visitors_for_store(db, store_id)
        
        # Compute conversion rate
        conversion_rate = None
        if unique_visitor_count > 0:
            conversion_rate = (len(converted_visitors) / unique_visitor_count) * 100
        
        # Avg dwell per zone
        avg_dwell_per_zone = {}
        zones = db.query(Event.zone_id, func.avg(Event.dwell_ms)).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'ZONE_DWELL',
                Event.is_staff == False,
                Event.zone_id != None
            )
        ).group_by(Event.zone_id).all()
        
        for zone_id, avg_dwell in zones:
            if zone_id:
                avg_dwell_per_zone[zone_id] = avg_dwell or 0
        
        # Queue depth (last value)
        queue_depth = None
        queue_events = db.query(Event).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type.in_(['BILLING_QUEUE_JOIN', 'BILLING_QUEUE_ABANDON'])
            )
        ).order_by(Event.timestamp.desc()).limit(10).all()
        
        for event in queue_events:
            if event.event_metadata and 'queue_depth' in event.event_metadata:
                queue_depth = event.event_metadata['queue_depth']
                break
        
        return MetricsResponse(
            store_id=store_id,
            timestamp=_utc_now(),
            total_visitors=entry_events,
            unique_visitors=unique_visitor_count,
            conversion_rate=conversion_rate,
            avg_dwell_per_zone=avg_dwell_per_zone,
            current_queue_depth=queue_depth,
            queue_abandonment_rate=_queue_abandonment_rate(db, store_id),
            transactions_count=transaction_count,
            total_sales=total_sales,
            data_quality_score=_data_quality_score(db, store_id),
        )
    
    except Exception as e:
        logger.error(f"Metrics error for {store_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Temporarily unavailable"
        )


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def get_funnel(store_id: str, db: Session = Depends(get_db)):
    """
    Get conversion funnel.
    
    Entry -> Zone Visit -> Billing Queue -> Purchase
    Counts unique visitors per stage (no double-counting from re-entry).
    """
    try:
        # Stage 1: Entry
        entries = db.query(func.count(func.distinct(Event.visitor_id))).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'ENTRY',
                Event.is_staff == False
            )
        ).scalar() or 0
        
        # Stage 2: Zone Visit
        zone_visitors = db.query(func.count(func.distinct(Event.visitor_id))).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type.in_(['ZONE_ENTER', 'ZONE_DWELL']),
                Event.is_staff == False
            )
        ).scalar() or 0
        
        # Stage 3: Billing Queue — only visitors who explicitly joined the queue
        billing_visitors = db.query(func.count(func.distinct(Event.visitor_id))).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'BILLING_QUEUE_JOIN',
                Event.is_staff == False
            )
        ).scalar() or 0
        
        # Stage 4: Purchase — visitor-level POS correlation, not raw transaction count.
        converted_visitors = _converted_visitors_for_store(db, store_id)
        converted_count = len(converted_visitors)
        
        # Compute drop-offs
        funnel_stages = [
            FunnelStage(
                stage="Entry",
                visitor_count=entries,
                drop_off_pct=0.0,
                next_stage="Zone Visit"
            ),
            FunnelStage(
                stage="Zone Visit",
                visitor_count=zone_visitors,
                drop_off_pct=100 * (1 - (zone_visitors / entries if entries > 0 else 0)),
                next_stage="Billing"
            ),
            FunnelStage(
                stage="Billing",
                visitor_count=billing_visitors,
                drop_off_pct=100 * (1 - (billing_visitors / zone_visitors if zone_visitors > 0 else 0)),
                next_stage="Purchase"
            ),
            FunnelStage(
                stage="Purchase",
                visitor_count=converted_count,
                drop_off_pct=100 * (1 - (converted_count / billing_visitors if billing_visitors > 0 else 0)),
                next_stage=None
            ),
        ]
        
        conversion_rate = (converted_count / entries * 100) if entries > 0 else 0
        
        return FunnelResponse(
            store_id=store_id,
            timestamp=_utc_now(),
            funnel=funnel_stages,
            total_visitors=entries,
            converted_visitors=converted_count,
            conversion_rate=conversion_rate,
        )
    
    except Exception as e:
        logger.error(f"Funnel error: {str(e)}")
        raise HTTPException(status_code=500, detail="Error computing funnel")


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def get_heatmap(store_id: str, db: Session = Depends(get_db)):
    """
    Get zone visit frequency heatmap.
    
    Zones: visit count and avg dwell, normalized 0-100.
    data_confidence = 1.0 if >20 sessions, else lower.
    """
    try:
        zone_stats = []
        
        zones_data = db.query(
            Event.zone_id,
            func.count(func.distinct(Event.visitor_id)).label('visit_count'),
            func.avg(Event.dwell_ms).label('avg_dwell')
        ).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type.in_(['ZONE_ENTER', 'ZONE_DWELL']),
                Event.is_staff == False,
                Event.zone_id != None
            )
        ).group_by(Event.zone_id).all()
        
        if zones_data:
            max_visits = max(z[1] for z in zones_data)
            
            for zone_id, visit_count, avg_dwell in zones_data:
                intensity = (visit_count / max_visits * 100) if max_visits > 0 else 0
                confidence = 1.0 if visit_count >= 20 else 0.6
                
                zone_stats.append(HeatmapZone(
                    zone_id=zone_id,
                    zone_name=zone_id,
                    visit_frequency=visit_count,
                    avg_dwell_ms=avg_dwell or 0,
                    intensity_0_100=intensity,
                    data_confidence=confidence,
                ))
        
        overall_confidence = 1.0 if len(zone_stats) > 0 else 0.5
        
        return HeatmapResponse(
            store_id=store_id,
            timestamp=_utc_now(),
            zones=zone_stats,
            data_confidence=overall_confidence,
        )
    
    except Exception as e:
        logger.error(f"Heatmap error: {str(e)}")
        raise HTTPException(status_code=500, detail="Error computing heatmap")


@app.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
async def get_anomalies(store_id: str, db: Session = Depends(get_db)):
    """
    Detect active operational anomalies.
    
    - Queue spike: current queue > historical avg + 2*stddev
    - Conversion drop: today's rate < 7-day avg - threshold
    - Dead zone: no visits in 30 min
    """
    try:
        anomalies = []
        now = _utc_now_naive()

        # --- Queue spike: compare current hour against previous 24-hour baseline ---
        # Historical baseline: queue depths from 24h ago up to 1h ago
        historical_queue_events = db.query(Event.event_metadata).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'BILLING_QUEUE_JOIN',
                Event.timestamp >= now - timedelta(hours=25),
                Event.timestamp < now - timedelta(hours=1),
            )
        ).all()

        recent_queue_events = db.query(Event.event_metadata).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'BILLING_QUEUE_JOIN',
                Event.timestamp >= now - timedelta(minutes=60)
            )
        ).all()

        if historical_queue_events and recent_queue_events:
            historical_depths = [e[0].get('queue_depth', 0) for e in historical_queue_events if e[0]]
            current_depths = [e[0].get('queue_depth', 0) for e in recent_queue_events if e[0]]

            if historical_depths and current_depths:
                avg_historical = sum(historical_depths) / len(historical_depths)
                variance = sum((d - avg_historical) ** 2 for d in historical_depths) / len(historical_depths)
                stddev = variance ** 0.5
                current_queue = current_depths[-1]
                threshold = avg_historical + 2 * stddev

                if current_queue > threshold:
                    anomalies.append(Anomaly(
                        anomaly_type="BILLING_QUEUE_SPIKE",
                        severity="WARN",
                        message=f"Queue depth {current_queue} exceeds historical avg {avg_historical:.1f} + 2σ ({threshold:.1f})",
                        value=float(current_queue),
                        threshold=threshold,
                        suggested_action="Consider opening additional billing counters",
                        detected_at=_utc_now(),
                    ))

        # --- Conversion drop: today's rate vs 7-day rolling average ---
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today_start - timedelta(days=7)

        today_entries = db.query(func.count(func.distinct(Event.visitor_id))).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'ENTRY',
                Event.is_staff == False,
                Event.timestamp >= today_start,
            )
        ).scalar() or 0

        today_transactions = db.query(func.count(POSTransaction.transaction_id)).filter(
            and_(
                POSTransaction.store_id == store_id,
                POSTransaction.timestamp >= today_start,
            )
        ).scalar() or 0

        week_entries = db.query(func.count(func.distinct(Event.visitor_id))).filter(
            and_(
                Event.store_id == store_id,
                Event.event_type == 'ENTRY',
                Event.is_staff == False,
                Event.timestamp >= week_ago,
                Event.timestamp < today_start,
            )
        ).scalar() or 0

        week_transactions = db.query(func.count(POSTransaction.transaction_id)).filter(
            and_(
                POSTransaction.store_id == store_id,
                POSTransaction.timestamp >= week_ago,
                POSTransaction.timestamp < today_start,
            )
        ).scalar() or 0

        if today_entries > 0 and week_entries > 0:
            today_rate = (today_transactions / today_entries) * 100
            week_rate = (week_transactions / week_entries) * 100
            drop_threshold = 10.0  # percentage points

            if today_rate < week_rate - drop_threshold:
                anomalies.append(Anomaly(
                    anomaly_type="CONVERSION_DROP",
                    severity="WARN",
                    message=f"Today's conversion {today_rate:.1f}% is more than {drop_threshold}pp below 7-day avg {week_rate:.1f}%",
                    value=today_rate,
                    threshold=week_rate - drop_threshold,
                    suggested_action="Review today's visitor flow and check for operational issues",
                    detected_at=_utc_now(),
                ))
        
        # --- Dead zone: known active zones with no recent traffic while the store is active ---
        recent_store_events = db.query(func.count(Event.event_id)).filter(
            and_(
                Event.store_id == store_id,
                Event.timestamp >= now - timedelta(minutes=30),
                Event.is_staff == False,
            )
        ).scalar() or 0

        recent_zone_events = db.query(Event.zone_id).filter(
            and_(
                Event.store_id == store_id,
                Event.timestamp >= now - timedelta(minutes=30),
                Event.is_staff == False
            )
        ).all()
        
        visited_zones = set(z[0] for z in recent_zone_events if z[0])
        all_zones = db.query(Event.zone_id).filter(
            and_(
                Event.store_id == store_id,
                Event.is_staff == False,
                Event.zone_id != None,
            )
        ).group_by(Event.zone_id).having(
            func.count(Event.event_id) >= DEAD_ZONE_BASELINE_MIN_VISITS
        ).all()
        all_zone_ids = set(z[0] for z in all_zones if z[0])
        
        dead_zones = (
            all_zone_ids - visited_zones
            if recent_store_events >= DEAD_ZONE_RECENT_STORE_MIN_EVENTS
            else set()
        )
        if dead_zones:
            anomalies.append(Anomaly(
                anomaly_type="DEAD_ZONE",
                severity="INFO",
                message=f"No visitors in zones: {', '.join(dead_zones)}",
                suggested_action="Verify zone definitions and camera coverage",
                detected_at=_utc_now(),
            ))
        
        has_critical = any(a.severity == "CRITICAL" for a in anomalies)
        
        return AnomaliesResponse(
            store_id=store_id,
            timestamp=_utc_now(),
            anomalies=anomalies,
            has_critical=has_critical,
        )
    
    except Exception as e:
        logger.error(f"Anomaly detection error: {str(e)}")
        return AnomaliesResponse(
            store_id=store_id,
            timestamp=_utc_now(),
            anomalies=[],
            has_critical=False,
        )


@app.get("/health", response_model=HealthResponse)
async def health_check(db: Session = Depends(get_db)):
    """
    API health status.
    
    Returns:
    - API status (healthy/degraded)
    - Uptime
    - Last event timestamp per store
    - Stale feed warnings (>10 min lag)
    - Database status
    """
    try:
        uptime = time.time() - APP_START_TIME
        db_status = "connected"
        
        # Test DB connectivity
        try:
            from sqlalchemy import text
            db.execute(text("SELECT 1"))
            db_status = "connected"
        except Exception as e:
            logger.warning(f"DB connectivity check failed: {str(e)}")
            db_status = "disconnected"
        
        stale_feeds = []
        last_event_per_store = {}
        
        # Get last event timestamp per store
        for store_id, ts in LAST_EVENT_TIMESTAMP.items():
            last_event_per_store[store_id] = ts
            
            # Check if stale (>10 min). Event timestamps parsed from ISO-8601
            # may be timezone-aware, while legacy/generated values can be naive.
            now = datetime.now(ts.tzinfo) if ts.tzinfo else _utc_now_naive()
            if now - ts > timedelta(minutes=10):
                stale_feeds.append(store_id)
        
        return HealthResponse(
            status="healthy" if db_status == "connected" else "degraded",
            timestamp=_utc_now(),
            last_event_timestamp=last_event_per_store,
            stale_feeds=stale_feeds,
            uptime_seconds=uptime,
            db_status=db_status,
        )
    
    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        raise HTTPException(status_code=503, detail="Service unavailable")


# ============================================================================
# ERROR HANDLING
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Structured error responses."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "detail": exc.detail,
            "timestamp": _utc_now().isoformat().replace('+00:00', 'Z'),
            "trace_id": request.headers.get("X-Trace-ID", "unknown"),
        }
    )


# ============================================================================
# ROOT ENDPOINT
# ============================================================================

@app.get("/")
async def root():
    """API information."""
    return {
        "name": "Store Intelligence API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "ingest": "POST /events/ingest",
            "metrics": "GET /stores/{store_id}/metrics",
            "funnel": "GET /stores/{store_id}/funnel",
            "heatmap": "GET /stores/{store_id}/heatmap",
            "anomalies": "GET /stores/{store_id}/anomalies",
            "health": "GET /health",
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
