"""
Analyze why new controller performs WORSE on left zone overrides.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np
from ml.policy import controller_decision


def main() -> int:
    csv_path = Path("/tmp/controller_data.csv")
    df = pd.read_csv(csv_path)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    
    left_ov = df[
        (df["zone"] == "left") & 
        (df["action"] == "override") & 
        df["override_delta"].notna() & 
        df["setting"].notna()
    ].copy().reset_index(drop=True)
    
    left_ov["revealed_pref"] = (left_ov["setting"] + left_ov["override_delta"]).clip(-10, 0)
    
    new_settings = []
    rails = []
    for _, row in left_ov.iterrows():
        new_setting, rail = controller_decision(
            zone="left",
            elapsed_min=row["elapsed_min"],
            room_temp_f=row["room_temp_f"],
            body_f=row["body_left_f"]
        )
        new_settings.append(new_setting)
        rails.append(rail)
    
    left_ov["new_setting"] = new_settings
    left_ov["rail_fired"] = rails
    
    left_ov["v5_error"] = (left_ov["setting"] - left_ov["revealed_pref"]).abs()
    left_ov["new_error"] = (left_ov["new_setting"] - left_ov["revealed_pref"]).abs()
    left_ov["better_worse"] = np.where(
        left_ov["new_error"] < left_ov["v5_error"], "BETTER",
        np.where(left_ov["new_error"] > left_ov["v5_error"], "WORSE", "SAME")
    )
    
    print("=" * 80)
    print("LEFT ZONE OVERRIDE PERFORMANCE ANALYSIS")
    print("=" * 80)
    
    print(f"\nTotal overrides: {len(left_ov)}")
    print(f"  BETTER: {(left_ov['better_worse'] == 'BETTER').sum()}")
    print(f"  WORSE:  {(left_ov['better_worse'] == 'WORSE').sum()}")
    print(f"  SAME:   {(left_ov['better_worse'] == 'SAME').sum()}")
    
    worse = left_ov[left_ov["better_worse"] == "WORSE"]
    
    print(f"\n{len(worse)} WORSE cases:")
    for i, (_, row) in enumerate(worse.iterrows(), 1):
        print(f"\n  [{i}] {row['ts']}")
        print(f"      Setting: v5={int(row['setting']):2d} | Pref={int(row['revealed_pref']):2d} | NEW={int(row['new_setting']):2d}")
        print(f"      Error: v5={row['v5_error']:.0f} vs NEW={row['new_error']:.0f}")
        print(f"      Body: {row['body_left_f']:.1f}°F | Room: {row['room_temp_f']:.1f}°F | Elapsed: {row['elapsed_min']:.0f}min")
        print(f"      Rail fired: {row['rail_fired']}")
        print(f"      Override delta: {row['override_delta']:+.0f}")
    
    print(f"\n" + "=" * 80)
    print("PERFORMANCE BY CYCLE")
    print("=" * 80)
    
    left_ov["cycle"] = (left_ov["elapsed_min"] // 90).astype(int) + 1
    
    for cycle in sorted(left_ov["cycle"].unique()):
        cycle_ov = left_ov[left_ov["cycle"] == cycle]
        n_worse = (cycle_ov["better_worse"] == "WORSE").sum()
        n_better = (cycle_ov["better_worse"] == "BETTER").sum()
        n_same = (cycle_ov["better_worse"] == "SAME").sum()
        print(f"\nCycle {cycle}: {len(cycle_ov)} overrides")
        print(f"  BETTER: {n_better:2d} | WORSE: {n_worse:2d} | SAME: {n_same:2d}")
        print(f"  Mean error v5:  {cycle_ov['v5_error'].mean():.2f}")
        print(f"  Mean error NEW: {cycle_ov['new_error'].mean():.2f}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
