"""
Event ingestion script: Read events from jsonl file and POST to API.

Usage:
    python pipeline/ingest.py \
      --events_file events_output/events.jsonl \
      --api_url http://localhost:8000 \
      --batch_size 500
"""

import json
import argparse
import requests
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def ingest_events(events_file: str, api_url: str, batch_size: int = 500):
    """
    Read events from jsonl file and POST to API in batches.
    
    Args:
        events_file: Path to events.jsonl
        api_url: Base URL of Store Intelligence API
        batch_size: Number of events per batch
    """
    
    ingest_endpoint = f"{api_url}/events/ingest"
    
    try:
        with open(events_file, 'r') as f:
            batch = []
            total_ingested = 0
            total_duplicates = 0
            total_failed = 0
            
            for line_num, line in enumerate(f, 1):
                try:
                    event = json.loads(line)
                    batch.append(event)
                    
                    # Send batch when reaches size
                    if len(batch) >= batch_size:
                        result = send_batch(ingest_endpoint, batch)
                        total_ingested += result['successful']
                        total_duplicates += result['duplicates']
                        total_failed += result['failed']
                        
                        logger.info(
                            f"Batch {line_num // batch_size}: "
                            f"successful={result['successful']}, "
                            f"duplicates={result['duplicates']}, "
                            f"failed={result['failed']}"
                        )
                        batch = []
                
                except json.JSONDecodeError as e:
                    logger.warning(f"Line {line_num}: Invalid JSON - {str(e)}")
                    continue
            
            # Send remaining events
            if batch:
                result = send_batch(ingest_endpoint, batch)
                total_ingested += result['successful']
                total_duplicates += result['duplicates']
                total_failed += result['failed']
                
                logger.info(
                    f"Final batch: "
                    f"successful={result['successful']}, "
                    f"duplicates={result['duplicates']}, "
                    f"failed={result['failed']}"
                )
        
        logger.info(
            f"\nTotal Summary:\n"
            f"  Ingested: {total_ingested}\n"
            f"  Duplicates: {total_duplicates}\n"
            f"  Failed: {total_failed}\n"
            f"  Total: {total_ingested + total_duplicates + total_failed}"
        )
    
    except FileNotFoundError:
        logger.error(f"Events file not found: {events_file}")
        return False
    except Exception as e:
        logger.error(f"Ingestion error: {str(e)}")
        return False
    
    return True


def send_batch(endpoint: str, events: list) -> dict:
    """
    Send batch of events to API.
    
    Args:
        endpoint: API endpoint URL
        events: List of event dicts
    
    Returns:
        Response dict with successful, failed, duplicates counts
    """
    try:
        payload = {"events": events}
        response = requests.post(
            endpoint,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"API error {response.status_code}: {response.text}")
            return {
                'successful': 0,
                'duplicates': 0,
                'failed': len(events)
            }
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        return {
            'successful': 0,
            'duplicates': 0,
            'failed': len(events)
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest events from jsonl file into Store Intelligence API"
    )
    parser.add_argument(
        "--events_file",
        type=str,
        default="events_output/events.jsonl",
        help="Path to events.jsonl file"
    )
    parser.add_argument(
        "--api_url",
        type=str,
        default="http://localhost:8000",
        help="Base URL of Store Intelligence API"
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=500,
        help="Number of events per batch"
    )
    
    args = parser.parse_args()
    
    success = ingest_events(
        events_file=args.events_file,
        api_url=args.api_url,
        batch_size=args.batch_size
    )
    
    exit(0 if success else 1)
