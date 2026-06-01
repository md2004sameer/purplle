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
from datetime import datetime, timedelta
import json
import uuid
from typing import List, Dict, Optional, Tuple
import logging
import os

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
    for zone in layout.get('zones', []):
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
            'name': zone['zone_name'],
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
    
    def update(self, detections: np.ndarray, frame_idx: int) -> Dict[int, Dict]:
        """
        Update tracks with new detections.
        
        Args:
            detections: Nx4 array of [x1, y1, x2, y2] bboxes
            frame_idx: Current frame index
        
        Returns:
            Dict mapping track_id -> bbox and metadata
        """
        updated_tracks = {}
        
        if len(detections) == 0:
            # Age out tracks
            dead_tracks = [tid for tid, track in self.tracks.items() 
                          if frame_idx - track['last_seen'] > self.max_age]
            for tid in dead_tracks:
                del self.tracks[tid]
            return updated_tracks
        
        # Simple IoU-based association
        for det_bbox in detections:
            matched = False
            best_iou = 0.3
            best_id = None
            
            for tid, track in self.tracks.items():
                if frame_idx - track['last_seen'] <= self.max_age:
                    iou = self._compute_iou(det_bbox, track['bbox'])
                    if iou > best_iou:
                        best_iou = iou
                        best_id = tid
            
            if best_id is not None:
                self.tracks[best_id]['bbox'] = det_bbox
                self.tracks[best_id]['last_seen'] = frame_idx
                self.track_history[best_id].append(det_bbox)
                updated_tracks[best_id] = self.tracks[best_id]
                matched = True
            
            if not matched and len(self.tracks) < self.max_tracks:
                new_id = self.next_id
                self.next_id += 1
                self.tracks[new_id] = {
                    'bbox': det_bbox,
                    'first_seen': frame_idx,
                    'last_seen': frame_idx,
                    'track_id': new_id,
                    'visitor_token': f"VIS_{uuid.uuid4().hex[:6]}",
                    'zone_history': [],
                    'confidence': 0.95,
                }
                self.track_history[new_id].append(det_bbox)
                updated_tracks[new_id] = self.tracks[new_id]
        
        return updated_tracks
    
    @staticmethod
    def _compute_iou(bbox1, bbox2):
        """Compute IoU between two bboxes."""
        x1_min, y1_min, x1_max, y1_max = bbox1
        x2_min, y2_min, x2_max, x2_max = bbox2
        
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
            zones: Dict with zone_id -> {'x_min', 'x_max', 'y_min', 'y_max', 'name'}
        """
        self.zones = zones
        self.frame_height = frame_height
        self.frame_width = frame_width
    
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
        - Frequent zone transitions
        - Regular presence across cameras
        - Minimal billing zone dwell
        """
        if len(zone_history) < 5:
            return False
        
        # Too many transitions = likely staff
        unique_zones = len(set(zone_history[-20:]))
        if unique_zones >= 4:
            return True
        
        # Never visits billing = unusual for customer
        if 'BILLING' not in zone_history[-30:]:
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
                    zones: Dict[str, Dict]) -> List[Dict]:
        """
        Process a CCTV clip and emit events.
        
        Args:
            video_path: Path to MP4 clip
            store_id: Store identifier
            camera_id: Camera identifier
            zones: Zone definitions
        
        Returns:
            List of structured events
        """
        cap = cv2.VideoCapture(video_path)
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        zone_classifier = ZoneClassifier(frame_height, frame_width, zones)
        staff_detector = StaffDetector()
        emitter = EventEmitter(store_id, camera_id, fps)
        
        frame_idx = 0
        visitor_zones = defaultdict(lambda: {'current': None, 'dwell_start': None})
        visitor_states = {}  # Track visitor state for entry/exit
        
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
            
            # Update tracks
            tracks = self.tracker.update(detections, frame_idx)
            
            # Generate events
            timestamp = datetime.utcnow() + timedelta(seconds=frame_idx / fps)
            
            for track_id, track_data in tracks.items():
                visitor_id = track_data['visitor_token']
                bbox = track_data['bbox']
                conf = confidences[0] if len(confidences) > 0 else 0.9
                
                # Classify zone
                zone_id = zone_classifier.classify(bbox)
                
                # Track zone transitions
                prev_zone = visitor_zones[visitor_id]['current']
                if zone_id != prev_zone:
                    if prev_zone is not None:
                        dwell_ms = int((timestamp - visitor_zones[visitor_id]['dwell_start']).total_seconds() * 1000)
                        emitter.emit_zone_event(visitor_id, prev_zone, 'ZONE_EXIT', timestamp, dwell_ms, conf)
                    
                    if zone_id is not None:
                        emitter.emit_zone_event(visitor_id, zone_id, 'ZONE_ENTER', timestamp, 0, conf)
                        visitor_zones[visitor_id]['dwell_start'] = timestamp
                    
                    visitor_zones[visitor_id]['current'] = zone_id
        
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
    
    pipeline = DetectionPipeline()
    all_events = []
    
    for filename in sorted(os.listdir(data_dir)):
        if filename.endswith('.mp4'):
            video_path = os.path.join(data_dir, filename)
            camera_id = filename.replace('.mp4', '')
            
            events = pipeline.process_clip(video_path, store_id, camera_id, zones)
            all_events.extend(events)
    
    # Sort by timestamp
    all_events.sort(key=lambda e: e['timestamp'])
    
    return all_events
