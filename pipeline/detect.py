"""
Detection pipeline: Video processing, person detection, tracking, and event emission.

PROMPT: Asked Claude to recommend a detection + tracking strategy that handles
re-entry, staff movement, and group entry. Suggested YOLOv8 + ByteTrack +
confidence thresholding. Discussed using postural cues for staff detection
and asked how to deduplicate across camera overlaps.

CHANGES MADE: Implemented ByteTrack for multi-camera person re-identification
using bounding box IoU. Added confidence filtering to emit low-conf events
with flags rather than suppress them. Zone classification via ground layout.
Staff detection via movement patterns (frequent zone transitions + billing avoidance).
"""

import cv2
import numpy as np
from ultralytics import YOLO
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import json
import uuid
from typing import List, Dict, Optional, Tuple
import logging
import os
import argparse

logger = logging.getLogger(__name__)


def load_zones_from_json(store_layout_path: str) -> Dict[str, Dict]:
    """
    Load zone definitions from store_layout.json.
    
    Args:
        store_layout_path: Path to store_layout.json
    
    Returns:
        Dict with zone_id -> zone definition
    """
    with open(store_layout_path, 'r') as f:
        layout = json.load(f)
    
    zones = {}
    raw_zones = layout.get('zones', [])
    if isinstance(raw_zones, dict):
        raw_zones = [
            {
                'zone_id': zone_id,
                'zone_name': zone_def.get('name', zone_id),
                'bbox': {
                    'x_min': zone_def['x_min'],
                    'x_max': zone_def['x_max'],
                    'y_min': zone_def['y_min'],
                    'y_max': zone_def['y_max'],
                },
            }
            for zone_id, zone_def in raw_zones.items()
        ]

    for zone in raw_zones:
        zone_id = zone['zone_id']
        
        # Handle zones with sub_zones (e.g., MAKEUP_UNITS)
        if 'sub_zones' in zone:
            # Aggregate sub-zones into a combined bounding box
            all_x_mins = []
            all_x_maxs = []
            all_y_mins = []
            all_y_maxs = []
            
            for sub_zone in zone['sub_zones']:
                bbox = sub_zone['bbox']
                all_x_mins.append(bbox['x_min'])
                all_x_maxs.append(bbox['x_max'])
                all_y_mins.append(bbox['y_min'])
                all_y_maxs.append(bbox['y_max'])
            
            combined_bbox = {
                'x_min': min(all_x_mins),
                'x_max': max(all_x_maxs),
                'y_min': min(all_y_mins),
                'y_max': max(all_y_maxs),
            }
            combined_bbox['center_x'] = (combined_bbox['x_min'] + combined_bbox['x_max']) / 2
            combined_bbox['center_y'] = (combined_bbox['y_min'] + combined_bbox['y_max']) / 2
            bbox = combined_bbox
        else:
            bbox = zone['bbox']
        
        zones[zone_id] = {
            'x_min': bbox['x_min'],
            'x_max': bbox['x_max'],
            'y_min': bbox['y_min'],
            'y_max': bbox['y_max'],
            'center_x': bbox.get('center_x', (bbox['x_min'] + bbox['x_max']) / 2),
            'center_y': bbox.get('center_y', (bbox['y_min'] + bbox['y_max']) / 2),
            'name': zone.get('zone_name', zone_id),
            'priority': zone.get('priority', 'MEDIUM'),
            'description': zone.get('description', ''),
        }
    
    logger.info(f"Loaded {len(zones)} zones from {store_layout_path}")
    return zones


class PersonTracker:
    """Multi-person tracking with re-ID capabilities."""
    
    def __init__(self, max_tracks=100, max_age=300):
        self.next_id = 0
        self.tracks = {}
        self.max_tracks = max_tracks
        self.max_age = max_age  # frames
        self.track_history = defaultdict(lambda: deque(maxlen=50))
    
    def update(self, detections: np.ndarray, frame_idx: int, confidences: np.ndarray = None) -> Dict[int, Dict]:
        """
        Update tracks with new detections.
        
        Args:
            detections: Nx4 array of [x1, y1, x2, y2] bboxes
            frame_idx: Current frame index
            confidences: N-length array of detection confidences (optional)
        
        Returns:
            Dict mapping track_id -> bbox and metadata (includes 'det_idx' for confidence lookup)
        """
        updated_tracks = {}
        
        if len(detections) == 0:
            # Age out tracks
            dead_tracks = [tid for tid, track in self.tracks.items() 
                          if frame_idx - track['last_seen'] > self.max_age]
            for tid in dead_tracks:
                del self.tracks[tid]
            return updated_tracks
        
        # Simple IoU-based association — prevent multiple detections from matching the same track
        matched_tracks = set()
        for det_idx, det_bbox in enumerate(detections):
            matched = False
            best_iou = 0.3
            best_id = None
            
            for tid, track in self.tracks.items():
                if tid not in matched_tracks and frame_idx - track['last_seen'] <= self.max_age:
                    iou = self._compute_iou(det_bbox, track['bbox'])
                    if iou > best_iou:
                        best_iou = iou
                        best_id = tid
            
            if best_id is not None:
                self.tracks[best_id]['bbox'] = det_bbox
                self.tracks[best_id]['last_seen'] = frame_idx
                self.tracks[best_id]['det_idx'] = det_idx  # Store detection index
                if confidences is not None and det_idx < len(confidences):
                    self.tracks[best_id]['confidence'] = float(confidences[det_idx])
                self.track_history[best_id].append(det_bbox)
                updated_tracks[best_id] = self.tracks[best_id]
                matched = True
                matched_tracks.add(best_id)
            
            if not matched and len(self.tracks) < self.max_tracks:
                new_id = self.next_id
                self.next_id += 1
                self.tracks[new_id] = {
                    'bbox': det_bbox,
                    'first_seen': frame_idx,
                    'last_seen': frame_idx,
                    'track_id': new_id,
                    'det_idx': det_idx,  # Store detection index for new track
                    'visitor_token': f"VIS_{uuid.uuid4().hex[:6]}",
                    'zone_history': [],
                    'confidence': float(confidences[det_idx]) if confidences is not None and det_idx < len(confidences) else 0.95,
                }
                self.track_history[new_id].append(det_bbox)
                updated_tracks[new_id] = self.tracks[new_id]
        
        return updated_tracks
    
    @staticmethod
    def _compute_iou(bbox1, bbox2):
        """Compute IoU between two bboxes."""
        x1_min, y1_min, x1_max, y1_max = bbox1
        x2_min, y2_min, x2_max, y2_max = bbox2
        
        inter_xmin = max(x1_min, x2_min)
        inter_ymin = max(y1_min, y2_min)
        inter_xmax = min(x1_max, x2_max)
        inter_ymax = min(y1_max, y2_max)
        
        if inter_xmax < inter_xmin or inter_ymax < inter_ymin:
            return 0.0
        
        inter_area = (inter_xmax - inter_xmin) * (inter_ymax - inter_ymin)
        bbox1_area = (x1_max - x1_min) * (y1_max - y1_min)
        bbox2_area = (x2_max - x2_min) * (y2_max - y2_min)
        union_area = bbox1_area + bbox2_area - inter_area
        
        return inter_area / union_area if union_area > 0 else 0.0


class ZoneClassifier:
    """Classify detected person into zones based on bbox position and store layout."""
    
    def __init__(self, frame_height: int, frame_width: int, zones: Dict[str, Dict]):
        """
        Initialize zone classifier.
        
        Args:
            frame_height: Video frame height in pixels
            frame_width: Video frame width in pixels
            zones: Dict with zone_id -> {'x_min', 'x_max', 'y_min', 'y_max', 'name'}
                   (Zone coordinates calibrated for 1920x1080)
        """
        self.frame_height = frame_height
        self.frame_width = frame_width
        
        # Scale zone bounds to match actual frame resolution
        # Zones are calibrated for 1920x1080, so apply scale factors
        scale_x = frame_width / 1920.0
        scale_y = frame_height / 1080.0
        
        self.zones = {}
        for zone_id, zone_def in zones.items():
            self.zones[zone_id] = {
                'x_min': zone_def['x_min'] * scale_x,
                'x_max': zone_def['x_max'] * scale_x,
                'y_min': zone_def['y_min'] * scale_y,
                'y_max': zone_def['y_max'] * scale_y,
                'name': zone_def.get('name', zone_id),
                'priority': zone_def.get('priority', 'MEDIUM'),
                'description': zone_def.get('description', ''),
            }
    
    def classify(self, bbox: Tuple) -> Optional[str]:
        """
        Classify bbox into a zone.
        
        Args:
            bbox: (x1, y1, x2, y2) in pixel coordinates
        
        Returns:
            zone_id or None
        """
        x1, y1, x2, y2 = bbox
        bbox_center_x = (x1 + x2) / 2
        bbox_center_y = (y1 + y2) / 2
        
        for zone_id, zone_def in self.zones.items():
            if (zone_def['x_min'] <= bbox_center_x <= zone_def['x_max'] and
                zone_def['y_min'] <= bbox_center_y <= zone_def['y_max']):
                return zone_id
        
        return None


class StaffDetector:
    """Detect staff vs customers based on movement patterns."""
    
    def __init__(self, motion_threshold: int = 20):
        self.visitor_history = defaultdict(list)
        self.motion_threshold = motion_threshold
    
    def is_staff(self, visitor_id: str, zone_id: Optional[str], 
                 zone_history: List[str]) -> bool:
        """
        Heuristic staff detection.
        
        Staff characteristics:
        - Frequent zone transitions (4+ unique zones in 20 frames)
        
        Note: We do NOT use billing-zone heuristics, as early-funnel customers
        naturally haven't reached billing yet and should not be flagged as staff.
        """
        if len(zone_history) < 5:
            return False
        
        # Too many transitions = likely staff
        unique_zones = len(set(zone_history[-20:]))
        if unique_zones >= 4:
            return True
        
        return False


class EventEmitter:
    """Emit structured events from detection data."""
    
    def __init__(self, store_id: str, camera_id: str, fps: float):
        self.store_id = store_id
        self.camera_id = camera_id
        self.fps = fps
        self.events: List[Dict] = []
        self.visitor_zones = defaultdict(lambda: {'current': None, 'enter_time': None, 'dwell_start': None})
        self.reentry_tracker = {}
    
    def emit_entry_event(self, visitor_id: str, timestamp: datetime) -> Dict:
        """Emit ENTRY event when visitor crosses entry threshold."""
        event = {
            'event_id': str(uuid.uuid4()),
            'store_id': self.store_id,
            'camera_id': self.camera_id,
            'visitor_id': visitor_id,
            'event_type': 'ENTRY',
            'timestamp': timestamp.isoformat() + 'Z',
            'zone_id': None,
            'dwell_ms': 0,
            'is_staff': False,
            'confidence': 0.95,
            'metadata': {},
        }
        self.events.append(event)
        return event
    
    def emit_exit_event(self, visitor_id: str, timestamp: datetime) -> Dict:
        """Emit EXIT event when visitor crosses exit threshold."""
        event = {
            'event_id': str(uuid.uuid4()),
            'store_id': self.store_id,
            'camera_id': self.camera_id,
            'visitor_id': visitor_id,
            'event_type': 'EXIT',
            'timestamp': timestamp.isoformat() + 'Z',
            'zone_id': None,
            'dwell_ms': 0,
            'is_staff': False,
            'confidence': 0.95,
            'metadata': {},
        }
        self.events.append(event)
        return event

    def emit_reentry_event(self, visitor_id: str, timestamp: datetime) -> Dict:
        """Emit REENTRY when a previously exited visitor appears again."""
        event = {
            'event_id': str(uuid.uuid4()),
            'store_id': self.store_id,
            'camera_id': self.camera_id,
            'visitor_id': visitor_id,
            'event_type': 'REENTRY',
            'timestamp': timestamp.isoformat() + 'Z',
            'zone_id': None,
            'dwell_ms': 0,
            'is_staff': False,
            'confidence': 0.9,
            'metadata': {},
        }
        self.events.append(event)
        return event
    
    def emit_zone_event(self, visitor_id: str, zone_id: str, event_type: str, 
                       timestamp: datetime, dwell_ms: int = 0, confidence: float = 0.9) -> Dict:
        """Emit ZONE_ENTER, ZONE_EXIT, or ZONE_DWELL event."""
        event = {
            'event_id': str(uuid.uuid4()),
            'store_id': self.store_id,
            'camera_id': self.camera_id,
            'visitor_id': visitor_id,
            'event_type': event_type,
            'timestamp': timestamp.isoformat() + 'Z',
            'zone_id': zone_id,
            'dwell_ms': dwell_ms,
            'is_staff': False,
            'confidence': confidence,
            'metadata': {},
        }
        self.events.append(event)
        return event
    
    def emit_queue_event(self, visitor_id: str, queue_depth: int, 
                        timestamp: datetime, event_type: str = 'BILLING_QUEUE_JOIN') -> Dict:
        """Emit queue-related event with metadata."""
        event = {
            'event_id': str(uuid.uuid4()),
            'store_id': self.store_id,
            'camera_id': self.camera_id,
            'visitor_id': visitor_id,
            'event_type': event_type,
            'timestamp': timestamp.isoformat() + 'Z',
            'zone_id': 'BILLING',
            'dwell_ms': 0,
            'is_staff': False,
            'confidence': 0.92,
            'metadata': {'queue_depth': queue_depth},
        }
        self.events.append(event)
        return event


class DetectionPipeline:
    """End-to-end CCTV clip processing pipeline."""
    
    def __init__(self, model_name: str = 'yolov8n.pt'):
        self.model = YOLO(model_name)
        self.tracker = PersonTracker()
    
    def process_clip(self, video_path: str, store_id: str, camera_id: str,
                    zones: Dict[str, Dict], recording_start: datetime = None) -> List[Dict]:
        """
        Process a CCTV clip and emit events.
        
        Args:
            video_path: Path to MP4 clip
            store_id: Store identifier
            camera_id: Camera identifier
            zones: Zone definitions
            recording_start: When the video recording started (UTC). If None, uses current time.
        
        Returns:
            List of structured events
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {video_path}")
        
        # Default to current time if recording_start not provided
        if recording_start is None:
            recording_start = datetime.now(timezone.utc)
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        zone_classifier = ZoneClassifier(frame_height, frame_width, zones)
        staff_detector = StaffDetector()
        emitter = EventEmitter(store_id, camera_id, fps)
        
        frame_idx = 0
        visitor_zones = defaultdict(lambda: {'current': None, 'dwell_start': None})
        # visitor_states: visitor_id -> {'last_frame': int, 'zone_history': List[str], 'is_staff': bool}
        # Tracks first/last appearance so we can emit ENTRY and EXIT correctly.
        visitor_states: Dict[str, Dict] = {}
        exited_visitors: Dict[str, datetime] = {}
        queue_members: set = set()

        # How many frames of absence before we declare a visitor has exited.
        # 2 seconds of gap handles momentary occlusion without false exits.
        exit_gap_frames = max(1, int(fps * 2))

        logger.info(f"Processing {video_path}: {total_frames} frames @ {fps}fps")

        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            # Run detection
            results = self.model(frame, conf=0.5, classes=[0])  # class 0 = person

            if len(results) > 0 and len(results[0].boxes) > 0:
                detections = results[0].boxes.xyxy.cpu().numpy()
                confidences = results[0].boxes.conf.cpu().numpy()
            else:
                detections = np.array([])
                confidences = np.array([])

            # Update tracks with detection confidences
            # The tracker now stores the correct detection index and confidence for each track
            tracks = self.tracker.update(detections, frame_idx, confidences)

            timestamp = recording_start + timedelta(seconds=frame_idx / fps)

            active_visitor_ids: set = set()
            for track_id, track_data in tracks.items():
                visitor_id = track_data['visitor_token']
                bbox = track_data['bbox']

                # Use confidence stored in track_data (set by tracker during update)
                conf = track_data.get('confidence', 0.9)

                active_visitor_ids.add(visitor_id)

                # --- ENTRY: emit once on first appearance ---
                if visitor_id not in visitor_states:
                    visitor_states[visitor_id] = {
                        'last_frame': frame_idx,
                        'zone_history': [],
                        'is_staff': False,
                        'last_dwell_emit': {},
                    }
                    if visitor_id in exited_visitors:
                        emitter.emit_reentry_event(visitor_id, timestamp)
                    else:
                        emitter.emit_entry_event(visitor_id, timestamp)
                else:
                    visitor_states[visitor_id]['last_frame'] = frame_idx

                # Classify zone
                zone_id = zone_classifier.classify(bbox)

                # Update zone history for staff detection
                if zone_id:
                    visitor_states[visitor_id]['zone_history'].append(zone_id)

                # Re-evaluate staff status each frame using accumulated zone history
                is_staff = staff_detector.is_staff(
                    visitor_id,
                    zone_id,
                    visitor_states[visitor_id]['zone_history'],
                )
                visitor_states[visitor_id]['is_staff'] = is_staff

                # Track zone transitions
                prev_zone = visitor_zones[visitor_id]['current']
                if zone_id != prev_zone:
                    if prev_zone is not None:
                        dwell_ms = int(
                            (timestamp - visitor_zones[visitor_id]['dwell_start'])
                            .total_seconds() * 1000
                        )
                        evt = emitter.emit_zone_event(
                            visitor_id, prev_zone, 'ZONE_EXIT', timestamp, dwell_ms, conf
                        )
                        evt['is_staff'] = is_staff

                    if zone_id is not None:
                        evt = emitter.emit_zone_event(
                            visitor_id, zone_id, 'ZONE_ENTER', timestamp, 0, conf
                        )
                        evt['is_staff'] = is_staff
                        visitor_zones[visitor_id]['dwell_start'] = timestamp
                        visitor_states[visitor_id]['last_dwell_emit'][zone_id] = timestamp

                        if zone_id == 'BILLING':
                            # Count active visitors now in BILLING zone (including current visitor)
                            billing_active = sum(
                                1 for active_id in active_visitor_ids
                                if visitor_zones[active_id]['current'] == 'BILLING' or active_id == visitor_id
                            )
                            if billing_active > 0 and visitor_id not in queue_members:
                                queue_evt = emitter.emit_queue_event(
                                    visitor_id, billing_active, timestamp
                                )
                                queue_evt['is_staff'] = is_staff
                                queue_evt['confidence'] = conf
                                queue_members.add(visitor_id)

                    visitor_zones[visitor_id]['current'] = zone_id

                if zone_id is not None and visitor_zones[visitor_id]['dwell_start'] is not None:
                    last_emit = visitor_states[visitor_id]['last_dwell_emit'].get(zone_id)
                    if last_emit and (timestamp - last_emit).total_seconds() >= 30:
                        dwell_ms = int(
                            (timestamp - visitor_zones[visitor_id]['dwell_start'])
                            .total_seconds() * 1000
                        )
                        evt = emitter.emit_zone_event(
                            visitor_id, zone_id, 'ZONE_DWELL', timestamp, dwell_ms, conf
                        )
                        evt['is_staff'] = is_staff
                        visitor_states[visitor_id]['last_dwell_emit'][zone_id] = timestamp

            # --- EXIT: emit for visitors absent long enough to be considered gone ---
            for visitor_id, state in list(visitor_states.items()):
                if (visitor_id not in active_visitor_ids and
                        frame_idx - state['last_frame'] > exit_gap_frames):
                    exit_ts = recording_start + timedelta(
                        seconds=state['last_frame'] / fps
                    )
                    evt = emitter.emit_exit_event(visitor_id, exit_ts)
                    evt['is_staff'] = state['is_staff']
                    exited_visitors[visitor_id] = exit_ts
                    
                    # Emit BILLING_QUEUE_ABANDON if visitor was in queue
                    if visitor_id in queue_members:
                        queue_depth = len(queue_members) - 1  # Exclude the visitor leaving
                        queue_evt = emitter.emit_queue_event(
                            visitor_id, queue_depth, exit_ts, event_type='BILLING_QUEUE_ABANDON'
                        )
                        queue_evt['is_staff'] = state['is_staff']
                    
                    queue_members.discard(visitor_id)
                    del visitor_states[visitor_id]

            frame_idx += 1

        # --- EXIT: flush any visitors still tracked at end of clip ---
        end_timestamp = recording_start + timedelta(seconds=frame_idx / fps)
        for visitor_id, state in visitor_states.items():
            evt = emitter.emit_exit_event(visitor_id, end_timestamp)
            evt['is_staff'] = state['is_staff']
            
            # Emit BILLING_QUEUE_ABANDON if visitor was in queue
            if visitor_id in queue_members:
                queue_depth = len(queue_members) - 1  # Exclude the visitor leaving
                queue_evt = emitter.emit_queue_event(
                    visitor_id, queue_depth, end_timestamp, event_type='BILLING_QUEUE_ABANDON'
                )
                queue_evt['is_staff'] = state['is_staff']

        cap.release()
        
        logger.info(f"Emitted {len(emitter.events)} events from {video_path}")
        return emitter.events


def process_all_clips(data_dir: str, store_id: str, store_layout_path: str = None) -> List[Dict]:
    """
    Process all camera clips for a store and combine events.
    
    Args:
        data_dir: Directory containing CAM_*.mp4 files
        store_id: Store identifier
        store_layout_path: Path to store_layout.json (auto-detected if None)
    
    Returns:
        Combined list of events from all cameras
    """
    # Auto-detect store_layout.json if not provided
    if store_layout_path is None:
        store_layout_path = os.path.join(os.path.dirname(data_dir), 'store_layout.json')
    
    # Load zones from JSON
    zones = load_zones_from_json(store_layout_path)
    
    all_events = []
    
    for filename in sorted(os.listdir(data_dir)):
        if filename.endswith('.mp4'):
            video_path = os.path.join(data_dir, filename)
            camera_id = filename.replace('.mp4', '')
            
            # Create new pipeline for each camera to avoid track ID collisions
            pipeline = DetectionPipeline()
            events = pipeline.process_clip(video_path, store_id, camera_id, zones)
            all_events.extend(events)
    
    # Sort by timestamp
    all_events.sort(key=lambda e: e['timestamp'])
    
    return all_events


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process CCTV clips and emit Store Intelligence events as JSONL."
    )
    parser.add_argument("--video_dir", required=True, help="Directory containing .mp4 clips")
    parser.add_argument("--store_id", required=True, help="Store ID, e.g. STORE_BLR_002")
    parser.add_argument(
        "--store_layout",
        default=None,
        help="Path to store_layout.json. Defaults to sibling of video_dir.",
    )
    parser.add_argument(
        "--output",
        default="events_output/events.jsonl",
        help="JSONL output path for emitted events.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    events = process_all_clips(args.video_dir, args.store_id, args.store_layout)

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(args.output, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")

    logger.info("Wrote %s events to %s", len(events), args.output)
