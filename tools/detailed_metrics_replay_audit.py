"""
Comprehensive metrics dump for safety audit.
Exports detailed CSVs and statistics for manual review.
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
    
    print("Detailed Metrics Computation...")
    
    # Compute new settings
    new_settings = []
    rails = []
    for _, row in df.iterrows():
        body_f = row["body_left_f"] if row["zone"] == "left" else row["body_right_f"]
        new_setting, rail = controller_decision(
            zone=row["zone"],
            elapsed_min=row["elapsed_min"],
            room_temp_f=row["room_temp_f"],
            body_f=body_f
        )
        new_settings.append(new_setting)
        rails.append(rail)
    
    df["new_setting"] = new_settings
    df["rail_fired"] = rails
    df["divergence"] = (df["new_setting"] - df["setting"]).abs()
    
    # Export override moments
    left_ov = df[
        (df["zone"] == "left") & 
        (df["action"] == "override") & 
        df["override_delta"].notna() & 
        df["setting"].notna()
    ].copy()
    
    left_ov["revealed_pref"] = (left_ov["setting"] + left_ov["override_delta"]).clip(-10, 0)
    left_ov["v5_error"] = (left_ov["setting"] - left_ov["revealed_pref"]).abs()
    left_ov["new_error"] = (left_ov["new_setting"] - left_ov["revealed_pref"]).abs()
    left_ov["better_worse"] = np.where(
        left_ov["new_error"] < left_ov["v5_error"], "BETTER",
        np.where(left_ov["new_error"] > left_ov["v5_error"], "WORSE", "SAME")
    )
    
    cols_export = [
        "ts", "zone", "setting", "override_delta", "revealed_pref", "new_setting",
        "v5_error", "new_error", "better_worse", "body_left_f", "room_temp_f", 
        "elapsed_min", "rail_fired"
    ]
    left_ov[cols_export].to_csv("/tmp/left_overrides_detailed.csv", index=False)
    print(f"✓ Exported {len(left_ov)} left-zone overrides to left_overrides_detailed.csv")
    
    right_ov = df[
        (df["zone"] == "right") & 
        (df["action"] == "override") & 
        df["override_delta"].notna() & 
        df["setting"].notna()
    ].copy()
    
    if len(right_ov) > 0:
        right_ov["revealed_pref"] = (right_ov["setting"] + right_ov["override_delta"]).clip(-10, 0)
        right_ov["v5_error"] = (right_ov["setting"] - right_ov["revealed_pref"]).abs()
        right_ov["new_error"] = (right_ov["new_setting"] - right_ov["revealed_pref"]).abs()
        right_ov["better_worse"] = np.where(
            right_ov["new_error"] < right_ov["v5_error"], "BETTER",
            np.where(right_ov["new_error"] > right_ov["v5_error"], "WORSE", "SAME")
        )
        
        right_ov[cols_export].to_csv("/tmp/right_overrides_detailed.csv", index=False)
        print(f"✓ Exported {len(right_ov)} right-zone overrides to right_overrides_detailed.csv")
    
    # Export large divergences
    large_div = df[df["divergence"] >= 5].copy()
    large_div[["ts", "zone", "setting", "new_setting", "divergence", "body_left_f", 
               "body_right_f", "room_temp_f", "rail_fired", "elapsed_min"]].to_csv(
        "/tmp/large_divergences_5plus.csv", index=False
    )
    print(f"✓ Exported {len(large_div)} divergences ≥5 steps to large_divergences_5plus.csv")
    
    # Summary stats
    print("\n" + "=" * 70)
    print("SUMMARY STATISTICS")
    print("=" * 70)
    
    print(f"\nLeft zone overrides (n={len(left_ov)}):")
    print(f"  v5 MAE:                  {left_ov['v5_error'].mean():.2f}")
    print(f"  NEW MAE:                 {left_ov['new_error'].mean():.2f}")
    print(f"  Difference (NEW - v5):   {(left_ov['new_error'].mean() - left_ov['v5_error'].mean()):+.2f}")
    print(f"  NEW better than v5:      {(left_ov['better_worse'] == 'BETTER').sum()} ({(left_ov['better_worse'] == 'BETTER').mean():.0%})")
    print(f"  NEW worse than v5:       {(left_ov['better_worse'] == 'WORSE').sum()} ({(left_ov['better_worse'] == 'WORSE').mean():.0%})")
    
    if len(right_ov) > 0:
        print(f"\nRight zone overrides (n={len(right_ov)}):")
        print(f"  v5 MAE:                  {right_ov['v5_error'].mean():.2f}")
        print(f"  NEW MAE:                 {right_ov['new_error'].mean():.2f}")
        print(f"  NEW better than v5:      {(right_ov['better_worse'] == 'BETTER').sum()} ({(right_ov['better_worse'] == 'BETTER').mean():.0%})")
        print(f"  NEW worse than v5:       {(right_ov['better_worse'] == 'WORSE').sum()} ({(right_ov['better_worse'] == 'WORSE').mean():.0%})")
    
    print(f"\nLarge divergences (≥5 steps): {len(large_div)}")
    for zone in ["left", "right"]:
        zone_div = large_div[large_div["zone"] == zone]
        if len(zone_div) > 0:
            print(f"  {zone:5s}: {len(zone_div)} divergences, max {zone_div['divergence'].max():.0f} steps")
    
    # Body temp stats on right zone
    right_df = df[df["zone"] == "right"].copy()
    high_body = right_df[right_df["body_right_f"] >= 90.0]
    print(f"\nRight zone body temp ≥90°F (hard overheat threshold):")
    print(f"  Minutes: {len(high_body)}")
    print(f"  Max body temp: {right_df['body_right_f'].max():.1f}°F")
    print(f"  v5 settings during overheat: {high_body['setting'].value_counts().to_dict()}")
    print(f"  NEW settings during overheat: {high_body['new_setting'].value_counts().to_dict()}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
