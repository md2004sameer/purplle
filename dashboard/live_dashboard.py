"""
Terminal dashboard for live Store Intelligence metrics.

It polls the API while events are ingested and refreshes one screen with the
current visitor, conversion, queue, and dwell metrics.
"""

import argparse
import os
import time

import requests


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def render(metrics: dict):
    clear_screen()
    print("Store Intelligence Live Metrics")
    print("=" * 36)
    print(f"Store:              {metrics['store_id']}")
    print(f"Updated:            {metrics['timestamp']}")
    print(f"Total visitors:     {metrics['total_visitors']}")
    print(f"Unique visitors:    {metrics['unique_visitors']}")

    conversion = metrics.get("conversion_rate")
    conversion_text = "n/a" if conversion is None else f"{conversion:.2f}%"
    print(f"Conversion rate:    {conversion_text}")

    queue_depth = metrics.get("current_queue_depth")
    print(f"Queue depth:        {queue_depth if queue_depth is not None else 'n/a'}")

    abandonment = metrics.get("queue_abandonment_rate")
    abandonment_text = "n/a" if abandonment is None else f"{abandonment:.2f}%"
    print(f"Abandonment rate:   {abandonment_text}")

    print("\nAverage dwell by zone")
    print("-" * 36)
    dwell = metrics.get("avg_dwell_per_zone") or {}
    if not dwell:
        print("No dwell events yet")
    for zone_id, dwell_ms in sorted(dwell.items()):
        print(f"{zone_id:<20} {dwell_ms:>10.0f} ms")


def run(api_url: str, store_id: str, interval_seconds: float):
    endpoint = f"{api_url}/stores/{store_id}/metrics"
    while True:
        try:
            response = requests.get(endpoint, timeout=5)
            response.raise_for_status()
            render(response.json())
        except requests.RequestException as e:
            clear_screen()
            print("Store Intelligence Live Metrics")
            print("=" * 36)
            print(f"Waiting for API: {e}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll API metrics as a live terminal dashboard")
    parser.add_argument("--api_url", default="http://localhost:8000")
    parser.add_argument("--store_id", required=True)
    parser.add_argument("--interval_seconds", type=float, default=2.0)
    args = parser.parse_args()

    run(args.api_url, args.store_id, args.interval_seconds)
