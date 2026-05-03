#!/usr/bin/env python3
"""Fit the firmware Stage-1 cap table: L_active setting -> steady-state setpoint_f.

Per `docs/proposals/2026-05-01_recommendation.md` §12, the firmware reports a
`temperature_setpoint` (logged as `controller_readings.setpoint_f`) which the
Stage-1 cap interpolates from the active L setting. This tool computes the
median observed setpoint per L setting and emits a JSON table consumed by
`ml/v6/firmware_plant.py` (R1B).

Known anchor points (from agent.md / firmware spec):
    L = -8 -> ~69 F
    L =  0 -> ~91.4 F
    L = +5 -> ~95.9 F  (~2.8 F per L step)

Empirical fit will be noisier (transient ramps, blocked actions, etc.). The
sanity check warns when warmer settings do not strictly produce warmer median
setpoints.

CLI:
    python tools/firmware_cap_fit.py \
        --since 2026-04-01 \
        --output ml/v6/firmware_cap_table.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

DB_DEFAULTS = {
    "host": "192.168.0.3",
    "port": 5432,
    "dbname": "sleepdata",
    "user": "sleepsync",
    "password": "sleepsync_local",
}

ANCHOR_POINTS = {-8: 69.0, 0: 91.4, 5: 95.9}


@dataclass
class CapPoint:
    setting: int
    n: int
    median_setpoint_f: float
    p25_setpoint_f: float
    p75_setpoint_f: float


def connect_db():
    import psycopg2

    cfg = DB_DEFAULTS.copy()
    for key in list(cfg):
        env = os.environ.get(f"SLEEPDATA_{key.upper()}")
        if env:
            cfg[key] = env
    return psycopg2.connect(**cfg)


def fit_from_rows(rows: Iterable[tuple[int, float]]) -> list[CapPoint]:
    """Group (setting, setpoint_f) tuples by setting and compute medians."""
    by_setting: dict[int, list[float]] = {}
    for setting, setpoint in rows:
        if setting is None or setpoint is None:
            continue
        try:
            s = int(setting)
            v = float(setpoint)
        except (TypeError, ValueError):
            continue
        by_setting.setdefault(s, []).append(v)

    out: list[CapPoint] = []
    for s in sorted(by_setting):
        vals = sorted(by_setting[s])
        n = len(vals)
        if n == 0:
            continue
        median = _quantile(vals, 0.5)
        p25 = _quantile(vals, 0.25)
        p75 = _quantile(vals, 0.75)
        out.append(CapPoint(setting=s, n=n, median_setpoint_f=median,
                             p25_setpoint_f=p25, p75_setpoint_f=p75))
    return out


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def check_monotonic(points: list[CapPoint]) -> list[str]:
    """Return a list of warning messages for non-monotonic transitions."""
    warnings: list[str] = []
    prev: CapPoint | None = None
    for p in points:
        if prev is not None and p.median_setpoint_f < prev.median_setpoint_f - 1e-6:
            warnings.append(
                f"non-monotonic: setting {prev.setting} median={prev.median_setpoint_f:.2f}F "
                f"-> setting {p.setting} median={p.median_setpoint_f:.2f}F"
            )
        prev = p
    return warnings


def query_rows(since: str | None, conn=None) -> list[tuple[int, float]]:
    close = False
    if conn is None:
        conn = connect_db()
        close = True
    try:
        cur = conn.cursor()
        sql = (
            "SELECT setting, setpoint_f FROM controller_readings "
            "WHERE setting IS NOT NULL AND setpoint_f IS NOT NULL "
            "AND actual_blower_pct_typed IS NOT NULL"
        )
        params: list[Any] = []
        if since:
            sql += " AND ts >= %s"
            params.append(since)
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        if close:
            conn.close()
    return [(int(r[0]), float(r[1])) for r in rows]


def build_table(points: list[CapPoint], since: str | None) -> dict[str, Any]:
    warnings = check_monotonic(points)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "since": since,
        "n_settings": len(points),
        "anchor_points": {str(k): v for k, v in ANCHOR_POINTS.items()},
        "monotonic_warnings": warnings,
        "is_monotonic": not warnings,
        "table": [
            {
                "setting": p.setting,
                "n": p.n,
                "median_setpoint_f": round(p.median_setpoint_f, 3),
                "p25_setpoint_f": round(p.p25_setpoint_f, 3),
                "p75_setpoint_f": round(p.p75_setpoint_f, 3),
            }
            for p in points
        ],
    }


def write_table(table: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(table, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit firmware Stage-1 cap table from controller_readings")
    ap.add_argument("--since", default=None, help="ISO timestamp lower bound (e.g. 2026-04-01)")
    ap.add_argument("--output", type=Path, default=ROOT / "ml/v6/firmware_cap_table.json")
    ap.add_argument("--from-csv", type=Path, default=None,
                    help="Read (setting,setpoint_f) tuples from a CSV instead of PG (testing)")
    args = ap.parse_args(argv)

    if args.from_csv:
        import csv

        rows: list[tuple[int, float]] = []
        with args.from_csv.open() as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                try:
                    rows.append((int(r["setting"]), float(r["setpoint_f"])))
                except (KeyError, TypeError, ValueError):
                    continue
    else:
        rows = query_rows(args.since)

    points = fit_from_rows(rows)
    table = build_table(points, args.since)
    write_table(table, args.output)

    print(f"wrote {args.output} (n_settings={len(points)}, rows={len(rows)})")
    for p in points:
        print(f"  L={p.setting:+d}  n={p.n:6d}  median_setpoint_f={p.median_setpoint_f:6.2f}  "
              f"[p25={p.p25_setpoint_f:6.2f} p75={p.p75_setpoint_f:6.2f}]")
    if table["monotonic_warnings"]:
        print("WARNING: cap table is not monotonic in setting:", file=sys.stderr)
        for w in table["monotonic_warnings"]:
            print(f"  - {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
