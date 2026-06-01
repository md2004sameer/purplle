"""
Load pos_transactions.csv into the Store Intelligence API.

Usage:
    python pipeline/load_pos.py \
      --csv_file data/pos_transactions.csv \
      --api_url http://localhost:8000 \
      --batch_size 500
"""

import argparse
import csv
import logging

import requests


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_pos(csv_file: str, api_url: str, batch_size: int = 500) -> bool:
    endpoint = f"{api_url}/pos/ingest"
    total_successful = 0
    total_duplicates = 0
    total_failed = 0

    try:
        with open(csv_file, newline="") as f:
            reader = csv.DictReader(f)
            batch = []
            for row in reader:
                batch.append({
                    "store_id": row["store_id"],
                    "transaction_id": row["transaction_id"],
                    "timestamp": row["timestamp"],
                    "basket_value_inr": float(row["basket_value_inr"]),
                })

                if len(batch) >= batch_size:
                    result = send_batch(endpoint, batch)
                    total_successful += result["successful"]
                    total_duplicates += result["duplicates"]
                    total_failed += result["failed"]
                    batch = []

            if batch:
                result = send_batch(endpoint, batch)
                total_successful += result["successful"]
                total_duplicates += result["duplicates"]
                total_failed += result["failed"]

        logger.info(
            "POS load complete: successful=%s duplicates=%s failed=%s",
            total_successful,
            total_duplicates,
            total_failed,
        )
        return total_failed == 0
    except Exception as e:
        logger.error("POS load failed: %s", e)
        return False


def send_batch(endpoint: str, transactions: list) -> dict:
    response = requests.post(
        endpoint,
        json={"transactions": transactions},
        timeout=10,
    )
    if response.status_code != 200:
        logger.error("API error %s: %s", response.status_code, response.text)
        return {"successful": 0, "duplicates": 0, "failed": len(transactions)}
    return response.json()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load POS transactions into the API")
    parser.add_argument("--csv_file", default="data/pos_transactions.csv")
    parser.add_argument("--api_url", default="http://localhost:8000")
    parser.add_argument("--batch_size", type=int, default=500)
    args = parser.parse_args()

    raise SystemExit(0 if load_pos(args.csv_file, args.api_url, args.batch_size) else 1)
