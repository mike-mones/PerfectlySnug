#!/usr/bin/env python3
"""
End-to-end Phase 1 runner: pull data -> features -> labels -> LONO-CV ->
train final model.

Usage:
    cd PerfectlySnug
    .venv/bin/python tools/train_v6_ml.py [--out ml/state/snug_model_v6.txt]

Reports:
  - Per-night comfort rate (v5 actual / dumb baseline / ML LONO)
  - Aggregate comparisons + deployment-gate verdicts
  - Feature importances of the final model trained on all nights
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# Allow running as a script from PerfectlySnug/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml import data_io
from ml.features import build_features, build_labels, FEATURE_COLUMNS
from ml.training import (
    leave_one_night_out, train_final_model, feature_importance,
)


def fmt_pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="ml/state/snug_model_v6.txt",
                    help="path for the final trained LightGBM model")
    ap.add_argument("--report", default="ml/state/lono_report.json",
                    help="path for the LONO-CV report JSON")
    ap.add_argument("--csv", default="ml/state/lono_per_night.csv")
    ap.add_argument("--no-train-final", action="store_true",
                    help="skip training the all-nights final model (CV only)")
    args = ap.parse_args()

    print("=" * 80)
    print("  PerfectlySnug ML Controller — Phase 1 Training Pipeline")
    print("=" * 80)

    print("\n[1/5] Loading data from PostgreSQL …")
    readings = data_io.load_readings("v5%")
    stages = data_io.load_sleep_segments()
    health = data_io.load_health_metrics()
    print(f"  readings:       {len(readings):>5}")
    print(f"  sleep_segments: {len(stages):>5}")
    print(f"  health_metrics: {len(health):>5}")

    print("\n[2/5] Grouping into nights …")
    readings = data_io.assign_nights(readings)
    summary = data_io.night_summary(readings)
    print(summary.to_string(index=False))

    print("\n[3/5] Building features …")
    feats = build_features(readings, stages, health)
    print(f"  feature rows: {len(feats)}, columns: {len(FEATURE_COLUMNS)}")

    print("\n[4/5] Building labels …")
    labels = build_labels(feats)
    print(f"  labelled rows: {len(labels)}")
    print("  weight distribution:")
    print(labels["sample_weight"].value_counts().sort_index().to_string())

    print("\n[5/5] Leave-One-Night-Out cross-validation …")
    per_night, agg = leave_one_night_out(feats, labels)

    print("\nPer-night comfort (% of readings within ±1 L1 of preference)")
    print("-" * 80)
    print(f"{'#':>2}  {'date':<12} {'rows':>4} {'ovr':>3}   "
          f"{'v5':>6} {'dumb':>6} {'smart':>6} {'ML':>6}   "
          f"{'RMSE':>5} {'bias':>6}")
    print("-" * 88)
    for _, r in per_night.iterrows():
        print(f"{r.night_id:>2}  {r.date:<12} {r.rows:>4} {r.overrides:>3}   "
              f"{fmt_pct(r.comfort_rate_v5)} {fmt_pct(r.comfort_rate_dumb)} "
              f"{fmt_pct(r.comfort_rate_smart)} {fmt_pct(r.comfort_rate_ml)}   "
              f"{r.rmse_setting_ml:>5.2f} {r.bias_ml:>+6.2f}")
    print("-" * 88)
    print(f"{'mean':>20}        "
          f"{fmt_pct(agg['comfort_v5_mean'])} "
          f"{fmt_pct(agg['comfort_dumb_mean'])} "
          f"{fmt_pct(agg['comfort_smart_mean'])} "
          f"{fmt_pct(agg['comfort_ml_mean'])}   "
          f"{agg['rmse_ml_mean']:>5.2f} {agg['bias_ml_mean']:>+6.2f}")

    print("\nAggregate held-out metrics")
    print("-" * 80)
    print(f"  v5 actual comfort:    {fmt_pct(agg['comfort_v5_mean'])} "
          f"(±{fmt_pct(agg['comfort_v5_std'])})")
    print(f"  dumb baseline:         {fmt_pct(agg['comfort_dumb_mean'])}")
    print(f"  smart baseline (v5 cycle+room, no learning): {fmt_pct(agg['comfort_smart_mean'])} "
          f"(±{fmt_pct(agg['comfort_smart_std'])})")
    print(f"  ML LONO-CV (smart+residual):                  {fmt_pct(agg['comfort_ml_mean'])} "
          f"(±{fmt_pct(agg['comfort_ml_std'])})")
    print(f"  ML beats v5 on    {agg['ml_beats_v5_nights']}/{agg['n_nights']} nights")
    print(f"  ML beats smart on {agg['ml_beats_smart_nights']}/{agg['n_nights']} nights")
    print(f"  smart beats v5 on {agg['smart_beats_v5_nights']}/{agg['n_nights']} nights")
    print(f"  ML predicted-overrides: {agg['ml_pred_overrides_total']} "
          f"(v5 actual {agg['v5_pred_overrides_total']}, "
          f"dumb {agg['dumb_pred_overrides_total']})")

    print("\nStrong-preference comfort (rows within ±60 min of an override)")
    print("-" * 80)
    print(f"  v5 actual:  {fmt_pct(agg['comfort_strong_v5_mean'])}")
    print(f"  smart:      {fmt_pct(agg['comfort_strong_smart_mean'])}")
    print(f"  ML LONO:    {fmt_pct(agg['comfort_strong_ml_mean'])}")
    print(f"  ML beats v5    on {agg['ml_beats_v5_strong_nights']}/{agg['n_nights']} strong nights")
    print(f"  smart beats v5 on {agg['smart_beats_v5_strong_nights']}/{agg['n_nights']} strong nights")

    # Deployment gate (PRD §4.4)
    gate_v5 = agg["comfort_ml_mean"] > agg["comfort_v5_mean"]
    gate_smart = agg["comfort_ml_mean"] > agg["comfort_smart_mean"]
    print("\nDeployment gate (PRD §4.4)")
    print("-" * 80)
    print(f"  ML > v5 baseline?     {'PASS' if gate_v5 else 'FAIL'}")
    print(f"  ML > smart baseline?  {'PASS' if gate_smart else 'FAIL'}")
    print(f"  → {'READY for Phase 2' if (gate_v5 and gate_smart) else 'NOT READY — investigate before deploying'}")

    out_dir = Path(args.report).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(agg, indent=2))
    per_night.to_csv(args.csv, index=False)
    print(f"\nWrote {args.report} and {args.csv}")

    if not args.no_train_final:
        print("\nTraining final all-nights model …")
        booster = train_final_model(labels)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        booster.save_model(str(out_path))
        print(f"  saved {out_path}")

        fi = feature_importance(booster, top=15)
        print("\nTop feature importances (gain)")
        print(fi.to_string(index=False))


if __name__ == "__main__":
    main()
