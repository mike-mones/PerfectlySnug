"""
Build the discomfort-label corpus and compute candidate-signal validation
metrics vs override events.

Run from a workstation with ssh access to macmini (PG host):

    cd PerfectlySnug && .venv/bin/python tools/build_discomfort_corpus.py \
        --out ml/state/discomfort_corpus.parquet \
        --report-out docs/discomfort_labeling_metrics.json

Outputs:
  - parquet:  per-minute (ts, zone='left', signals, label, weight, source)
  - JSON:     ROC table per signal, corpus summary numbers
  - stdout:   markdown table that can be pasted into the report

This script reuses ml.data_io for PG access (ssh + psql --csv) so it
inherits the same auth/host config as the rest of the pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from ml import data_io
from ml.discomfort_label import (
    build_label_corpus,
    compute_candidate_signals,
    corpus_summary,
    precision_recall_vs_overrides,
)


# ── Minute-resolution joiner ───────────────────────────────────────────

def to_minute_grid(rd: pd.DataFrame, stages: pd.DataFrame,
                   health: pd.DataFrame,
                   movement: pd.DataFrame | None = None) -> pd.DataFrame:
    """Join 5-min controller readings + Apple Watch stages + health metrics
    + (optional) per-minute movement-density onto a regular per-minute grid,
    per night.

    Forward-fills controller fields up to 6 min (skip past gaps); HR/HRV/RR
    are forward-filled up to 10 min (Apple Watch arrives in batches). Movement
    fields are NOT forward-filled — a missing minute means no pressure events
    were recorded (likely either bed empty or sensor offline), and we want to
    distinguish that from "0 movements but bed sensor was active".
    """
    rd = rd.sort_values("ts").set_index("ts")
    if rd.empty:
        return pd.DataFrame()
    night_grids = []
    rd["night"] = (rd.index - pd.Timedelta(hours=6)).date
    for night, g in rd.groupby("night"):
        if len(g) < 5:
            continue
        idx = pd.date_range(g.index.min().floor("min"),
                            g.index.max().ceil("min"), freq="1min",
                            tz=g.index.tz)
        per_min = pd.DataFrame(index=idx)
        per_min.index.name = "ts"

        # Controller fields (5-min cadence)
        ctl = g[["body_avg_f", "body_left_f", "body_center_f", "room_temp_f",
                 "setting", "elapsed_min", "action", "override_delta",
                 "bed_left_calibrated_pressure_pct", "bed_occupied_left"]] \
                .reindex(per_min.index, method="ffill", limit=6)
        per_min = per_min.join(ctl)
        per_min = per_min.rename(columns={
            "bed_left_calibrated_pressure_pct": "pressure_pct",
            "bed_occupied_left": "occupied",
        })

        # Override flag: true on the minute the override was logged.
        ovr_mask = pd.Series(False, index=per_min.index)
        for ts, row in g[g["action"] == "override"].iterrows():
            m = ts.floor("min")
            if m in ovr_mask.index:
                ovr_mask.loc[m] = True
        per_min["is_override"] = ovr_mask

        # Apple Watch stages
        stg_per_min = pd.Series("unknown", index=per_min.index, dtype=object)
        if not stages.empty:
            night_stg = stages[(stages["start_ts"] <= per_min.index.max())
                               & (stages["end_ts"] >= per_min.index.min())]
            for _, s in night_stg.iterrows():
                m = (per_min.index >= s["start_ts"]) & (per_min.index < s["end_ts"])
                stg_per_min.loc[m] = s["stage"]
        per_min["sleep_stage"] = stg_per_min

        # Health metrics — pivot then forward-fill within night
        if not health.empty:
            night_h = health[(health["ts"] >= per_min.index.min())
                             & (health["ts"] <= per_min.index.max())]
            if not night_h.empty:
                # pandas 3.0 changed pivot_table's default index semantics; be
                # explicit so we always pivot ts→rows, metric_name→cols.
                p = night_h.pivot_table(values="value", index="ts",
                                        columns="metric_name", aggfunc="mean")
                p = p.reindex(per_min.index.union(p.index)).sort_index()
                p = p.ffill(limit=10).reindex(per_min.index)
                per_min["hr"]  = p.get("heart_rate")
                per_min["hrv"] = p.get("heart_rate_variability")
                per_min["rr"]  = p.get("respiratory_rate")
        for c in ["hr", "hrv", "rr"]:
            if c not in per_min.columns:
                per_min[c] = np.nan

        # Bed-pressure movement density (high-resolution per-minute counts)
        if movement is not None and not movement.empty:
            try:
                night_mv = movement[
                    (movement.index >= per_min.index.min())
                    & (movement.index <= per_min.index.max())
                ]
                if not night_mv.empty:
                    per_min = per_min.join(night_mv[
                        ["n_events", "n_movements", "max_delta", "pressure_std"]
                    ])
                    # Fill explicit zeros for minutes with no events
                    for c in ["n_events", "n_movements", "max_delta", "pressure_std"]:
                        if c in per_min.columns:
                            per_min[c] = per_min[c].fillna(0)
            except Exception:
                pass
        for c in ["n_events", "n_movements", "max_delta", "pressure_std"]:
            if c not in per_min.columns:
                per_min[c] = 0.0

        per_min["night"] = night
        night_grids.append(per_min)

    if not night_grids:
        return pd.DataFrame()
    return pd.concat(night_grids).sort_index()


# ── Per-night percentile-gating bookkeeping ────────────────────────────

def attach_signals_per_night(per_min: pd.DataFrame) -> pd.DataFrame:
    """Apply compute_candidate_signals() one night at a time so trailing
    percentile windows don't leak across night boundaries."""
    out = []
    for night, g in per_min.groupby("night", sort=True):
        out.append(compute_candidate_signals(g))
    return pd.concat(out).sort_index() if out else per_min


# ── CLI ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="ml/state/discomfort_corpus.parquet")
    ap.add_argument("--report-out",
                    default="docs/discomfort_labeling_metrics.json")
    ap.add_argument("--lead-lo", type=int, default=5,
                    help="lower bound (min) of pre-override lead window")
    ap.add_argument("--lead-hi", type=int, default=15,
                    help="upper bound (min) of pre-override lead window")
    args = ap.parse_args()

    print("Loading PG…")
    rd     = data_io.load_readings()
    stages = data_io.load_sleep_segments()
    health = data_io.load_health_metrics()
    print(f"  controller_readings: {len(rd):,} rows")
    print(f"  sleep_segments:      {len(stages):,} rows")
    print(f"  health_metrics:      {len(health):,} rows")

    # Bed-pressure movement (HA recorder, optional — depends on HA reachable)
    movement = pd.DataFrame()
    if not rd.empty:
        try:
            mv_start = rd["ts"].min().isoformat()
            mv_end   = rd["ts"].max().isoformat()
            movement = data_io.load_movement_per_minute(
                start=mv_start, end=mv_end, side="left")
            print(f"  bed_movement_min:    {len(movement):,} minute-rows "
                  f"(HA recorder, sub-second pressure events aggregated)")
        except Exception as e:
            print(f"  bed_movement_min:    SKIPPED ({e.__class__.__name__}: {e})")

    n_overrides = int((rd["action"] == "override").sum())
    print(f"\nGround-truth overrides (left): {n_overrides}")
    if n_overrides != 47:
        print(f"  ⚠ note: PROGRESS_REPORT cited 47, actual is {n_overrides}")

    print("\nGridding to per-minute…")
    per_min = to_minute_grid(rd, stages, health, movement=movement)
    print(f"  {len(per_min):,} minute-rows across {per_min['night'].nunique()} nights")

    print("\nComputing candidate signals (per-night)…")
    per_min = attach_signals_per_night(per_min)

    print("\nValidating signals against overrides "
          f"(lead window {args.lead_lo}-{args.lead_hi} min)…")
    metrics = precision_recall_vs_overrides(
        per_min, lead_window_min=(args.lead_lo, args.lead_hi))

    print("\n| signal              | precision | recall | FPR/night | fires | caught |")
    print("|---------------------|-----------|--------|-----------|-------|--------|")
    for sig, m in metrics.items():
        print(f"| {sig:<19} | {m['precision']:>9.2f} | "
              f"{m['recall']:>6.2f} | {m['fpr_per_night']:>9.2f} | "
              f"{m['n_fires']:>5d} | {m['n_overrides_caught']:>6d} |")

    print("\nAssembling labelled corpus…")
    labelled = build_label_corpus(per_min)
    summary = corpus_summary(labelled)
    print(json.dumps(summary, indent=2))

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["night", "setting", "elapsed_min", "room_temp_f", "body_avg_f",
            "occupied", "is_override", "proxy_fired",
            "discomfort_label", "label_weight", "label_source",
            "proxy_signals",
            "sig_hr_spike", "sig_hrv_dip", "sig_rr_jump",
            "sig_stage_frag", "sig_pressure_burst", "sig_body_sd_q4",
            "sig_movement_density",
            "n_movements", "max_delta", "pressure_std",
            "sig_combined"]
    cols = [c for c in cols if c in labelled.columns]
    try:
        labelled[cols].to_parquet(out_path)
        print(f"\nWrote {out_path.relative_to(ROOT)}")
    except Exception as e:
        csv_path = out_path.with_suffix(".csv")
        labelled[cols].to_csv(csv_path)
        print(f"\n(parquet engine missing: {e}) -> wrote CSV {csv_path.relative_to(ROOT)}")

    report_path = ROOT / args.report_out
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "n_overrides_left": n_overrides,
        "lead_window_min": [args.lead_lo, args.lead_hi],
        "signals": metrics,
        "corpus_summary": summary,
    }, indent=2, default=str))
    print(f"Wrote {report_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
