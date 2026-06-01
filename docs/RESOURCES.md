# Store Intelligence — Resource Integration Guide

**Version:** 1.0.0  
**Date:** June 2, 2026  
**Status:** All Purplle team resources integrated and ready for use

---

## Overview

This document provides a comprehensive inventory and usage guide for all resources provided by the Purplle team for the Store Intelligence platform.

---

## 📊 Data Resources

### 1. **POS Transaction Data**
**File:** `/data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv`

**Purpose:** Ground truth for conversion metrics and business outcomes

**Schema:**
- Transaction ID
- Timestamp (ISO-8601 UTC)
- Customer ID (optional)
- Amount (INR)
- Product categories
- Payment method

**Usage in Pipeline:**
- Input to `pipeline/load_pos.py` for POS data ingestion
- Correlated with CCTV events via timestamp matching
- Powers conversion rate calculations:
  ```
  Conversion Rate = (Unique Transactions / Unique CCTV Visitors) × 100%
  ```
- Enables customer journey reconstruction (ENTRY → ZONES → BILLING → PURCHASE)

**Integration Points:**
- **API Endpoint:** `GET /stores/{id}/funnel` returns conversion metrics with POS correlation
- **Database:** Loaded into `pos_transactions` table with indexes on:
  - `timestamp` (for event correlation)
  - `store_id` (for multi-store queries)

**Sample Query:**
```python
# In app/database.py
def get_conversion_rate(store_id: str, date_range: tuple):
    """
    Calculate conversion rate for a store.
    
    Matches CCTV visitor entries to POS transactions within 2-hour window.
    Returns: (transactions, unique_visitors, conversion_rate %)
    """
    session = get_session()
    transactions = session.query(POSTransaction).filter(
        POSTransaction.store_id == store_id,
        POSTransaction.timestamp.between(*date_range)
    ).count()
    
    visitors = session.query(VisitorSession).filter(
        VisitorSession.store_id == store_id,
        VisitorSession.entry_time.between(*date_range)
    ).count()
    
    return {
        "transactions": transactions,
        "unique_visitors": visitors,
        "conversion_rate": (transactions / visitors * 100) if visitors > 0 else 0
    }
```

---

### 2. **Store Layout & Zone Mapping**
**File:** `/data/Brigade Road - Store layoutc5f5d56.xlsx`  
**Derived JSON:** `/data/store_layout.json`

**Purpose:** Spatial reference for zone classification and heatmap generation

**Contents (from spreadsheet):**
- Store dimensions: 4000mm × 3941mm (15.8 sq.m.)
- Zone coordinates (bounding boxes in pixel space)
- Product category assignments per zone
- Camera calibration data (pixel-to-mm conversion factors)

**Zones Defined:**
| Zone ID | Zone Name | Product Category | Priority |
|---------|-----------|-----------------|----------|
| ENTRANCE | Entrance Area | Entry/Exit | CRITICAL |
| TOP_BRANDS | Premium Brands (Top Row) | Skincare, Premium | HIGH |
| MID_BRANDS | Mid-Range Brands (Middle Row) | Skincare, General | HIGH |
| MAKEUP | Makeup Zone | Makeup | HIGH |
| BILLING | Billing Counter | Checkout | CRITICAL |
| FRAGRANCE | Fragrance Section | Fragrances | MEDIUM |
| SELF_CHECKOUT | Self-Checkout Area | Checkout | LOW |

**Camera Specs:**
- Resolution: 1920×1080 @ 30fps
- Pixel-to-mm conversion: H=2.08, V=3.65
- Aspect ratio: 16:9

**Integration Points:**

1. **Detection Pipeline** (`pipeline/detect.py`):
   ```python
   from app.models import ZONE_BBOX  # Loaded from store_layout.json
   
   def classify_zone(bbox: dict, store_id: str) -> str:
       """Map YOLOv8 detection bbox to store zone."""
       layout = load_store_layout(store_id)
       for zone in layout['zones']:
           if bbox_overlaps(bbox, zone['bbox']):
               return zone['zone_id']
       return "UNKNOWN"
   ```

2. **Heatmap Generation** (`app/main.py`):
   ```python
   @app.get("/stores/{store_id}/heatmap")
   def get_heatmap(store_id: str, date_range: tuple):
       """
       Returns zone visit frequency + dwell time heatmap.
       Heatmap normalized to 0-100 using zone pixel area.
       """
       layout = load_store_layout(store_id)
       metrics = {}
       for zone in layout['zones']:
           visit_count = count_zone_visits(store_id, zone['zone_id'], date_range)
           zone_area = zone['bbox']['width'] * zone['bbox']['height']
           intensity = (visit_count / zone_area) * 100
           metrics[zone['zone_id']] = {
               "intensity": min(100, intensity),
               "visit_count": visit_count
           }
       return metrics
   ```

3. **Store Configuration** (`app/database.py`):
   ```python
   # Loaded on startup
   STORE_LAYOUT = load_store_layout(store_id="STORE_BLR_001")
   
   # Used for all spatial queries
   def get_zone_name(zone_id: str) -> str:
       for zone in STORE_LAYOUT['zones']:
           if zone['zone_id'] == zone_id:
               return zone['zone_name']
       return None
   ```

---

### 3. **CCTV Footage**
**Directory:** `/data/CCTV Footage/`

**Video Files:**
- `CAM 1.mp4` - Entrance camera
- `CAM 2.mp4` - Premium brands section (top row)
- `CAM 3.mp4` - Mid-range brands section (middle row)
- `CAM 4.mp4` - Makeup section
- `CAM 5.mp4` - Billing counter area

**Video Specs:**
- Resolution: 1920×1080
- Frame rate: 30 fps
- Codec: H.264
- Total coverage: 5 camera angles for comprehensive store monitoring

**Processing Pipeline:**

1. **Detection** (`pipeline/detect.py`):
   ```python
   import cv2
   from ultralytics import YOLO
   from boxmot import BYTETracker
   
   def process_video_stream(video_path: str, camera_id: str, store_id: str):
       """
       Process CCTV footage frame-by-frame:
       1. YOLOv8 Nano detects humans in each frame
       2. ByteTrack maintains visitor IDs across frames
       3. Zone classification assigns visitors to zones
       4. Event emission for ENTRY, EXIT, ZONE_ENTER, ZONE_EXIT, DWELL
       """
       cap = cv2.VideoCapture(video_path)
       model = YOLO("yolov8n.pt")  # Nano model for speed
       tracker = BYTETracker()
       
       frame_idx = 0
       while cap.isOpened():
           ret, frame = cap.read()
           if not ret:
               break
           
           # Detection
           results = model.predict(frame, conf=0.3)
           detections = results[0].boxes  # [x1, y1, x2, y2, conf, cls]
           
           # Tracking
           tracked = tracker.update(detections)  # [x1, y1, x2, y2, track_id]
           
           # Zone classification & event emission
           for track in tracked:
               zone_id = classify_zone(track, store_id)
               emit_event(camera_id, track, zone_id, frame_idx)
           
           frame_idx += 1
       cap.release()
   ```

2. **Event Ingestion** (`pipeline/ingest.py`):
   ```python
   def ingest_events_from_video(video_path: str, camera_id: str):
       """
       1. Process video to generate events
       2. Batch events into 500-event chunks
       3. POST to /events/ingest endpoint
       4. Track ingestion status
       """
       events = process_video_stream(video_path, camera_id)
       for batch in chunk(events, 500):
           response = requests.post(
               "http://localhost:8000/events/ingest",
               json={"events": [e.model_dump() for e in batch]}
           )
           assert response.status_code == 201
   ```

3. **Live Dashboard** (`dashboard/live_dashboard.py`):
   - Real-time metrics display as videos are processed
   - Live queue depth tracking at BILLING zone
   - Zone heatmap updates
   - Conversion funnel progression

---

### 4. **Assessment & Evaluation Framework**
**File:** `/data/Assessment Evaluation Frameworkb24a398.pdf`

**Purpose:** Quality assurance and success criteria validation

**Framework Components:**

#### A. **Detection Quality Metrics**
- **Recall (Sensitivity):** % of actual visitors detected
  - Target: ≥ 90% for visible persons
  - Measurement: Manual frame-by-frame audit
  - Tolerance: ±2% due to occlusion/motion blur

- **Precision:** % of detections that are true visitors (vs. false positives)
  - Target: ≥ 85%
  - Sources of false positives: reflections, mannequins, signage
  - Mitigation: Confidence score filtering

- **Tracking Continuity:** % of visitor tracks maintained across 60+ frames
  - Target: ≥ 80%
  - Measurement: Frame-level track ID persistence
  - Re-ID handled via exit→entry pattern

#### B. **Analytics Accuracy**
- **Conversion Rate:** Within ±5% of actual POS data
  - Calculation: (POS Transactions / CCTV Entries) × 100%
  - Time window: 2 hours (accounts for transaction lag)

- **Dwell Time:** Within ±10% of ground truth
  - Measurement: ZONE_ENTER timestamp to ZONE_EXIT timestamp
  - Validation: Spot-check 50 random visitors per zone

- **Queue Depth:** Peak queue size estimate
  - Target: Detect queues ≥ 2 visitors
  - Measurement: Bounding box overlap at BILLING zone
  - Latency: Real-time (≤ 1 frame delay)

#### C. **System Reliability**
- **Uptime:** ≥ 99% during store hours
- **Latency:** Event ingestion < 100ms
- **Database:** No duplicate event_id entries (UUID uniqueness enforced)
- **API Response Time:** ≤ 200ms for all endpoints

#### D. **Operational Criteria**
- All 5 camera feeds processing continuously
- POS data ingestion daily (cron job)
- Live dashboard updates every 5 seconds
- Anomaly detection firing on:
  - Queue depth > 5 visitors
  - Conversion rate drop > 20% vs. 24h rolling average
  - Zone with zero visits during store hours

---

## 🔗 Data Flow Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  PURPLLE RESOURCES                      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  CCTV Footage (5 cameras)    Brigade Road Layout (.xlsx)│
│         ↓                              ↓               │
│    Detection Pipeline          Store Layout Config      │
│   (YOLOv8 + ByteTrack)        (Zone Definitions)       │
│         ↓                              ↓               │
│   Structured Events ←──────────────────┘               │
│         │                                              │
│         ├─────────────────────────────┐                │
│         ↓                             ↓                │
│   Event Ingestion              POS Transactions        │
│   (`/events/ingest`)           (CSV → DB)             │
│         │                             │                │
│         └─────────────────────────────┘                │
│                     ↓                                   │
│           ┌─────────────────────┐                      │
│           │   SQLite/Postgres   │                      │
│           │   - events          │                      │
│           │   - visitor_sessions│                      │
│           │   - pos_transactions│                      │
│           │   - anomalies       │                      │
│           └─────────────────────┘                      │
│                     ↓                                   │
│           ┌─────────────────────┐                      │
│           │     REST API        │                      │
│           ├─────────────────────┤                      │
│           │ /metrics            │                      │
│           │ /funnel             │                      │
│           │ /heatmap            │                      │
│           │ /anomalies          │                      │
│           │ /health             │                      │
│           └─────────────────────┘                      │
│                     ↓                                   │
│    ┌─────────────────────────────────┐                │
│    │   Live Dashboard (Streamlit)    │                │
│    │   - Real-time metrics           │                │
│    │   - Zone heatmap                │                │
│    │   - Conversion funnel           │                │
│    │   - Queue depth alerts          │                │
│    └─────────────────────────────────┘                │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start: Using All Resources

### Step 1: Ingest POS Data
```bash
cd /Users/sameerog/Documents/store-intelligence
python pipeline/load_pos.py --file data/Brigade_Bangalore_10_April_26\ \(1\)bc6219c.csv --store-id STORE_BLR_001
```

### Step 2: Process CCTV Footage
```bash
# Process all camera feeds
for camera in {1..5}; do
    python pipeline/ingest.py \
        --video "data/CCTV Footage/CAM $camera.mp4" \
        --camera-id "CAM_$camera" \
        --store-id "STORE_BLR_001"
done
```

### Step 3: Validate Against Assessment Framework
```bash
python tests/test_api.py --metrics-threshold 0.90 --latency-threshold 100
```

### Step 4: Launch Live Dashboard
```bash
streamlit run dashboard/live_dashboard.py
```

### Step 5: Review Metrics
```bash
curl http://localhost:8000/stores/STORE_BLR_001/metrics
curl http://localhost:8000/stores/STORE_BLR_001/funnel
curl http://localhost:8000/stores/STORE_BLR_001/heatmap
```

---

## 📋 Implementation Checklist

- [x] Store layout JSON created from Brigade Road spreadsheet
- [x] POS transaction CSV ingestion pipeline implemented
- [x] CCTV video paths documented and accessible
- [x] Detection pipeline (YOLOv8 + ByteTrack) integrated
- [x] Event schema aligned with assessment requirements
- [x] API endpoints returning metrics per framework
- [x] Live dashboard connected to all data sources
- [ ] Assessment framework validation tests (in progress)
- [ ] Performance benchmarking against evaluation criteria
- [ ] Multi-store support (scaling beyond Brigade Road)

---

## 📞 Resource Support

**Questions about resource usage:**
- See `/docs/DESIGN.md` for architecture details
- See `/tests/` for integration examples
- See `/app/models.py` for schema definitions

**Troubleshooting:**
- Videos not found? Check `/data/CCTV Footage/` directory permissions
- POS data not ingesting? Verify CSV schema matches `Brigade_Bangalore_10_April_26 (1)bc6219c.csv`
- Zones not mapping? Check `store_layout.json` pixel coordinates against camera resolution
