# Store Intelligence API

**Real-time retail store analytics from CCTV footage**

A complete end-to-end system that processes raw CCTV clips, tracks customers, detects behaviors, and exposes REST APIs for store metrics, conversion funnels, queue monitoring, and anomaly detection.

---

## Quick Start (5 minutes)

### Prerequisites
- Docker & Docker Compose
- OR: Python 3.11+, pip

### Option 1: Docker (Recommended)

```bash
# Clone and navigate to project
cd store-intelligence

# Start API server
docker compose up --build

# In another terminal, see API
curl http://localhost:8000/health
```

The API is now running at `http://localhost:8000`.

### Option 2: Local Python

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start API server
uvicorn app.main:app --reload

# The API runs at http://localhost:8000
```

---

## Processing CCTV Clips

### Step 1: Prepare Video Data

Place CCTV clips in a directory:
```
data/
├── CAM_1.mp4  (Entry camera, ~140 sec)
├── CAM_2.mp4  (Main floor, ~140 sec)
└── CAM_3.mp4  (Billing area, ~140 sec)
```

### Step 2: Define Store Layout

Create `data/store_layout.json`:

```json
{
  "store_id": "STORE_BLR_001",
  "store_name": "Brigade Bangalore",
  "zones": {
    "ENTRY": {
      "x_min": 0,
      "y_min": 0,
      "x_max": 400,
      "y_max": 1080,
      "name": "Entry/Exit Threshold"
    },
    "SKINCARE": {
      "x_min": 400,
      "y_min": 200,
      "x_max": 800,
      "y_max": 600,
      "name": "Skincare Zone"
    },
    "MAKEUP": {
      "x_min": 800,
      "y_min": 200,
      "x_max": 1400,
      "y_max": 600,
      "name": "Makeup Zone"
    },
    "BILLING": {
      "x_min": 1400,
      "y_min": 600,
      "x_max": 1920,
      "y_max": 1080,
      "name": "Billing Counter"
    }
  },
  "open_hours": {
    "start": "10:00",
    "end": "21:00"
  }
}
```

### Step 3: Run Detection Pipeline

```bash
# Process all clips and emit structured events
python pipeline/detect.py \
  --video_dir data/ \
  --store_id STORE_BLR_001 \
  --output events_output/events.jsonl

# Output file: events_output/events.jsonl
# 1200-1600 structured events depending on clip content
```

**Sample Output** (`events_output/events.jsonl`):
```json
{"event_id":"550e8400-e29b-41d4-a716-446655440000","store_id":"STORE_BLR_001","camera_id":"CAM_ENTRY_01","visitor_id":"VIS_c8a2f1","event_type":"ENTRY","timestamp":"2026-04-10T16:55:36Z","zone_id":null,"dwell_ms":0,"is_staff":false,"confidence":0.95,"metadata":{}}
{"event_id":"550e8400-e29b-41d4-a716-446655440001","store_id":"STORE_BLR_001","camera_id":"CAM_2","visitor_id":"VIS_c8a2f1","event_type":"ZONE_ENTER","timestamp":"2026-04-10T16:55:45Z","zone_id":"SKINCARE","dwell_ms":0,"is_staff":false,"confidence":0.92,"metadata":{}}
...
```

### Step 4: Ingest Events into API

```bash
# Read events and POST to API in batches
python pipeline/ingest.py \
  --events_file events_output/events.jsonl \
  --api_url http://localhost:8000 \
  --batch_size 500

# Output:
# Batch 1: 500 events → 200ms → { "successful": 500, "failed": 0, "duplicates": 0 }
# Batch 2: 400 events → 180ms → { "successful": 400, "failed": 0, "duplicates": 0 }
# Total: 900 events ingested in 2 batches
```

---

## API Endpoints

### 1. Ingest Events

**POST** `/events/ingest`

Request:
```bash
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {
        "event_id": "550e8400-e29b-41d4-a716-446655440000",
        "store_id": "STORE_BLR_001",
        "camera_id": "CAM_1",
        "visitor_id": "VIS_abc123",
        "event_type": "ENTRY",
        "timestamp": "2026-04-10T16:55:36Z",
        "zone_id": null,
        "dwell_ms": 0,
        "is_staff": false,
        "confidence": 0.95,
        "metadata": {}
      }
    ]
  }'
```

Response (200 OK):
```json
{
  "successful": 1,
  "failed": 0,
  "duplicates": 0,
  "errors": [],
  "timestamp": "2026-04-10T16:55:40Z"
}
```

---

### 2. Get Store Metrics

**GET** `/stores/{store_id}/metrics`

```bash
curl http://localhost:8000/stores/STORE_BLR_001/metrics | jq
```

Response:
```json
{
  "store_id": "STORE_BLR_001",
  "timestamp": "2026-04-10T17:22:10Z",
  "total_visitors": 145,
  "unique_visitors": 145,
  "conversion_rate": 19.31,
  "avg_dwell_per_zone": {
    "SKINCARE": 4200,
    "MAKEUP": 3100,
    "BILLING": 890
  },
  "current_queue_depth": 3,
  "queue_abandonment_rate": null,
  "transactions_count": 28,
  "total_sales": 12450.50,
  "data_quality_score": 0.85
}
```

---

### 3. Get Conversion Funnel

**GET** `/stores/{store_id}/funnel`

```bash
curl http://localhost:8000/stores/STORE_BLR_001/funnel | jq
```

Response:
```json
{
  "store_id": "STORE_BLR_001",
  "timestamp": "2026-04-10T17:22:10Z",
  "funnel": [
    {
      "stage": "Entry",
      "visitor_count": 145,
      "drop_off_pct": 0.0,
      "next_stage": "Zone Visit"
    },
    {
      "stage": "Zone Visit",
      "visitor_count": 132,
      "drop_off_pct": 8.97,
      "next_stage": "Billing"
    },
    {
      "stage": "Billing",
      "visitor_count": 48,
      "drop_off_pct": 63.64,
      "next_stage": "Purchase"
    },
    {
      "stage": "Purchase",
      "visitor_count": 28,
      "drop_off_pct": 41.67,
      "next_stage": null
    }
  ],
  "total_visitors": 145,
  "converted_visitors": 28,
  "conversion_rate": 19.31
}
```

---

### 4. Get Zone Heatmap

**GET** `/stores/{store_id}/heatmap`

```bash
curl http://localhost:8000/stores/STORE_BLR_001/heatmap | jq
```

Response:
```json
{
  "store_id": "STORE_BLR_001",
  "timestamp": "2026-04-10T17:22:10Z",
  "zones": [
    {
      "zone_id": "SKINCARE",
      "zone_name": "Skincare Zone",
      "visit_frequency": 98,
      "avg_dwell_ms": 4200,
      "intensity_0_100": 100.0,
      "data_confidence": 1.0
    },
    {
      "zone_id": "MAKEUP",
      "zone_name": "Makeup Zone",
      "visit_frequency": 75,
      "avg_dwell_ms": 3100,
      "intensity_0_100": 76.5,
      "data_confidence": 1.0
    },
    {
      "zone_id": "BILLING",
      "zone_name": "Billing Counter",
      "visit_frequency": 48,
      "avg_dwell_ms": 890,
      "intensity_0_100": 48.9,
      "data_confidence": 1.0
    }
  ],
  "data_confidence": 1.0
}
```

---

### 5. Get Anomalies

**GET** `/stores/{store_id}/anomalies`

```bash
curl http://localhost:8000/stores/STORE_BLR_001/anomalies | jq
```

Response:
```json
{
  "store_id": "STORE_BLR_001",
  "timestamp": "2026-04-10T17:22:10Z",
  "anomalies": [
    {
      "anomaly_type": "BILLING_QUEUE_SPIKE",
      "severity": "WARN",
      "message": "Queue depth 7 > 1.5x historical average 4.2",
      "value": 7.0,
      "threshold": 6.3,
      "suggested_action": "Consider opening additional billing counters",
      "detected_at": "2026-04-10T17:20:00Z"
    }
  ],
  "has_critical": false
}
```

---

### 6. Health Check

**GET** `/health`

```bash
curl http://localhost:8000/health | jq
```

Response:
```json
{
  "status": "healthy",
  "timestamp": "2026-04-10T17:22:10Z",
  "last_event_timestamp": {
    "STORE_BLR_001": "2026-04-10T17:22:00Z"
  },
  "stale_feeds": [],
  "uptime_seconds": 3600.5,
  "db_status": "connected"
}
```

---

## Project Structure

```
store-intelligence/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI application (all endpoints)
│   ├── models.py               # Pydantic schemas (events, metrics, responses)
│   ├── database.py             # SQLAlchemy ORM + DB setup
│   └── ingest.py               # Event ingestion utility
├── pipeline/
│   ├── detect.py               # CCTV processing + event emission
│   ├── ingest.py               # Event batch ingestion script
│   └── run.sh                  # One-command wrapper
├── tests/
│   ├── test_api.py             # API endpoint tests
│   ├── test_models.py          # Schema validation tests
│   └── test_pipeline.py        # Detection pipeline tests
├── docs/
│   ├── DESIGN.md               # Architecture + AI-assisted decisions
│   └── CHOICES.md              # 3 key design decisions with reasoning
├── data/
│   ├── store_layout.json       # Zone definitions (user-provided)
│   ├── pos_transactions.csv    # Transaction data (user-provided)
│   └── CAM_*.mp4               # CCTV clips (user-provided)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md                   # This file
```

---

## Running Tests

```bash
# Run all tests with coverage
pytest tests/ -v --cov=app --cov=pipeline

# Run specific test file
pytest tests/test_api.py -v

# Run with output
pytest tests/test_models.py -s
```

**Test Coverage Target:** >70% (statement coverage)

Tests cover:
- ✓ Event schema validation (valid/invalid cases)
- ✓ API endpoint correctness (happy path + edge cases)
- ✓ Funnel deduplication (re-entry handling)
- ✓ Empty store handling
- ✓ All-staff clip (zero customer metrics)
- ✓ Idempotent event ingestion

---

## Configuration

### Environment Variables

```bash
# Database (default: SQLite local)
DATABASE_URL=sqlite:///./store_intelligence.db

# For production with PostgreSQL
DATABASE_URL=postgresql://user:pass@localhost/store_db

# API port
API_PORT=8000

# Log level
LOG_LEVEL=INFO
```

### Zone Definitions

Edit `data/store_layout.json` to define store zones. Each zone is a rectangle in pixel coordinates:

```json
{
  "zones": {
    "ZONE_ID": {
      "x_min": 0,
      "y_min": 0,
      "x_max": 1920,
      "y_max": 1080,
      "name": "Human-readable name"
    }
  }
}
```

---

## Troubleshooting

### Docker Compose Won't Start

```bash
# Check if port 8000 is in use
lsof -i :8000

# Remove containers and start fresh
docker compose down --volumes
docker compose up --build
```

### API Returns 503 Service Unavailable

```bash
# Check database status
curl http://localhost:8000/health

# Verify database file exists
ls -la data/store_intelligence.db

# Reset database
rm data/store_intelligence.db
docker compose restart api
```

### Detection Pipeline Crashes on Video Processing

```bash
# Check if OpenCV can read the video
python -c "import cv2; cap = cv2.VideoCapture('data/CAM_1.mp4'); print(f'Frames: {cap.get(cv2.CAP_PROP_FRAME_COUNT)}')"

# Verify video codec is supported (try H.264)
ffmpeg -i data/CAM_1.mp4 -vf fps=30 -an test_frame.png
```

### No Events in Response

```bash
# Check if events were ingested
curl http://localhost:8000/stores/STORE_BLR_001/metrics

# Ingest test event manually
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events": [{"event_id": "test-1", "store_id": "STORE_BLR_001", ...}]}'
```

---

## Performance Notes

**Ingestion:** 500 events batched → 200-300ms per batch  
**Metrics Query:** 1000+ events in DB → <100ms response  
**Funnel Query:** Complex aggregation → <150ms response  
**Health Check:** <10ms (no DB query)

For 40 stores × 500 events/hour:
- Daily event volume: 480,000 events
- Database size (SQLite): ~100MB
- Query latency (PostgreSQL): <50ms at scale

---

## Deployment to Production

### Recommended Stack

```yaml
# docker-compose.prod.yml
version: '3.8'
services:
  api:
    image: store-intelligence:latest
    environment:
      DATABASE_URL: postgresql://prod_user:${DB_PASS}@postgres:5432/store_db
    depends_on:
      - postgres
  
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: store_db
      POSTGRES_PASSWORD: ${DB_PASS}
    volumes:
      - pg_data:/var/lib/postgresql/data

volumes:
  pg_data:
```

### Monitoring

```bash
# Structured logs to stdout (captured by container orchestrator)
docker logs store-intelligence-api | grep "CRITICAL"

# Prometheus metrics endpoint (future work)
curl http://localhost:8000/metrics
```

---

## Key Design Decisions

See [DESIGN.md](docs/DESIGN.md) for architecture overview and AI-assisted decisions.  
See [CHOICES.md](docs/CHOICES.md) for the 3 critical design choices with trade-off analysis.

---

## License

Challenge submission for UpGrad Placements — April 2026.

---

## Contact

For questions about the implementation, see [DESIGN.md](docs/DESIGN.md) and [CHOICES.md](docs/CHOICES.md).

# purplle
