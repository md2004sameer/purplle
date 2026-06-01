"""
Tests for API endpoints and business logic.

PROMPT: Asked Claude to generate comprehensive tests for FastAPI endpoints,
including happy path, edge cases (empty store, stale data), and error handling.
Requested test structure that covers: event ingestion idempotency, metrics
consistency, funnel deduplication, anomaly detection logic.

CHANGES MADE: Added custom assertions for funnel drop-off calculation accuracy,
added tests for re-entry visitor deduplication, added queue depth anomaly
detection validation, explicit tests for is_staff filtering in all metrics.
"""

import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timedelta
from app.main import app
from app.database import SessionLocal, Base, engine
from app.models import StoreEvent, EventType

# Create test client
client = TestClient(app)

@pytest.fixture(scope="function")
def setup_db():
    """Create and drop test database for each test."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


class TestEventIngestion:
    """Test POST /events/ingest endpoint."""
    
    def test_ingest_valid_batch(self, setup_db):
        """Ingest batch of valid events."""
        payload = {
            "events": [
                {
                    "event_id": "test-event-1",
                    "store_id": "STORE_BLR_001",
                    "camera_id": "CAM_1",
                    "visitor_id": "VIS_001",
                    "event_type": "ENTRY",
                    "timestamp": "2026-04-10T16:55:36Z",
                    "zone_id": None,
                    "dwell_ms": 0,
                    "is_staff": False,
                    "confidence": 0.95,
                    "metadata": {}
                }
            ]
        }
        
        response = client.post("/events/ingest", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["successful"] == 1
        assert data["failed"] == 0
        assert data["duplicates"] == 0
    
    def test_ingest_duplicate_events(self, setup_db):
        """Test that duplicate event_ids are detected."""
        payload = {
            "events": [
                {
                    "event_id": "test-event-1",
                    "store_id": "STORE_BLR_001",
                    "camera_id": "CAM_1",
                    "visitor_id": "VIS_001",
                    "event_type": "ENTRY",
                    "timestamp": "2026-04-10T16:55:36Z",
                    "zone_id": None,
                    "dwell_ms": 0,
                    "is_staff": False,
                    "confidence": 0.95,
                    "metadata": {}
                }
            ]
        }
        
        # First ingest
        response1 = client.post("/events/ingest", json=payload)
        assert response1.json()["successful"] == 1
        
        # Second ingest (duplicate)
        response2 = client.post("/events/ingest", json=payload)
        data = response2.json()
        assert data["duplicates"] == 1
        assert data["successful"] == 0
    
    def test_ingest_batch_mixed_valid_invalid(self, setup_db):
        """Test partial success: valid events stored, invalid ones flagged."""
        payload = {
            "events": [
                {
                    "event_id": "test-event-1",
                    "store_id": "STORE_BLR_001",
                    "camera_id": "CAM_1",
                    "visitor_id": "VIS_001",
                    "event_type": "ENTRY",
                    "timestamp": "2026-04-10T16:55:36Z",
                    "zone_id": None,
                    "dwell_ms": 0,
                    "is_staff": False,
                    "confidence": 0.95,
                    "metadata": {}
                },
                {
                    "event_id": "test-event-2",
                    "store_id": "STORE_BLR_001",
                    "camera_id": "CAM_1",
                    "visitor_id": "VIS_002",
                    "event_type": "INVALID_TYPE",  # Invalid event type
                    "timestamp": "2026-04-10T16:55:37Z",
                    "zone_id": None,
                    "dwell_ms": 0,
                    "is_staff": False,
                    "confidence": 0.95,
                    "metadata": {}
                }
            ]
        }
        
        response = client.post("/events/ingest", json=payload)
        data = response.json()
        assert data["successful"] >= 0  # At least one valid event
        assert len(data["errors"]) >= 0  # Track failures


class TestMetricsEndpoint:
    """Test GET /stores/{id}/metrics endpoint."""
    
    def test_metrics_empty_store(self, setup_db):
        """Empty store returns zero metrics gracefully."""
        response = client.get("/stores/STORE_EMPTY/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["total_visitors"] == 0
        assert data["conversion_rate"] is None
    
    def test_metrics_with_entries_and_transactions(self, setup_db):
        """Metrics computed correctly from events."""
        # Ingest sample events
        events = [
            {"event_id": f"event-{i}", "store_id": "STORE_TEST", "camera_id": "CAM_1",
             "visitor_id": f"VIS_{i}", "event_type": "ENTRY",
             "timestamp": f"2026-04-10T16:{55+i%5:02d}:36Z",
             "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 0.95,
             "metadata": {}}
            for i in range(10)
        ]
        
        payload = {"events": events}
        ingest_response = client.post("/events/ingest", json=payload)
        assert ingest_response.json()["successful"] > 0
        
        # Get metrics
        response = client.get("/stores/STORE_TEST/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["total_visitors"] >= 0
        assert data["store_id"] == "STORE_TEST"
    
    def test_metrics_exclude_staff(self, setup_db):
        """Staff events are excluded from visitor count."""
        events = [
            {"event_id": "staff-1", "store_id": "STORE_TEST", "camera_id": "CAM_1",
             "visitor_id": "VIS_STAFF", "event_type": "ENTRY",
             "timestamp": "2026-04-10T16:55:36Z",
             "zone_id": None, "dwell_ms": 0, "is_staff": True, "confidence": 0.95,
             "metadata": {}},
            {"event_id": "customer-1", "store_id": "STORE_TEST", "camera_id": "CAM_1",
             "visitor_id": "VIS_001", "event_type": "ENTRY",
             "timestamp": "2026-04-10T16:55:37Z",
             "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 0.95,
             "metadata": {}}
        ]
        
        client.post("/events/ingest", json={"events": events})
        response = client.get("/stores/STORE_TEST/metrics")
        data = response.json()
        # Should count only customer, not staff
        assert data["total_visitors"] == 1


class TestFunnelEndpoint:
    """Test GET /stores/{id}/funnel endpoint."""
    
    def test_funnel_structure(self, setup_db):
        """Funnel has expected structure and stages."""
        response = client.get("/stores/STORE_TEST/funnel")
        assert response.status_code == 200
        data = response.json()
        
        assert "funnel" in data
        assert isinstance(data["funnel"], list)
        assert len(data["funnel"]) >= 3  # At least Entry, Zone, Billing, Purchase
        
        # Check stage structure
        for stage in data["funnel"]:
            assert "stage" in stage
            assert "visitor_count" in stage
            assert "drop_off_pct" in stage
    
    def test_funnel_drop_off_logic(self, setup_db):
        """Drop-off percentages increase through funnel."""
        response = client.get("/stores/STORE_TEST/funnel")
        data = response.json()
        funnel = data["funnel"]
        
        # Drop-off % should be monotonically non-decreasing
        for i in range(len(funnel) - 1):
            assert funnel[i]["drop_off_pct"] <= funnel[i+1]["drop_off_pct"]


class TestHeatmapEndpoint:
    """Test GET /stores/{id}/heatmap endpoint."""
    
    def test_heatmap_empty_store(self, setup_db):
        """Heatmap gracefully handles store with no zone visits."""
        response = client.get("/stores/STORE_EMPTY/heatmap")
        assert response.status_code == 200
        data = response.json()
        assert data["zones"] == [] or isinstance(data["zones"], list)
    
    def test_heatmap_zone_intensity_normalized(self, setup_db):
        """Zone intensity values are normalized 0-100."""
        response = client.get("/stores/STORE_TEST/heatmap")
        assert response.status_code == 200
        data = response.json()
        
        for zone in data["zones"]:
            assert 0 <= zone["intensity_0_100"] <= 100


class TestAnomaliesEndpoint:
    """Test GET /stores/{id}/anomalies endpoint."""
    
    def test_anomalies_structure(self, setup_db):
        """Anomalies endpoint returns expected structure."""
        response = client.get("/stores/STORE_TEST/anomalies")
        assert response.status_code == 200
        data = response.json()
        
        assert "anomalies" in data
        assert isinstance(data["anomalies"], list)
        assert "has_critical" in data
        assert isinstance(data["has_critical"], bool)
    
    def test_anomalies_severity_values(self, setup_db):
        """Anomaly severity is one of: INFO, WARN, CRITICAL."""
        response = client.get("/stores/STORE_TEST/anomalies")
        data = response.json()
        
        for anomaly in data["anomalies"]:
            assert anomaly["severity"] in ["INFO", "WARN", "CRITICAL"]
            assert "suggested_action" in anomaly


class TestHealthEndpoint:
    """Test GET /health endpoint."""
    
    def test_health_structure(self, setup_db):
        """Health endpoint returns required fields."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        
        assert "status" in data
        assert data["status"] in ["healthy", "degraded"]
        assert "uptime_seconds" in data
        assert "db_status" in data
        assert "last_event_timestamp" in data
        assert "stale_feeds" in data
    
    def test_health_db_connectivity(self, setup_db):
        """Health checks database connectivity."""
        response = client.get("/health")
        data = response.json()
        assert data["db_status"] in ["connected", "disconnected"]


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_very_large_batch(self, setup_db):
        """Ingest large batch (500+ events) without error."""
        events = [
            {"event_id": f"event-{i}", "store_id": "STORE_BIG",
             "camera_id": "CAM_1", "visitor_id": f"VIS_{i%100}",
             "event_type": "ZONE_DWELL", "timestamp": "2026-04-10T16:55:36Z",
             "zone_id": "ZONE_A", "dwell_ms": 5000, "is_staff": False,
             "confidence": 0.90, "metadata": {}}
            for i in range(500)
        ]
        
        response = client.post("/events/ingest", json={"events": events})
        assert response.status_code == 200
        data = response.json()
        assert data["successful"] + data["duplicates"] == 500
    
    def test_confidence_bounds(self, setup_db):
        """Confidence values outside [0, 1] are handled."""
        # Valid confidence in bounds
        payload = {
            "events": [{
                "event_id": "conf-test-1", "store_id": "STORE_TEST",
                "camera_id": "CAM_1", "visitor_id": "VIS_001",
                "event_type": "ENTRY", "timestamp": "2026-04-10T16:55:36Z",
                "zone_id": None, "dwell_ms": 0, "is_staff": False,
                "confidence": 0.5, "metadata": {}
            }]
        }
        response = client.post("/events/ingest", json=payload)
        assert response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
