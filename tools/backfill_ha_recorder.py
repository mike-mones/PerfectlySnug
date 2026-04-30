#!/usr/bin/env python3
"""Backfill HA recorder long-term + short-term statistics into PG ha_stats.

HA's recorder purges raw states at 30 days but keeps hourly aggregates in
`statistics` (long-term) potentially indefinitely. This script copies the
HA SQLite DB locally, extracts topper / bed-presence / bedroom temperature
entities, and loads them into PG `ha_stats` (idempotent ON CONFLICT).

Usage:
    python tools/backfill_ha_recorder.py
        --ha-host root@192.168.0.106
        --pg-host 192.168.0.3
        --pg-user sleepsync
        --pg-db sleepdata
        --pg-pass-env PG_PASSWORD

The PG schema is created if missing. Run periodically (weekly) so older
short-term rows are captured before the recorder purges them.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ENTITY_PREFIXES = (
    "sensor.smart_topper_",
    "sensor.bed_presence_",
    "sensor.bedroom_temperature_",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ha_stats (
    statistic_id TEXT       NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    mean         REAL,
    min          REAL,
    max          REAL,
    source       TEXT       NOT NULL CHECK (source IN ('long_term','short_term')),
    PRIMARY KEY (statistic_id, ts, source)
);
CREATE INDEX IF NOT EXISTS ha_stats_ts_idx ON ha_stats (ts);
CREATE INDEX IF NOT EXISTS ha_stats_entity_ts_idx ON ha_stats (statistic_id, ts);
"""


def extract(ha_db: Path, csv_path: Path) -> tuple[int, int]:
    con = sqlite3.connect(ha_db)
    where = " OR ".join(f"statistic_id LIKE '{p}%'" for p in ENTITY_PREFIXES)
    by_id: dict[int, str] = {
        mid: sid
        for mid, sid in con.execute(
            f"SELECT id, statistic_id FROM statistics_meta WHERE {where}"
        )
    }
    if not by_id:
        print("No matching entities found in HA recorder", file=sys.stderr)
        return (0, 0)
    n_long = n_short = 0
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        for table, src in [("statistics", "long_term"),
                           ("statistics_short_term", "short_term")]:
            sql = (f"SELECT metadata_id, start_ts, mean, min, max, state "
                   f"FROM {table} WHERE metadata_id IN "
                   f"({','.join(str(i) for i in by_id)})")
            for mid, ts, mean, mn, mx, state in con.execute(sql):
                val = mean if mean is not None else state
                w.writerow([
                    by_id[mid],
                    "" if val is None else val,
                    "" if mn is None else mn,
                    "" if mx is None else mx,
                    src,
                    ts,
                ])
                if src == "long_term":
                    n_long += 1
                else:
                    n_short += 1
    return n_long, n_short


def load(csv_path: Path, pg_host: str, pg_user: str, pg_db: str, pg_pass: str) -> None:
    env = {**os.environ, "PGPASSWORD": pg_pass}
    sql = SCHEMA_SQL + f"""
CREATE UNLOGGED TABLE IF NOT EXISTS ha_stats_stage (
    statistic_id TEXT, mean REAL, min REAL, max REAL,
    source TEXT, epoch DOUBLE PRECISION
);
TRUNCATE ha_stats_stage;
\\copy ha_stats_stage FROM '{csv_path}' CSV
INSERT INTO ha_stats (statistic_id, ts, mean, min, max, source)
SELECT statistic_id, to_timestamp(epoch), mean, min, max, source
FROM ha_stats_stage
ON CONFLICT DO NOTHING;
DROP TABLE ha_stats_stage;
"""
    subprocess.run(
        ["psql", "-h", pg_host, "-U", pg_user, "-d", pg_db,
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, env=env,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ha-host", default="root@192.168.0.106",
                   help="ssh target where HA recorder lives")
    p.add_argument("--ha-db", default="/config/home-assistant_v2.db")
    p.add_argument("--pg-host", default="192.168.0.3")
    p.add_argument("--pg-user", default="sleepsync")
    p.add_argument("--pg-db", default="sleepdata")
    p.add_argument("--pg-pass-env", default="PG_PASSWORD",
                   help="env var holding PG password")
    args = p.parse_args()

    pg_pass = os.environ.get(args.pg_pass_env)
    if not pg_pass:
        print(f"Missing env {args.pg_pass_env}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        local_db = Path(tmp) / "ha.db"
        csv_path = Path(tmp) / "ha_stats.csv"
        print(f"Copying HA recorder from {args.ha_host}...", file=sys.stderr)
        subprocess.run(
            ["scp", "-q", f"{args.ha_host}:{args.ha_db}", str(local_db)],
            check=True,
        )
        n_long, n_short = extract(local_db, csv_path)
        print(f"Extracted {n_long:,} long-term + {n_short:,} short-term rows",
              file=sys.stderr)
        if n_long + n_short == 0:
            return 0
        load(csv_path, args.pg_host, args.pg_user, args.pg_db, pg_pass)
        print("Loaded into PG ha_stats", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
