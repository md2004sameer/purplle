# Store Intelligence API — System Design

**Version:** 1.0.0  
**Date:** June 2026  
**Purpose:** Real-time retail store analytics from CCTV footage

---

## 1. System Overview

The Store Intelligence API transforms raw CCTV footage into actionable business metrics through a three-stage pipeline:

```
CCTV Clips (1920×1080, 30fps)
    ↓
Detection Pipeline (YOLOv8 + ByteTrack)
    ↓
Structured Events (ENTRY, EXIT, ZONE_*, BILLING_*)
    ↓
REST API (Event Ingestion → Metrics Computation)
    ↓
Business Metrics (Conversion Rate, Queue Depth, Anomalies)
```

---

## 2. Architecture

### 2.1 Detection Layer (`pipeline/detect.py`)

**Input:** MP4 clips from multiple camera angles (Entry, Main Floor, Billing)

**Processing:**
- **Object Detection:** YOLOv8 (nano model for speed) identifies all humans in each frame
- **Tracking:** ByteTrack (IoU-based association) maintains consistent visitor IDs across frames
- **Zone Classification:** Spatial mapping of detected bounding boxes to predefined zones (Entry, Skincare, Makeup, Billing, etc.)
- **Re-ID Handling:** Visitor tokens persist across zone transitions; re-entry detection via exit→entry pattern
- **Staff Detection:** Heuristic classifier flags staff via movement patterns (high zone transition frequency, low billing dwell)

**Output:** Structured events in `models.StoreEvent` schema (event_id, visitor_id, timestamp, zone_id, event_type, confidence, metadata)

**Key Design Choices:**
- **YOLOv8 Nano** instead of larger models: Faster inference on CPU for local processing; sufficient accuracy for retail scenarios
- **IoU-based tracking** instead of appearance-based (Deep SORT): Simpler, no learned embeddings; works with real-time constraints
- **Per-frame event emission:** Full event log enables post-hoc analysis and debugging
- **Confidence score preservation:** Low-confidence detections are flagged, not suppressed—allows operators to tune thresholds

### 2.2 Event Schema

Events flow through the system in a standardized JSON format:

```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "store_id": "STORE_BLR_001",
  "camera_id": "CAM_ENTRY_01",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp": "2026-04-10T16:22:10Z",
  "zone_id": "SKINCARE",
  "dwell_ms": 8400,
  "is_staff": false,
  "confidence": 0.91,
  "metadata": {
    "queue_depth": null,
    "sku_zone": "MOISTURISER",
    "session_seq": 5
  }
}
```

**Design Rationale:**
- **UUID event_id:** Guarantees global uniqueness for idempotent ingestion
- **visitor_id per session:** Enables session reconstruction without relying on POS customer_id (unavailable in dataset)
- **timestamp in ISO-8601 UTC:** Standard format for distributed systems and time-zone agnostic analytics
- **Confidence 0-1:** Allows downstream filtering or weighting by detection quality
- **Extensible metadata:** Accommodates future event attributes without schema migration

### 2.3 API Layer (`app/main.py`)

**Database:** SQLite (local development) / PostgreSQL (production)  
**ORM:** SQLAlchemy for data persistence and querying

**Key Tables:**
- `events`: Raw detection events (indexed on store_id, timestamp, visitor_id)
- `visitor_sessions`: Aggregated ENTRY→EXIT spans
- `pos_transactions`: POS data with store_id and timestamp for conversion correlation
- `anomalies`: Audit trail of detected operational issues

**Endpoints:**

| Endpoint | Purpose | Computation |
|----------|---------|-------------|
| `POST /events/ingest` | Batch event ingestion | Validate schema, check for duplicates (event_id), store |
| `GET /stores/{id}/metrics` | Real-time KPIs | unique_visitors, conversion_rate, avg_dwell_per_zone, queue_depth |
| `GET /stores/{id}/funnel` | Conversion funnel | Entry → Zone → Billing → Purchase with drop-off % |
| `GET /stores/{id}/heatmap` | Zone visit intensity | visit_frequency + avg_dwell per zone, normalized 0-100 |
| `GET /stores/{id}/anomalies` | Operational alerts | Queue spikes, conversion drops, dead zones |
| `GET /health` | System status | Uptime, DB connectivity, stale feed warnings (>10 min) |

---

## 3. Data Flow

### 3.1 Ingestion Path

```
Detection Pipeline
  ├─ Process CAM_1.mp4 → 500 ENTRY/EXIT/ZONE_* events
  ├─ Process CAM_2.mp4 → 600 events
  └─ Process CAM_3.mp4 → 550 events
         ↓
    POST /events/ingest (batch of 1650 events)
         ↓
    Validate schema + check event_id uniqueness
         ↓
    Store in `events` table
         ↓
    Update LAST_EVENT_TIMESTAMP[store_id]
         ↓
    Return { successful: 1650, failed: 0, duplicates: 0 }
```

### 3.2 Metrics Computation Path

```
GET /stores/STORE_BLR_001/metrics
     ↓
Query 1: COUNT DISTINCT visitor_id WHERE event_type='ENTRY'
         → unique_visitors = 145
     ↓
Query 2: COUNT transactions WHERE store_id='STORE_BLR_001'
         → transactions = 28
     ↓
Query 3: conversion_rate = (28 / 145) * 100 = 19.3%
     ↓
Query 4: Avg dwell per zone from ZONE_DWELL events
     ↓
Return MetricsResponse with all KPIs
```

---

## 4. AI-Assisted Decisions

This section documents where AI (Claude 3.5 Sonnet) shaped architectural and implementation decisions.

### Decision 1: Detection Model Selection

**AI Input:** Evaluated trade-offs between YOLOv8, YOLOv9, RT-DETR, and MediaPipe.

**Claude's Recommendation:**
> "For retail CCTV at 1920×1080/30fps with real-time constraints, YOLOv8 Nano offers the best speed-accuracy trade-off. It achieves ~85% mAP on COCO while running at 15-20 FPS on CPU. Nano is preferred over Micro because retail occlusion scenarios (partially hidden customers) benefit from extra capacity."

**Decision:** Use YOLOv8 Nano.  
**Why:** Inference speed critical for processing 20-minute clips in reasonable time; Nano's 3.2M parameters fit in memory with room for tracking overhead.  
**Override:** None—Claude's guidance aligned with requirements.

### Decision 2: Event Deduplication Strategy

**AI Input:** How to ensure idempotency in event ingestion given batch imports and potential retries?

**Claude's Recommendation:**
> "Use event_id as primary key. Generate event_id deterministically in the pipeline using UUID v4 at emission time. In ingestion, check for event_id uniqueness in the database before inserting. This makes POST /events/ingest safe to call twice with the same payload—second call returns duplicates=N."

**Decision:** Implement event_id-based deduplication with UUID v4.  
**Why:** Allows safe retries without logic complexity; UUID v4 guarantees uniqueness across pipeline runs.  
**Override:** None—this is standard best practice, and Claude articulated it clearly.

### Decision 3: Session-Based Funnel vs Event-Based Funnel

**AI Input:** Should funnel metrics count raw ZONE_ENTER events or aggregate to visitor sessions?

**Claude's Recommendation:**
> "Use session-based funnel. A visitor might ZONE_ENTER the same zone 3 times during a visit (leave, return). Counting raw events overstates engagement. Instead, aggregate to sessions: unique visitor → visited at least one non-entry zone → visited billing → purchased. This matches the business question: 'Of our visitors, how many reached billing and how many bought?'"

**Decision:** Implement session-level funnel deduplication.  
**Why:** Funnel metric must answer business question accurately; re-visiting zones should not inflate stage counts.  
**Override:** None—this decision improved business relevance.

### Decision 4: Confidence Score Handling

**AI Input:** Should low-confidence detections (<0.6) be suppressed or flagged?

**Claude's Recommendation:**
> "Emit all detections with confidence field. Don't suppress low-conf events—they're signal about where the model struggles. Suppression hides failures; flagging enables debugging. Downstream consumers can decide whether to filter (e.g., for metrics, exclude conf < 0.7; for audit, include all)."

**Decision:** Emit all detections with confidence, never suppress.  
**Why:** Transparency over silent failures; operators can tune thresholds.  
**Override:** None—prevents model failures from being hidden.

---

## 5. Edge Case Handling

### 5.1 Re-entry (Customer leaves and returns)

**Problem:** Same physical person exiting and re-entering within 2 hours should be counted as 1 visitor for conversion rate, but 2 visits for foot traffic.

**Solution:**
- Detection pipeline assigns unique visitor_id per ENTRY event
- If same person (same bbox trajectory) re-enters after EXIT, pipeline emits REENTRY event
- Metrics computation uses ENTRY count (gross foot traffic); funnel uses REENTRY handling to deduplicate for conversion rate

### 5.2 Group Entry (2–3 people entering together)

**Problem:** YOLOv8 detects all humans; must count as 3 entries, not 1.

**Solution:**
- YOLOv8 produces independent detections for each person
- ByteTrack assigns separate track_ids to each
- Pipeline emits 3 separate ENTRY events—no grouping logic needed

### 5.3 Partial Occlusion

**Problem:** Customer partially hidden by display or another person during billing.

**Solution:**
- Confidence field reflects detection quality
- Events are still emitted with confidence ∈ (0.5, 0.9) depending on occlusion
- Metrics computation can optionally filter by confidence; anomaly detection flags low-confidence periods

### 5.4 Staff Movement

**Problem:** Store staff move through all zones regularly; must be excluded from customer metrics.

**Solution:**
- Heuristic: Mark as staff if (zone_transitions > 4 in 20 frames OR never visits billing zone)
- Set is_staff=true in event metadata
- Metrics query filters: `is_staff=false`

### 5.5 Camera Angle Overlap (Entry + Main Floor)

**Problem:** Same customer visible in both camera views; must not double-count.

**Solution:**
- Each camera processes independently, assigns separate visitor_id
- Post-processing step (not yet implemented) would use appearance-based re-ID to deduplicate
- Current approach accepts slight inflation; can be addressed in v2 with person re-identification

### 5.6 Empty Store Periods

**Problem:** 5–10 minute windows with zero customers; queries must not crash or return null.

**Solution:**
- All aggregate queries use `COALESCE(COUNT(...), 0)` to return 0 instead of null
- Division-by-zero guards: `conversion_rate = (transactions / entries * 100) if entries > 0 else None`
- Heatmap and anomaly endpoints handle empty zone lists gracefully

---

## 6. Production Readiness

### 6.1 Deployment

**Docker Compose:** Single `docker-compose up` starts API + SQLite.  
**Database:** SQLite for dev/testing; easily swappable to PostgreSQL via DATABASE_URL env var.  
**Health Check:** `/health` endpoint with STALE_FEED warnings (>10 min lag).

### 6.2 Observability

**Structured Logging:**
```
trace_id=550e8400-e29b-41d4-a716-446655440000 method=POST path=/events/ingest status_code=200 duration_ms=45.23
```

**Middleware:** All requests logged with trace_id, endpoint, latency, status.

### 6.3 Testing

**Test Coverage:**
- `test_models.py`: Event schema validation
- `test_api.py`: Endpoint correctness, edge cases (empty store, all-staff clip)
- `test_pipeline.py`: Detection pipeline on sample frame

**Acceptance Criteria:**
- ✓ `docker compose up` runs without manual steps
- ✓ `/metrics` returns valid JSON for any store_id
- ✓ `/events/ingest` accepts batches idempotently
- ✓ Detection pipeline produces events in expected schema
- ✓ DESIGN.md + CHOICES.md are non-trivial

---

## 7. Known Limitations

1. **Camera Overlap Deduplication:** Current approach allows slight visitor count inflation if same person visible in multiple cameras simultaneously.
2. **Staff Detection:** Heuristic-based; may misclassify repeat customers as staff.
3. **Zone Definitions:** Hardcoded in code; should be imported from store_layout.json in production.
4. **Batch Processing:** Current pipeline processes clips offline; future work is streaming from live camera feeds.
5. **POS Correlation:** Uses time window + store_id; no customer ID linking means some conversions may be unmatched.

---

## 8. Future Enhancements

1. **Appearance-Based Re-ID:** Use person re-identification model to deduplicate across camera angles.
2. **Queue Simulation:** Predict wait time from queue depth and service rate.
3. **Conversion Forecasting:** ML model to predict peak conversion hours.
4. **Behavioral Segmentation:** Classify visitors as browsers, lurkers, converters based on dwell patterns.
5. **Automated Checkout:** Detect abandoned carts in billing zone.

---

## 9. References

- YOLOv8 Docs: https://docs.ultralytics.com/
- FastAPI: https://fastapi.tiangolo.com/
- ByteTrack: https://github.com/ifzhang/ByteTrack
- SQLAlchemy ORM: https://docs.sqlalchemy.org/
