"""
Counterfactual replay: how would the right-zone overheat rail have engaged
across the past N days, with body_center_f vs body_left_f?

This is the formal verification of the 2026-04-30 sensor swap. The earlier
SQL approximation ("body crosses 88°F for 2 ticks") ignored hysteresis,
occupancy gating, and the BedJet 30-min suppression. This replay drives
the actual rail logic (`_step` from tests/test_right_overheat_safety.py)
through the historical data minute-by-minute.

Output:
  - per-sensor: total engage events, total minutes engaged, per-night
    breakdown.
  - delta vs old behaviour, formatted for the rollout report.

Run:
    cd PerfectlySnug && .venv/bin/python tools/replay_right_rail.py \\
        --csv /tmp/right_zone_14d.csv

The CSV columns expected: ts, body_center_f, body_left_f, body_right_f,
bed_occupied_right, bed_right_pressure_pct.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_right_overheat_safety import _step, BEDJET_SUPPRESS_MIN  # noqa: E402


def _to_bool(v) -> bool:
    return str(v).strip().lower() in ("true", "t", "1", "yes", "on")


def assign_night(ts: pd.Series) -> pd.Series:
    """Same 18:00-local split used in the contamination SQL view."""
    local = ts.dt.tz_convert("America/New_York")
    return ((local - pd.Timedelta(hours=12)).dt.floor("D")).astype(str)


def replay(df: pd.DataFrame, sensor_col: str, *, rail_enabled: bool = True) -> dict:
    """Drive _step over each night with the chosen sensor column."""
    df = df.sort_values("ts").reset_index(drop=True)
    df["night"] = assign_night(df["ts"])

    out_rows = []
    per_night = Counter()
    engage_events_per_night = Counter()
    minutes_engaged_per_night = Counter()

    for night, g in df.groupby("night"):
        g = g.reset_index(drop=True)
        state: dict = {}
        # Track right-bed onset for BedJet window
        first_occupied_ts = None
        prev_engaged = False
        for _, r in g.iterrows():
            occupied = bool(r.get("bed_occupied_right")) if pd.notna(r.get("bed_occupied_right")) else False
            body = r.get(sensor_col)
            if pd.isna(body):
                body = None
            else:
                body = float(body)

            if occupied and first_occupied_ts is None:
                first_occupied_ts = r["ts"]
            if not occupied:
                first_occupied_ts = None

            mins_since_onset = None
            if first_occupied_ts is not None:
                delta = (r["ts"] - first_occupied_ts).total_seconds() / 60.0
                mins_since_onset = delta

            action = _step(state, body=body, occupied=occupied,
                           rail_enabled=rail_enabled,
                           minutes_since_onset=mins_since_onset)

            currently_engaged = state.get("engaged", False)
            if currently_engaged and not prev_engaged:
                engage_events_per_night[night] += 1
            if currently_engaged:
                # Each row is ~5 min in controller_readings, but to keep this
                # comparable we count rows-engaged.
                minutes_engaged_per_night[night] += 1

            per_night[night] += 1
            prev_engaged = currently_engaged

            if action is not None or currently_engaged:
                out_rows.append({
                    "night": night, "ts": r["ts"], "sensor": sensor_col,
                    "body": body, "occupied": occupied,
                    "mins_since_onset": mins_since_onset,
                    "action": action, "engaged": currently_engaged,
                })

    return {
        "sensor": sensor_col,
        "rows_total": int(len(df)),
        "engage_events": int(sum(engage_events_per_night.values())),
        "rows_engaged": int(sum(minutes_engaged_per_night.values())),
        "nights": sorted(per_night.keys()),
        "engage_events_per_night": dict(engage_events_per_night),
        "rows_engaged_per_night": dict(minutes_engaged_per_night),
        "trace_head": out_rows[:8],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, required=True)
    args = p.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["ts"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")

    print(f"Loaded {len(df)} rows from {args.csv}")
    print(f"Nights covered: {df['ts'].dt.tz_convert('America/New_York').dt.date.nunique()}\n")

    res_center = replay(df, "body_center_f")
    res_left   = replay(df, "body_left_f")

    print(f"{'sensor':<20} {'engage_events':>15} {'rows_engaged':>15} "
          f"{'engaged_pct':>12}")
    for r in (res_center, res_left):
        pct = 100.0 * r["rows_engaged"] / max(r["rows_total"], 1)
        print(f"{r['sensor']:<20} {r['engage_events']:>15} "
              f"{r['rows_engaged']:>15} {pct:>11.1f}%")

    print()
    print("Per-night engage events:")
    print(f"{'night':<14} {'center':>10} {'left':>10}")
    nights = sorted(set(res_center["nights"]) | set(res_left["nights"]))
    for n in nights:
        c = res_center["engage_events_per_night"].get(n, 0)
        l = res_left["engage_events_per_night"].get(n, 0)
        print(f"{n:<14} {c:>10} {l:>10}")

    delta_events = res_center["engage_events"] - res_left["engage_events"]
    delta_minutes = res_center["rows_engaged"] - res_left["rows_engaged"]
    if res_center["engage_events"] > 0:
        ratio = res_center["engage_events"] / max(res_left["engage_events"], 0.5)
        print(f"\nSensor swap reduces engage events by {delta_events} "
              f"({ratio:.1f}× fewer)")
        print(f"Sensor swap reduces engaged minutes by {delta_minutes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
