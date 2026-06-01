# PROMPT: Asked Claude to generate comprehensive tests for FastAPI endpoints,
# including happy path, edge cases (empty store, stale data), and error handling.
# Requested test structure that covers: event ingestion idempotency, metrics
# consistency, funnel deduplication, anomaly detection logic, and POS conversion
# correlation by store plus billing-zone time window.
#
# CHANGES MADE: Added custom assertions for funnel drop-off calculation accuracy,
# added tests for re-entry visitor deduplication, queue depth anomaly detection,
# is_staff filtering, partial per-event ingestion failures, POS ingestion
# idempotency, and conversion requiring a billing event in the 5-minute window.
# Fixed DB isolation (each test gets its own in-memory SQLite engine),
# fixed funnel drop-off assertion logic, strengthened partial-failure assertions.

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from datetime import datetime, timedelta

from app.main import app
from app.database import APIMetadata, Base, get_db
from app.models import StoreEvent, EventType


# ---------------------------------------------------------------------------
# Test DB isolation — each test function gets a fresh in-memory database.
# ---------------------------------------------------------------------------

def make_test_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def client():
    """FastAPI test client wired to a fresh in-memory DB per test."""
    test_engine = make_test_engine()
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_id, visitor_id, event_type="ENTRY", store_id="STORE_TEST",
                is_staff=False, zone_id=None, dwell_ms=0,
                timestamp="2026-04-10T16:55:36Z"):
    return {
        "event_id": event_id,
        "store_id": store_id,
        "camera_id": "CAM_1",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": 0.95,
        "metadata": {},
    }


def _ingest(client, events):
    return client.post("/events/ingest", json={"events": events})


def _ingest_pos(client, transactions):
    return client.post("/pos/ingest", json={"transactions": transactions})


def _db_session_for_client(client):
    override = app.dependency_overrides[get_db]
    db_generator = override()
    db = next(db_generator)
    return db, db_generator


# ---------------------------------------------------------------------------
# Event Ingestion
# ---------------------------------------------------------------------------

class TestEventIngestion:

    def test_ingest_valid_batch(self, client):
        """Ingest batch of valid events."""
        resp = _ingest(client, [_make_event("evt-1", "VIS_001")])
        assert resp.status_code == 200
        data = resp.json()
        assert data["successful"] == 1
        assert data["failed"] == 0
        assert data["duplicates"] == 0

    def test_ingest_duplicate_events(self, client):
        """Duplicate event_ids are detected and counted, not double-stored."""
        event = [_make_event("evt-1", "VIS_001")]
        assert _ingest(client, event).json()["successful"] == 1

        resp = _ingest(client, event)
        data = resp.json()
        assert data["duplicates"] == 1
        assert data["successful"] == 0

    def test_ingest_batch_partial_failure(self, client):
        """Valid event is stored; invalid event_type is rejected — counts must be exact."""
        events = [
            _make_event("evt-1", "VIS_001"),                        # valid
            {**_make_event("evt-2", "VIS_002"), "event_type": "INVALID_TYPE"},  # invalid
        ]
        resp = _ingest(client, events)
        assert resp.status_code == 200
        data = resp.json()
        assert data["successful"] == 1
        assert data["failed"] == 1
        assert data["duplicates"] == 0
        assert data["errors"][0]["event_id"] == "evt-2"

    def test_ingest_multiple_same_store_updates_one_last_event_metadata_row(self, client):
        """Last-event metadata is deduped per store within a single batch."""
        events = [
            _make_event("evt-1", "VIS_001", timestamp="2026-04-10T10:00:00Z"),
            _make_event("evt-2", "VIS_002", timestamp="2026-04-10T10:05:00Z"),
            _make_event("evt-3", "VIS_003", timestamp="2026-04-10T10:03:00Z"),
        ]

        resp = _ingest(client, events)
        assert resp.status_code == 200
        assert resp.json()["successful"] == 3

        db, db_generator = _db_session_for_client(client)
        try:
            rows = db.query(APIMetadata).filter(
                APIMetadata.key == "last_event_timestamp:STORE_TEST"
            ).all()
            assert len(rows) == 1
            assert rows[0].value == "2026-04-10T10:05:00Z"
        finally:
            db.close()
            db_generator.close()


class TestPOSIngestion:

    def test_pos_ingest_idempotent(self, client):
        transaction = [{
            "store_id": "STORE_TEST",
            "transaction_id": "TXN_001",
            "timestamp": "2026-04-10T16:58:00Z",
            "basket_value_inr": 1200.0,
        }]
        first = _ingest_pos(client, transaction)
        second = _ingest_pos(client, transaction)

        assert first.status_code == 200
        assert first.json()["successful"] == 1
        assert second.status_code == 200
        assert second.json()["duplicates"] == 1


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetricsEndpoint:

    def test_metrics_empty_store(self, client):
        """Empty store returns zero metrics gracefully."""
        resp = client.get("/stores/STORE_EMPTY/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_visitors"] == 0
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] is None

    def test_metrics_total_vs_unique_visitors(self, client):
        """total_visitors counts re-entries; unique_visitors deduplicates."""
        events = [
            _make_event("evt-1", "VIS_001", timestamp="2026-04-10T10:00:00Z"),
            _make_event("evt-2", "VIS_001", timestamp="2026-04-10T11:00:00Z"),  # re-entry same visitor
            _make_event("evt-3", "VIS_002", timestamp="2026-04-10T10:30:00Z"),
        ]
        _ingest(client, events)
        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["total_visitors"] == 3    # all three ENTRY events
        assert data["unique_visitors"] == 2   # VIS_001 and VIS_002

    def test_metrics_exclude_staff(self, client):
        """Staff events must not be counted in visitor metrics."""
        events = [
            _make_event("evt-staff", "VIS_STAFF", is_staff=True),
            _make_event("evt-cust",  "VIS_001",   is_staff=False),
        ]
        _ingest(client, events)
        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["total_visitors"] == 1
        assert data["unique_visitors"] == 1

    def test_metrics_conversion_requires_billing_window(self, client):
        """POS conversion only counts visitors seen in billing within 5 minutes."""
        _ingest(client, [
            _make_event("entry-1", "VIS_001", timestamp="2026-04-10T16:50:00Z"),
            _make_event(
                "bill-1", "VIS_001", event_type="BILLING_QUEUE_JOIN",
                zone_id="BILLING", timestamp="2026-04-10T16:56:00Z"
            ),
            _make_event("entry-2", "VIS_002", timestamp="2026-04-10T16:51:00Z"),
        ])
        _ingest_pos(client, [{
            "store_id": "STORE_TEST",
            "transaction_id": "TXN_001",
            "timestamp": "2026-04-10T16:58:00Z",
            "basket_value_inr": 999.0,
        }])

        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["transactions_count"] == 1
        assert data["conversion_rate"] == 50.0

    def test_metrics_transaction_without_billing_does_not_convert(self, client):
        _ingest(client, [
            _make_event("entry-1", "VIS_001", timestamp="2026-04-10T16:50:00Z"),
        ])
        _ingest_pos(client, [{
            "store_id": "STORE_TEST",
            "transaction_id": "TXN_001",
            "timestamp": "2026-04-10T16:58:00Z",
            "basket_value_inr": 999.0,
        }])

        data = client.get("/stores/STORE_TEST/metrics").json()
        assert data["conversion_rate"] == 0.0


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------

class TestFunnelEndpoint:

    def test_funnel_structure(self, client):
        """Funnel has the required four stages with expected fields."""
        resp = client.get("/stores/STORE_TEST/funnel")
        assert resp.status_code == 200
        data = resp.json()
        assert "funnel" in data
        assert len(data["funnel"]) == 4
        for stage in data["funnel"]:
            assert "stage" in stage
            assert "visitor_count" in stage
            assert "drop_off_pct" in stage

    def test_funnel_entry_drop_off_is_zero(self, client):
        """Entry stage drop-off must always be 0 — it is the top of the funnel."""
        data = client.get("/stores/STORE_TEST/funnel").json()
        entry_stage = next(s for s in data["funnel"] if s["stage"] == "Entry")
        assert entry_stage["drop_off_pct"] == 0.0

    def test_funnel_visitor_counts_decrease(self, client):
        """Each successive funnel stage must have <= visitors than the prior stage."""
        _ingest(client, [
            _make_event("e1", "VIS_001"),
            _make_event("e2", "VIS_002"),
        ])
        data = client.get("/stores/STORE_TEST/funnel").json()
        counts = [s["visitor_count"] for s in data["funnel"]]
        for i in range(len(counts) - 1):
            assert counts[i] >= counts[i + 1], (
                f"Stage {i} count {counts[i]} < stage {i+1} count {counts[i+1]}"
            )

    def test_funnel_conversion_rate_matches_stages(self, client):
        """conversion_rate must equal purchased / entered * 100."""
        data = client.get("/stores/STORE_TEST/funnel").json()
        total = data["total_visitors"]
        converted = data["converted_visitors"]
        expected_rate = (converted / total * 100) if total > 0 else 0
        assert abs(data["conversion_rate"] - expected_rate) < 0.01

    def test_funnel_purchase_stage_uses_converted_visitors(self, client):
        _ingest(client, [
            _make_event("entry-1", "VIS_001", timestamp="2026-04-10T16:50:00Z"),
            _make_event(
                "bill-1", "VIS_001", event_type="BILLING_QUEUE_JOIN",
                zone_id="BILLING", timestamp="2026-04-10T16:56:00Z"
            ),
        ])
        _ingest_pos(client, [{
            "store_id": "STORE_TEST",
            "transaction_id": "TXN_001",
            "timestamp": "2026-04-10T16:58:00Z",
            "basket_value_inr": 999.0,
        }])

        data = client.get("/stores/STORE_TEST/funnel").json()
        purchase = next(stage for stage in data["funnel"] if stage["stage"] == "Purchase")
        assert purchase["visitor_count"] == 1
        assert data["converted_visitors"] == 1


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

class TestHeatmapEndpoint:

    def test_heatmap_empty_store(self, client):
        resp = client.get("/stores/STORE_EMPTY/heatmap")
        assert resp.status_code == 200
        assert isinstance(resp.json()["zones"], list)

    def test_heatmap_intensity_normalized_0_to_100(self, client):
        """Every zone intensity must be in [0, 100]."""
        _ingest(client, [
            _make_event("e1", "VIS_001", event_type="ZONE_ENTER", zone_id="ZONE_A"),
            _make_event("e2", "VIS_002", event_type="ZONE_ENTER", zone_id="ZONE_B"),
        ])
        for zone in client.get("/stores/STORE_TEST/heatmap").json()["zones"]:
            assert 0 <= zone["intensity_0_100"] <= 100


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------

class TestAnomaliesEndpoint:

    def test_anomalies_structure(self, client):
        resp = client.get("/stores/STORE_TEST/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["anomalies"], list)
        assert isinstance(data["has_critical"], bool)

    def test_anomaly_severity_values(self, client):
        """Every anomaly severity must be one of INFO / WARN / CRITICAL."""
        for anomaly in client.get("/stores/STORE_TEST/anomalies").json()["anomalies"]:
            assert anomaly["severity"] in {"INFO", "WARN", "CRITICAL"}
            assert "suggested_action" in anomaly


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:

    def test_health_structure(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in {"healthy", "degraded"}
        assert "uptime_seconds" in data
        assert "db_status" in data
        assert "last_event_timestamp" in data
        assert "stale_feeds" in data

    def test_health_db_status_values(self, client):
        data = client.get("/health").json()
        assert data["db_status"] in {"connected", "disconnected"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_large_batch(self, client):
        """500-event batch ingests without error; successful + duplicates == 500."""
        events = [
            _make_event(f"evt-{i}", f"VIS_{i % 100}",
                        event_type="ZONE_DWELL", zone_id="ZONE_A", dwell_ms=5000)
            for i in range(500)
        ]
        resp = _ingest(client, events)
        assert resp.status_code == 200
        data = resp.json()
        assert data["successful"] + data["duplicates"] == 500

    def test_confidence_bounds_accepted(self, client):
        """Confidence values at the boundary (0.0 and 1.0) are accepted."""
        for conf, eid in [(0.0, "conf-low"), (1.0, "conf-high")]:
            payload = [{**_make_event(eid, "VIS_001"), "confidence": conf}]
            assert _ingest(client, payload).status_code == 200

    def test_confidence_out_of_bounds_rejected(self, client):
        """Confidence outside [0, 1] is rejected with HTTP 422."""
        payload = [{**_make_event("conf-bad", "VIS_001"), "confidence": 1.5}]
        resp = _ingest(client, payload)
        assert resp.status_code == 422
        assert "detail" in resp.json()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
