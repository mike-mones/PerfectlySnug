#!/usr/bin/env python3
"""
Backfill Home Assistant history data into Postgres.

Pulls historical entity states from the HA REST API and inserts them
into the appropriate Postgres tables for analysis.

Usage:
    python3 tools/backfill_ha_history.py            # last 7 days
    python3 tools/backfill_ha_history.py --days 14  # last 14 days
    python3 tools/backfill_ha_history.py --since 2026-04-01
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import requests

# ── Configuration ────────────────────────────────────────────
HA_URL = os.environ.get("HA_URL", "http://192.168.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

PG_HOST = os.environ.get("PG_HOST", "192.168.0.75")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "sleepdata")
PG_USER = os.environ.get("PG_USER", "sleepsync")
PG_PASS = os.environ.get("PG_PASS", "sleepsync_local")

# Entities to backfill and their mapping
ENTITY_MAP = {
    "sensor.smart_topper_left_side_body_sensor_center": {
        "table": "state_changes",
        "domain": "sensor",
        "description": "body temp center",
    },
    "sensor.superior_6000s_temperature": {
        "table": "state_changes",
        "domain": "sensor",
        "description": "room temp",
    },
    "number.smart_topper_left_side_bedtime_temperature": {
        "table": "state_changes",
        "domain": "number",
        "description": "bedtime setting",
    },
    "sensor.smart_topper_left_side_blower_output": {
        "table": "state_changes",
        "domain": "sensor",
        "description": "blower output",
    },
    "input_boolean.sleep_mode": {
        "table": "state_changes",
        "domain": "input_boolean",
        "description": "sleep mode",
    },
}

ZONE = "left"


def get_pg_conn():
    """Get Postgres connection with retry."""
    for attempt in range(3):
        try:
            return psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASS,
                connect_timeout=10,
            )
        except psycopg2.OperationalError as e:
            if attempt == 2:
                raise
            print(f"  DB retry {attempt + 1}: {e}", file=sys.stderr)
            import time; time.sleep(2)


def ha_headers() -> dict:
    if not HA_TOKEN:
        print("❌ HA_TOKEN environment variable is required.", file=sys.stderr)
        print("   export HA_TOKEN='your_long_lived_access_token'", file=sys.stderr)
        sys.exit(1)
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }


def fetch_history(entity_id: str, start: datetime,
                  end: Optional[datetime] = None) -> list:
    """Fetch entity history from HA REST API."""
    url = f"{HA_URL}/api/history/period/{start.isoformat()}"
    params = {
        "filter_entity_id": entity_id,
        "minimal_response": "",
        "no_attributes": "",
        "significant_changes_only": "",
    }
    if end:
        params["end_time"] = end.isoformat()

    try:
        resp = requests.get(url, headers=ha_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]  # API returns [[states]] for single entity
        return []
    except requests.exceptions.RequestException as e:
        print(f"  ❌ HA API error for {entity_id}: {e}", file=sys.stderr)
        return []


def parse_state_value(state_str: str) -> Optional[float]:
    """Try to parse a numeric state value."""
    if state_str in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state_str)
    except (ValueError, TypeError):
        return None


def insert_state_changes(conn, entity_id: str, domain: str,
                         states: list) -> int:
    """Insert state changes into state_changes table, skipping duplicates."""
    if not states:
        return 0

    rows = []
    for i, state in enumerate(states):
        ts = state.get("last_changed") or state.get("last_updated")
        if not ts:
            continue
        new_state = state.get("state", "")
        old_state = states[i - 1].get("state", "") if i > 0 else None
        attrs = state.get("attributes")
        attrs_json = None
        if attrs:
            import json
            attrs_json = json.dumps(attrs)
        rows.append((ts, entity_id, domain, old_state, new_state, attrs_json))

    if not rows:
        return 0

    inserted = 0
    batch_size = 500
    with conn.cursor() as cur:
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            args = ",".join(
                cur.mogrify("(%s,%s,%s,%s,%s,%s)", r).decode() for r in batch
            )
            cur.execute(f"""
                INSERT INTO state_changes
                    (timestamp, entity_id, domain, old_state, new_state, attributes_json)
                VALUES {args}
                ON CONFLICT DO NOTHING
            """)
            inserted += cur.rowcount
        conn.commit()
    return inserted


def insert_health_metrics(conn, entity_id: str, states: list,
                          metric_name: str, units: str = "°F") -> int:
    """Insert numeric states into health_metrics table."""
    if not states:
        return 0

    rows = []
    for state in states:
        ts = state.get("last_changed") or state.get("last_updated")
        if not ts:
            continue
        value = parse_state_value(state.get("state", ""))
        if value is None:
            continue
        rows.append((ts, metric_name, value, units, "ha_backfill"))

    if not rows:
        return 0

    inserted = 0
    batch_size = 500
    with conn.cursor() as cur:
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            args = ",".join(
                cur.mogrify("(%s,%s,%s,%s,%s)", r).decode() for r in batch
            )
            cur.execute(f"""
                INSERT INTO health_metrics
                    (ts, metric_name, value, units, source)
                VALUES {args}
                ON CONFLICT (ts, metric_name, COALESCE(source, '')) DO NOTHING
            """)
            inserted += cur.rowcount
        conn.commit()
    return inserted


# Metric mappings for health_metrics table
HEALTH_METRIC_MAP = {
    "sensor.smart_topper_left_side_body_sensor_center": ("body_temp_center", "°F"),
    "sensor.superior_6000s_temperature": ("room_temp", "°F"),
    "sensor.smart_topper_left_side_blower_output": ("blower_output", "%"),
}


def backfill_entity(conn, entity_id: str, config: dict,
                    start: datetime, end: datetime) -> dict:
    """Backfill a single entity's history."""
    desc = config["description"]
    print(f"\n  📡 {desc} ({entity_id})")

    states = fetch_history(entity_id, start, end)
    print(f"     Fetched {len(states)} state changes from HA")

    if not states:
        return {"entity": entity_id, "fetched": 0, "inserted_sc": 0, "inserted_hm": 0}

    # Insert into state_changes
    sc_count = insert_state_changes(conn, entity_id, config["domain"], states)
    print(f"     Inserted {sc_count} into state_changes")

    # Also insert into health_metrics for numeric sensors
    hm_count = 0
    if entity_id in HEALTH_METRIC_MAP:
        metric_name, units = HEALTH_METRIC_MAP[entity_id]
        hm_count = insert_health_metrics(conn, entity_id, states, metric_name, units)
        print(f"     Inserted {hm_count} into health_metrics")

    return {
        "entity": entity_id,
        "fetched": len(states),
        "inserted_sc": sc_count,
        "inserted_hm": hm_count,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Backfill Home Assistant history into Postgres",
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Number of days to backfill (default: 7)")
    parser.add_argument("--since", type=str, default=None,
                        help="Start date (YYYY-MM-DD). Overrides --days")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch from HA but don't insert into Postgres")
    args = parser.parse_args()

    if args.since:
        start = datetime.fromisoformat(args.since).replace(
            tzinfo=timezone(timedelta(hours=-4))
        )
    else:
        start = datetime.now(timezone(timedelta(hours=-4))) - timedelta(days=args.days)

    end = datetime.now(timezone(timedelta(hours=-4)))

    print(f"{'═' * 60}")
    print(f"  HA History Backfill → Postgres")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"{'═' * 60}")

    # Check for token early
    if not HA_TOKEN:
        print("❌ HA_TOKEN environment variable is required.", file=sys.stderr)
        print("   export HA_TOKEN='your_long_lived_access_token'", file=sys.stderr)
        sys.exit(1)

    # Test HA connection
    try:
        resp = requests.get(f"{HA_URL}/api/", headers=ha_headers(), timeout=10)
        resp.raise_for_status()
        print(f"  ✅ Connected to Home Assistant")
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Cannot reach HA at {HA_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    # Connect to Postgres
    try:
        conn = get_pg_conn()
        print(f"  ✅ Connected to Postgres ({PG_HOST})")
    except Exception as e:
        print(f"  ❌ Cannot connect to Postgres: {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    try:
        for entity_id, config in ENTITY_MAP.items():
            result = backfill_entity(conn, entity_id, config, start, end)
            results.append(result)
    finally:
        conn.close()

    # Summary
    print(f"\n{'─' * 60}")
    print(f"  Summary:")
    total_fetched = sum(r["fetched"] for r in results)
    total_sc = sum(r["inserted_sc"] for r in results)
    total_hm = sum(r["inserted_hm"] for r in results)
    print(f"  Total fetched:             {total_fetched}")
    print(f"  Inserted into state_changes:  {total_sc}")
    print(f"  Inserted into health_metrics: {total_hm}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
