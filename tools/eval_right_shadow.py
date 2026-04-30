"""
Tomorrow-morning evaluator for the right-zone v5.2 shadow controller.

Reads /config/snug_right_v52_shadow.jsonl (via SSH to HA) and produces:
  - decision distribution (how many ticks proposed cooler/warmer/same vs firmware)
  - body-temp range during shadow operation
  - any night-by-night anomalies (e.g., correction stuck at cap, oscillation)

Doesn't replace the offline counterfactual replay against override events —
this is just "did the shadow controller make sane decisions overnight?"

Run from a workstation with `ssh root@192.168.0.106` access:

    cd PerfectlySnug && .venv/bin/python tools/eval_right_shadow.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from collections import Counter

import pandas as pd


HA_HOST = "root@192.168.0.106"
SHADOW_LOG = "/config/snug_right_v52_shadow.jsonl"


def fetch(remote_path: str = SHADOW_LOG) -> list[dict]:
    out = subprocess.run(["ssh", HA_HOST, f"cat {remote_path}"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"ssh fetch failed: {out.stderr}")
    rows = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv-out", type=Path, default=None,
                   help="Optional path to dump full table to CSV.")
    p.add_argument("--occupied-only", action="store_true", default=True)
    args = p.parse_args()

    rows = fetch()
    if not rows:
        print("No shadow log entries yet — wait for at least one full sleep cycle.")
        return 0

    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], format="ISO8601")
    df = df.sort_values("ts").reset_index(drop=True)
    print(f"Loaded {len(df)} ticks across {df['ts'].dt.date.nunique()} dates")

    if args.occupied_only and "occupied" in df.columns:
        df = df[df["occupied"] == True].reset_index(drop=True)
        print(f"Filtered to occupied: {len(df)} ticks")

    if df.empty:
        print("No occupied ticks yet — nothing to evaluate.")
        return 0

    print("\n=== body sensor distribution during shadow operation ===")
    print(df[["body_left", "body_center", "body_right", "body_avg", "body_skin"]].describe()
          .round(2).to_string())

    print("\n=== shadow proposed setting vs firmware actual ===")
    if "right_v52_diff_vs_firmware" in df.columns:
        diff = df["right_v52_diff_vs_firmware"].dropna()
        print(f"diff (shadow - firmware) summary:")
        print(f"  cooler-than-firmware ticks: {(diff < 0).sum()} ({100*(diff<0).mean():.0f}%)")
        print(f"  same-as-firmware ticks:     {(diff == 0).sum()} ({100*(diff==0).mean():.0f}%)")
        print(f"  warmer-than-firmware ticks: {(diff > 0).sum()} ({100*(diff>0).mean():.0f}%)")
        print(f"  mean diff: {diff.mean():+.2f}, max={diff.max():.0f}, min={diff.min():.0f}")

    print("\n=== correction reasons ===")
    if "right_v52_reason" in df.columns:
        print(df["right_v52_reason"].value_counts().to_string())

    print("\n=== BedJet-window vs post-window distribution ===")
    if "in_bedjet_window" in df.columns:
        in_window = df["in_bedjet_window"].fillna(False)
        print(f"  in BedJet window: {in_window.sum()} ticks")
        print(f"  post-window:      {(~in_window).sum()} ticks")
        if in_window.any():
            corr_in = df.loc[in_window, "right_v52_correction"]
            print(f"  in-window corrections: should all be 0 → "
                  f"{(corr_in == 0).all()} (counter-examples: {(corr_in != 0).sum()})")

    print("\n=== per cycle: shadow proposed setting (mean), firmware setting (mean) ===")
    if "cycle" in df.columns:
        agg = df.groupby("cycle").agg(
            n=("right_v52_proposed", "size"),
            shadow_mean=("right_v52_proposed", "mean"),
            firmware_mean=("firmware_setting", "mean"),
            body_avg_mean=("body_avg", "mean"),
            firmware_blower_mean=("firmware_blower", "mean"),
        ).round(2)
        print(agg.to_string())

    print("\n=== ticks where shadow strongly disagrees with firmware (|diff|>=3) ===")
    strong = df[df["right_v52_diff_vs_firmware"].abs() >= 3]
    if not strong.empty:
        cols = ["ts", "cycle", "body_avg", "firmware_setting",
                "firmware_blower", "right_v52_proposed",
                "right_v52_correction", "right_v52_reason"]
        print(strong[cols].to_string(index=False))
    else:
        print("(none)")

    if args.csv_out:
        df.to_csv(args.csv_out, index=False)
        print(f"\nFull table written to {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
