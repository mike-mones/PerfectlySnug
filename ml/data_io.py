"""
Data loading for the ML controller pipeline (Phase 1).

Pulls historical data from the sleepdata PostgreSQL on the Mac Mini via
ssh + psql --csv (mirrors the access pattern in tools/backtest_v5.py so
this script can run from the dev workstation without VPN/tunneling).

Three datasets are returned as pandas DataFrames:
  - readings:       controller_readings, zone='left', v5_* versions only
  - sleep_segments: Apple Watch sleep stages (per-segment)
  - health_metrics: HR / HRV / RR / wrist temp samples

Nights are inferred from gaps in `controller_readings.ts` of >2h, matching
the v5 backtester so night counts are directly comparable.
"""
from __future__ import annotations

import csv
import io
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd


SSH_HOST = "macmini"
PSQL = ("PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost "
        "-d sleepdata --csv --pset=footer=off -c")


def _query(sql: str) -> pd.DataFrame:
    remote = f'{PSQL} "{sql}"'
    res = subprocess.run(
        ["ssh", SSH_HOST, remote],
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"psql failed:\n{res.stderr}")
    if not res.stdout.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(res.stdout))


# ── Loaders ────────────────────────────────────────────────────────────

READING_COLUMNS = [
    "ts", "zone", "phase", "elapsed_min",
    "body_right_f", "body_center_f", "body_left_f", "body_avg_f",
    "ambient_f", "room_temp_f",
    "setting", "effective", "baseline", "learned_adj",
    "action", "override_delta", "controller_version", "setpoint_f",
    "bed_left_calibrated_pressure_pct", "bed_right_calibrated_pressure_pct",
    "bed_occupied_left", "bed_occupied_right",
    "bed_occupied_either", "bed_occupied_both",
]


def load_readings(controller_version_like: str = "v5%") -> pd.DataFrame:
    cols = ", ".join(READING_COLUMNS)
    sql = (
        f"SELECT {cols} FROM controller_readings "
        f"WHERE zone='left' AND controller_version LIKE '{controller_version_like}' "
        f"AND action NOT IN ('empty_bed','passive') "
        f"ORDER BY ts"
    )
    df = _query(sql)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601").dt.tz_convert("America/New_York")
    for c in ["elapsed_min", "body_right_f", "body_center_f", "body_left_f",
              "body_avg_f", "ambient_f", "room_temp_f", "setpoint_f",
              "bed_left_calibrated_pressure_pct",
              "bed_right_calibrated_pressure_pct"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["setting", "effective", "baseline", "learned_adj", "override_delta"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    for c in ["bed_occupied_left", "bed_occupied_right",
              "bed_occupied_either", "bed_occupied_both"]:
        df[c] = df[c].map({"t": True, "f": False, True: True, False: False})
    return df.reset_index(drop=True)


def load_sleep_segments() -> pd.DataFrame:
    sql = ("SELECT night_date, start_ts, end_ts, stage, duration_min "
           "FROM sleep_segments ORDER BY start_ts")
    df = _query(sql)
    if df.empty:
        return df
    df["start_ts"] = pd.to_datetime(df["start_ts"], utc=True, format="ISO8601").dt.tz_convert("America/New_York")
    df["end_ts"] = pd.to_datetime(df["end_ts"], utc=True, format="ISO8601").dt.tz_convert("America/New_York")
    df["duration_min"] = pd.to_numeric(df["duration_min"], errors="coerce")
    return df


def load_health_metrics(metrics: tuple[str, ...] = (
        "heart_rate", "heart_rate_variability",
        "respiratory_rate", "apple_sleeping_wrist_temperature")) -> pd.DataFrame:
    in_clause = ",".join(f"'{m}'" for m in metrics)
    sql = (f"SELECT ts, metric_name, value FROM health_metrics "
           f"WHERE metric_name IN ({in_clause}) ORDER BY ts")
    df = _query(sql)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601").dt.tz_convert("America/New_York")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


# ── Night grouping ─────────────────────────────────────────────────────

def assign_nights(df: pd.DataFrame, gap_hours: float = 2.0,
                  min_readings: int = 5) -> pd.DataFrame:
    """Add `night_id` column; rows with night_id=NaN are dropped."""
    if df.empty:
        df["night_id"] = pd.Series(dtype="Int64")
        return df
    ts = df["ts"].to_numpy()
    night_id = [0]
    cur = 0
    for i in range(1, len(ts)):
        gap = (ts[i] - ts[i - 1]) / pd.Timedelta(hours=1)
        if gap > gap_hours:
            cur += 1
        night_id.append(cur)
    df = df.copy()
    df["night_id"] = night_id
    counts = df["night_id"].value_counts()
    keep = counts[counts >= min_readings].index
    df = df[df["night_id"].isin(keep)].reset_index(drop=True)
    # Re-number nights densely from 0
    remap = {n: i for i, n in enumerate(sorted(df["night_id"].unique()))}
    df["night_id"] = df["night_id"].map(remap)
    return df


def night_summary(readings: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for nid, g in readings.groupby("night_id"):
        ovr = (g["action"] == "override").sum()
        rows.append({
            "night_id": nid,
            "date": g["ts"].iloc[0].date(),
            "rows": len(g),
            "duration_h": (g["ts"].iloc[-1] - g["ts"].iloc[0]).total_seconds() / 3600,
            "overrides": int(ovr),
            "avg_room_f": float(g["room_temp_f"].mean()) if g["room_temp_f"].notna().any() else None,
        })
    return pd.DataFrame(rows)
