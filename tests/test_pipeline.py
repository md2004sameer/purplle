# PROMPT: Asked Claude for lightweight detection-pipeline tests that avoid
# running YOLO on real video while still validating challenge-critical behavior:
# store_layout parsing, event schema fields, and queue/dwell event emission.
#
# CHANGES MADE: Kept the tests deterministic by targeting pure functions and
# EventEmitter methods instead of model inference. Added coverage for both the
# README zone format and the list-based challenge layout format.

import json
from datetime import datetime

from pipeline.detect import EventEmitter, load_zones_from_json


def test_load_zones_accepts_readme_dict_format(tmp_path):
    layout = {
        "store_id": "STORE_TEST",
        "zones": {
            "BILLING": {
                "x_min": 10,
                "y_min": 20,
                "x_max": 30,
                "y_max": 40,
                "name": "Billing Counter",
            }
        },
    }
    layout_path = tmp_path / "store_layout.json"
    layout_path.write_text(json.dumps(layout))

    zones = load_zones_from_json(str(layout_path))

    assert zones["BILLING"]["x_min"] == 10
    assert zones["BILLING"]["name"] == "Billing Counter"


def test_load_zones_accepts_challenge_list_format(tmp_path):
    layout = {
        "store_id": "STORE_TEST",
        "zones": [
            {
                "zone_id": "SKINCARE",
                "zone_name": "Skincare Zone",
                "bbox": {"x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4},
            }
        ],
    }
    layout_path = tmp_path / "store_layout.json"
    layout_path.write_text(json.dumps(layout))

    zones = load_zones_from_json(str(layout_path))

    assert zones["SKINCARE"]["x_max"] == 3
    assert zones["SKINCARE"]["name"] == "Skincare Zone"


def test_event_emitter_outputs_required_catalogue_events():
    emitter = EventEmitter("STORE_TEST", "CAM_ENTRY_01", fps=15)
    timestamp = datetime(2026, 4, 10, 16, 55, 36)

    emitter.emit_entry_event("VIS_001", timestamp)
    emitter.emit_reentry_event("VIS_001", timestamp)
    emitter.emit_zone_event("VIS_001", "SKINCARE", "ZONE_DWELL", timestamp, 30000)
    emitter.emit_queue_event("VIS_001", queue_depth=3, timestamp=timestamp)
    emitter.emit_exit_event("VIS_001", timestamp)

    event_types = [event["event_type"] for event in emitter.events]
    assert event_types == [
        "ENTRY",
        "REENTRY",
        "ZONE_DWELL",
        "BILLING_QUEUE_JOIN",
        "EXIT",
    ]
    assert emitter.events[3]["metadata"]["queue_depth"] == 3
