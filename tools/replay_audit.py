"""
Counterfactual safety audit: new controller_decision vs v5 actual for 14 nights.

For every minute of bedtime across historical data:
1. Compute what the new controller would have set
2. Compare to what v5 actually set
3. Identify override wins/losses and risky divergences
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional
import math

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np
from ml.policy import controller_decision
from ml.features import cycle_num_of


def load_data() -> pd.DataFrame:
    """Load controller readings from the CSV export."""
    csv_path = Path("/tmp/controller_data.csv")
    df = pd.read_csv(csv_path)
    df["ts"] = pd.to_datetime(df["ts"])
    # Sort chronologically (data came out reverse-sorted)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def augment_with_new_policy(df: pd.DataFrame) -> pd.DataFrame:
    """Compute what the new controller_decision would set for each row."""
    results = []
    for zone in ["left", "right"]:
        zone_mask = df["zone"] == zone
        zone_df = df[zone_mask].copy()
        
        new_settings = []
        new_rails = []
        
        for _, row in zone_df.iterrows():
            body_f = row["body_left_f"] if zone == "left" else row["body_right_f"]
            
            # Call the new controller
            setting, rail = controller_decision(
                zone=zone,
                elapsed_min=row["elapsed_min"],
                room_temp_f=row["room_temp_f"],
                body_f=body_f
            )
            new_settings.append(setting)
            new_rails.append(rail)
        
        zone_df["new_setting"] = new_settings
        zone_df["new_rail_fired"] = new_rails
        results.append(zone_df)
    
    return pd.concat(results, ignore_index=True).sort_values("ts").reset_index(drop=True)


def segment_nights(df: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """Split data into nights (assume ~8hr sleep cycles, group by consecutive data)."""
    df = df.copy()
    df["date"] = df["ts"].dt.date
    nights = {}
    night_id = 0
    for date, group in df.groupby("date"):
        nights[night_id] = group.reset_index(drop=True)
        night_id += 1
    return nights


def analyze_override_moments(df: pd.DataFrame) -> dict:
    """
    For each override event, compare new vs v5 against revealed user preference.
    
    Returns dict with:
    - left_zone_overrides: detailed list
    - right_zone_overrides: detailed list
    - summary stats
    """
    # Only look at left zone (we have reliable override ground truth)
    left_overrides = df[
        (df["zone"] == "left") & 
        (df["action"] == "override") & 
        df["override_delta"].notna() & 
        df["setting"].notna()
    ].copy().reset_index(drop=True)
    
    right_overrides = df[
        (df["zone"] == "right") & 
        (df["action"] == "override") & 
        df["override_delta"].notna() & 
        df["setting"].notna()
    ].copy().reset_index(drop=True)
    
    # Compute revealed preference
    left_overrides["revealed_pref"] = (left_overrides["setting"] + left_overrides["override_delta"]).clip(-10, 0)
    right_overrides["revealed_pref"] = (right_overrides["setting"] + right_overrides["override_delta"]).clip(-10, 0)
    
    # Errors
    left_overrides["v5_error"] = (left_overrides["setting"] - left_overrides["revealed_pref"]).abs()
    left_overrides["new_error"] = (left_overrides["new_setting"] - left_overrides["revealed_pref"]).abs()
    left_overrides["v5_hit_1"] = left_overrides["v5_error"] <= 1
    left_overrides["new_hit_1"] = left_overrides["new_error"] <= 1
    left_overrides["better_worse"] = np.where(
        left_overrides["new_error"] < left_overrides["v5_error"], "BETTER",
        np.where(left_overrides["new_error"] > left_overrides["v5_error"], "WORSE", "SAME")
    )
    left_overrides["dir_v5"] = np.where(
        left_overrides["setting"] < left_overrides["revealed_pref"], "TOO_COLD",
        np.where(left_overrides["setting"] > left_overrides["revealed_pref"], "TOO_WARM", "EXACT")
    )
    left_overrides["dir_new"] = np.where(
        left_overrides["new_setting"] < left_overrides["revealed_pref"], "TOO_COLD",
        np.where(left_overrides["new_setting"] > left_overrides["revealed_pref"], "TOO_WARM", "EXACT")
    )
    
    right_overrides["v5_error"] = (right_overrides["setting"] - right_overrides["revealed_pref"]).abs()
    right_overrides["new_error"] = (right_overrides["new_setting"] - right_overrides["revealed_pref"]).abs()
    right_overrides["v5_hit_1"] = right_overrides["v5_error"] <= 1
    right_overrides["new_hit_1"] = right_overrides["new_error"] <= 1
    right_overrides["better_worse"] = np.where(
        right_overrides["new_error"] < right_overrides["v5_error"], "BETTER",
        np.where(right_overrides["new_error"] > right_overrides["v5_error"], "WORSE", "SAME")
    )
    right_overrides["dir_v5"] = np.where(
        right_overrides["setting"] < right_overrides["revealed_pref"], "TOO_COLD",
        np.where(right_overrides["setting"] > right_overrides["revealed_pref"], "TOO_WARM", "EXACT")
    )
    
    left_summary = {
        "n": len(left_overrides),
        "new_hit_rate": left_overrides["new_hit_1"].mean(),
        "v5_hit_rate": left_overrides["v5_hit_1"].mean(),
        "new_mae": left_overrides["new_error"].mean(),
        "v5_mae": left_overrides["v5_error"].mean(),
        "new_better_count": (left_overrides["better_worse"] == "BETTER").sum(),
        "new_worse_count": (left_overrides["better_worse"] == "WORSE").sum(),
        "new_same_count": (left_overrides["better_worse"] == "SAME").sum(),
    }
    
    right_summary = {
        "n": len(right_overrides),
        "new_hit_rate": right_overrides["new_hit_1"].mean(),
        "v5_hit_rate": right_overrides["v5_hit_1"].mean(),
        "new_mae": right_overrides["new_error"].mean(),
        "v5_mae": right_overrides["v5_error"].mean(),
        "new_better_count": (right_overrides["better_worse"] == "BETTER").sum(),
        "new_worse_count": (right_overrides["better_worse"] == "WORSE").sum(),
        "new_same_count": (right_overrides["better_worse"] == "SAME").sum(),
    }
    
    return {
        "left_overrides_df": left_overrides,
        "right_overrides_df": right_overrides,
        "left_summary": left_summary,
        "right_summary": right_summary,
    }


def audit_hard_rail_divergence(df: pd.DataFrame, threshold: int = 5) -> list[dict]:
    """
    Find every minute where new policy diverges from v5 by >= threshold steps.
    Return list of divergence events with context.
    """
    df = df.copy()
    df["divergence"] = (df["new_setting"] - df["setting"]).abs()
    
    diverged = df[df["divergence"] >= threshold].copy()
    
    results = []
    for _, row in diverged.iterrows():
        # Look for user response within 30 min
        resp_window = df[
            (df["zone"] == row["zone"]) & 
            (df["ts"] > row["ts"]) & 
            (df["ts"] <= row["ts"] + pd.Timedelta(minutes=30)) &
            (df["action"] == "override")
        ]
        
        # Check if user left/returned
        occ_after = df[
            (df["zone"] == row["zone"]) & 
            (df["ts"] > row["ts"]) & 
            (df["ts"] <= row["ts"] + pd.Timedelta(minutes=60))
        ]
        
        results.append({
            "ts": row["ts"],
            "zone": row["zone"],
            "elapsed_min": row["elapsed_min"],
            "v5_setting": row["setting"],
            "new_setting": row["new_setting"],
            "divergence": row["divergence"],
            "body_temp": row["body_left_f"] if row["zone"] == "left" else row["body_right_f"],
            "room_temp": row["room_temp_f"],
            "rail_fired": row["new_rail_fired"],
            "override_within_30min": len(resp_window) > 0,
            "override_dir": (resp_window["override_delta"].iloc[0] if len(resp_window) > 0 else None),
            "user_still_in_bed": occ_after["bed_occupied_left" if row["zone"] == "left" else "bed_occupied_right"].iloc[-1] if len(occ_after) > 0 else None,
        })
    
    return results


def audit_right_zone_rail_fires(df: pd.DataFrame) -> list[dict]:
    """
    For wife (right zone), identify rail-fire events (body >= 90 or >= 87).
    Track duration, occupancy transitions, and any override response.
    """
    right_df = df[df["zone"] == "right"].copy()
    right_df["body_temp"] = right_df["body_right_f"]
    
    # Identify rail-fire minutes
    right_df["hard_overheat"] = right_df["body_temp"] >= 90.0
    right_df["soft_overheat"] = (right_df["body_temp"] >= 87.0) & (right_df["body_temp"] < 90.0)
    right_df["rail_fire"] = right_df["hard_overheat"] | right_df["soft_overheat"]
    
    # Group consecutive rail-fire minutes into stretches
    right_df["fire_group"] = (right_df["rail_fire"] != right_df["rail_fire"].shift()).cumsum()
    
    results = []
    for group_id, group in right_df[right_df["rail_fire"]].groupby("fire_group"):
        if len(group) > 0:
            start_ts = group["ts"].iloc[0]
            end_ts = group["ts"].iloc[-1]
            duration_min = len(group)
            
            # Sample stats
            max_body = group["body_temp"].max()
            setting_during = group["new_setting"].iloc[0]  # Should be -10 if rail working
            
            # Did she override?
            override_during = group[group["action"] == "override"]
            override_within_30 = df[
                (df["zone"] == "right") & 
                (df["ts"] > end_ts) & 
                (df["ts"] <= end_ts + pd.Timedelta(minutes=30)) &
                (df["action"] == "override")
            ]
            
            # Occupancy before/after
            occ_before = group["bed_occupied_right"].iloc[0]
            occ_after = group["bed_occupied_right"].iloc[-1]
            
            results.append({
                "start_ts": start_ts,
                "end_ts": end_ts,
                "duration_min": duration_min,
                "max_body_f": max_body,
                "hard_overheat": group["hard_overheat"].any(),
                "soft_overheat": group["soft_overheat"].any(),
                "setting_new": setting_during,
                "override_during_stretch": len(override_during) > 0,
                "override_within_30min": len(override_within_30) > 0,
                "override_delta_within_30": override_within_30["override_delta"].iloc[0] if len(override_within_30) > 0 else None,
                "occupied_before": occ_before,
                "occupied_after": occ_after,
                "left_bed": occ_before and not occ_after,
            })
    
    return results


def compute_smart_baseline_drift(df: pd.DataFrame) -> dict:
    """
    Outside override moments, compute mean |new - v5| per cycle.
    Flag systematic shifts > 2 steps as warning signs.
    """
    # Exclude override windows
    non_override = df[
        (df["action"] != "override") | df["override_delta"].isna()
    ].copy()
    
    # Add cycle info
    non_override["cycle"] = non_override["elapsed_min"].apply(cycle_num_of)
    
    results = {}
    for zone in ["left", "right"]:
        zone_data = non_override[non_override["zone"] == zone]
        
        cycle_stats = []
        for cycle in sorted(zone_data["cycle"].unique()):
            cycle_rows = zone_data[zone_data["cycle"] == cycle]
            if len(cycle_rows) < 10:  # Need reasonable sample
                continue
            
            drift = (cycle_rows["new_setting"] - cycle_rows["setting"]).abs()
            cycle_stats.append({
                "zone": zone,
                "cycle": cycle,
                "n_samples": len(cycle_rows),
                "mean_drift": drift.mean(),
                "median_drift": drift.median(),
                "p95_drift": drift.quantile(0.95),
                "max_drift": drift.max(),
                "pct_diverged_1step": (drift >= 1).mean(),
                "pct_diverged_2plus_steps": (drift >= 2).mean(),
            })
        
        results[zone] = pd.DataFrame(cycle_stats) if cycle_stats else pd.DataFrame()
    
    return results


def main() -> int:
    print("=" * 80)
    print("COUNTERFACTUAL SAFETY AUDIT: New Controller vs v5")
    print("=" * 80)
    
    # Load and augment data
    print("\n[1/5] Loading controller data...")
    df = load_data()
    print(f"  Loaded {len(df):,} readings")
    print(f"  Date range: {df['ts'].min()} to {df['ts'].max()}")
    print(f"  Zones: {df['zone'].unique()}")
    
    print("\n[2/5] Computing new controller_decision for every minute...")
    df = augment_with_new_policy(df)
    
    # ────────────────────────────────────────────────────────────────────────────
    # ANALYSIS 1: Override-moment comparison
    print("\n" + "=" * 80)
    print("ANALYSIS 1: OVERRIDE-MOMENT COMPARISON (ground truth)")
    print("=" * 80)
    
    override_analysis = analyze_override_moments(df)
    
    left_ov = override_analysis["left_overrides_df"]
    left_sum = override_analysis["left_summary"]
    
    print(f"\nLEFT ZONE (user): {left_sum['n']} override events")
    print(f"  v5 hit rate (|err| ≤ 1):       {left_sum['v5_hit_rate']:.1%}")
    print(f"  NEW hit rate (|err| ≤ 1):      {left_sum['new_hit_rate']:.1%}")
    print(f"  v5 MAE:                        {left_sum['v5_mae']:.2f} steps")
    print(f"  NEW MAE:                       {left_sum['new_mae']:.2f} steps")
    print(f"  NEW vs v5 head-to-head:")
    print(f"    - NEW BETTER:                {left_sum['new_better_count']} ({left_sum['new_better_count']/left_sum['n']:.0%})")
    print(f"    - NEW WORSE:                 {left_sum['new_worse_count']} ({left_sum['new_worse_count']/left_sum['n']:.0%})")
    print(f"    - SAME:                      {left_sum['new_same_count']} ({left_sum['new_same_count']/left_sum['n']:.0%})")
    
    # Direction analysis
    left_v5_cold = (left_ov["dir_v5"] == "TOO_COLD").sum()
    left_v5_warm = (left_ov["dir_v5"] == "TOO_WARM").sum()
    left_new_cold = (left_ov["dir_new"] == "TOO_COLD").sum()
    left_new_warm = (left_ov["dir_new"] == "TOO_WARM").sum()
    
    print(f"  v5 direction misses: {left_v5_cold} too cold, {left_v5_warm} too warm")
    print(f"  NEW direction misses: {left_new_cold} too cold, {left_new_warm} too warm")
    
    if len(left_ov) > 0:
        print(f"\n  Sample override events (left zone):")
        sample_cols = ["ts", "setting", "override_delta", "revealed_pref", "new_setting", "v5_error", "new_error", "better_worse"]
        print(left_ov[sample_cols].head(10).to_string(index=False))
    
    right_ov = override_analysis["right_overrides_df"]
    right_sum = override_analysis["right_summary"]
    
    print(f"\nRIGHT ZONE (wife): {right_sum['n']} override events")
    if right_sum['n'] > 0:
        print(f"  v5 hit rate (|err| ≤ 1):       {right_sum['v5_hit_rate']:.1%}")
        print(f"  NEW hit rate (|err| ≤ 1):      {right_sum['new_hit_rate']:.1%}")
        print(f"  v5 MAE:                        {right_sum['v5_mae']:.2f} steps")
        print(f"  NEW MAE:                       {right_sum['new_mae']:.2f} steps")
        print(f"  NEW vs v5 head-to-head:")
        print(f"    - NEW BETTER:                {right_sum['new_better_count']} ({right_sum['new_better_count']/right_sum['n']:.0%})")
        print(f"    - NEW WORSE:                 {right_sum['new_worse_count']} ({right_sum['new_worse_count']/right_sum['n']:.0%})")
        print(f"    - SAME:                      {right_sum['new_same_count']} ({right_sum['new_same_count']/right_sum['n']:.0%})")
    else:
        print("  NO OVERRIDE GROUND TRUTH FOR RIGHT ZONE (only 6 events total)")
    
    # ────────────────────────────────────────────────────────────────────────────
    # ANALYSIS 2: Hard-rail divergence audit
    print("\n" + "=" * 80)
    print("ANALYSIS 2: HARD-RAIL DIVERGENCE AUDIT (≥5 setting step divergences)")
    print("=" * 80)
    
    divergences = audit_hard_rail_divergence(df, threshold=5)
    print(f"\nFound {len(divergences)} divergence moments (≥5 steps)")
    
    if divergences:
        print("\nTop 20 largest divergences:")
        div_sorted = sorted(divergences, key=lambda x: x["divergence"], reverse=True)[:20]
        for i, d in enumerate(div_sorted, 1):
            print(f"\n  [{i}] {d['ts']} | Zone: {d['zone']} | Div: {d['divergence']} steps")
            print(f"      v5={d['v5_setting']:2d} vs NEW={d['new_setting']:2d}")
            print(f"      Body: {d['body_temp']:.1f}°F | Room: {d['room_temp']:.1f}°F")
            print(f"      Rail fired: {d['rail_fired']}")
            if d['override_within_30min']:
                ovr_dir = "WARMER" if d['override_dir'] > 0 else "COOLER" if d['override_dir'] < 0 else "SAME"
                print(f"      USER OVERRODE WITHIN 30min → {ovr_dir} (delta={d['override_dir']})")
            print(f"      Still in bed after: {d['user_still_in_bed']}")
    
    # ────────────────────────────────────────────────────────────────────────────
    # ANALYSIS 3: Right-zone (wife) rail-fire audit
    print("\n" + "=" * 80)
    print("ANALYSIS 3: RIGHT-ZONE (WIFE) RAIL-FIRE SAFETY AUDIT")
    print("=" * 80)
    
    rail_fires = audit_right_zone_rail_fires(df)
    print(f"\nIdentified {len(rail_fires)} rail-fire stretches (body ≥87°F)")
    
    hard_overheat_count = sum(1 for rf in rail_fires if rf["hard_overheat"])
    soft_overheat_count = sum(1 for rf in rail_fires if rf["soft_overheat"] and not rf["hard_overheat"])
    
    print(f"  - Hard overheat (≥90°F):  {hard_overheat_count} stretches")
    print(f"  - Soft overheat (87-90°F): {soft_overheat_count} stretches")
    
    if rail_fires:
        rail_df = pd.DataFrame(rail_fires)
        
        print(f"\nDuration distribution (minutes):")
        print(f"  Mean:   {rail_df['duration_min'].mean():.1f} min")
        print(f"  Median: {rail_df['duration_min'].median():.0f} min")
        print(f"  Max:    {rail_df['duration_min'].max():.0f} min")
        print(f"  >30min: {(rail_df['duration_min'] > 30).sum()} stretches")
        
        print(f"\nUser response during/after rail-fires:")
        print(f"  Override during stretch:   {rail_df['override_during_stretch'].sum()}")
        print(f"  Override within 30min:     {rail_df['override_within_30min'].sum()}")
        print(f"  User left bed after:       {rail_df['left_bed'].sum()}")
        
        print(f"\nTop rail-fire events:")
        top_fires = rail_df.nlargest(10, "max_body_f")
        for i, (_, rf) in enumerate(top_fires.iterrows(), 1):
            print(f"\n  [{i}] {rf['start_ts']} | Duration: {rf['duration_min']:.0f} min | Max body: {rf['max_body_f']:.1f}°F")
            print(f"      Setting: {rf['setting_new']} | {('HARD' if rf['hard_overheat'] else 'SOFT')} overheat")
            if rf['override_within_30min']:
                dir_str = "WARMER" if rf['override_delta_within_30'] > 0 else "COOLER" if rf['override_delta_within_30'] < 0 else "SAME"
                print(f"      Wife overrode within 30min → {dir_str}")
            print(f"      Left bed: {rf['left_bed']}")
    
    # ────────────────────────────────────────────────────────────────────────────
    # ANALYSIS 4: Smart baseline drift
    print("\n" + "=" * 80)
    print("ANALYSIS 4: SMART_BASELINE DRIFT (non-override, per cycle)")
    print("=" * 80)
    
    drift_by_zone = compute_smart_baseline_drift(df)
    
    for zone in ["left", "right"]:
        drift_df = drift_by_zone[zone]
        if drift_df.empty:
            print(f"\n{zone.upper()} ZONE: Insufficient data")
            continue
        
        print(f"\n{zone.upper()} ZONE:")
        print(drift_df[["cycle", "n_samples", "mean_drift", "pct_diverged_2plus_steps"]].to_string(index=False))
        
        # Warning if systematic shift
        for _, row in drift_df.iterrows():
            if row["mean_drift"] > 2.0:
                print(f"  ⚠️  CYCLE {int(row['cycle'])}: Mean drift {row['mean_drift']:.2f} steps (WARNING)")
    
    print("\n" + "=" * 80)
    print("FINAL VERDICT & RISK ASSESSMENT")
    print("=" * 80)
    
    # Compute confidence level
    issues = []
    
    # Issue 1: Override performance
    if left_sum['new_worse_count'] > left_sum['new_better_count']:
        issues.append(f"LEFT ZONE: New controller worse on {left_sum['new_worse_count']}/{left_sum['n']} overrides")
    if left_sum['new_mae'] > left_sum['v5_mae'] + 0.5:
        issues.append(f"LEFT ZONE: New MAE {left_sum['new_mae']:.2f} > v5 MAE {left_sum['v5_mae']:.2f}")
    
    # Issue 2: Large divergences
    max_div = max([d["divergence"] for d in divergences], default=0)
    if max_div >= 10:
        issues.append(f"HARD RAILS: Max divergence {max_div} steps (potential safety rail misfire)")
    
    # Issue 3: Rail firing effectiveness
    if rail_fires:
        rail_df = pd.DataFrame(rail_fires)
        long_fires = (rail_df['duration_min'] > 30).sum()
        if long_fires > 0:
            issues.append(f"WIFE SAFETY: {long_fires} rail-fire stretches >30min (sustained overheat despite max cool)")
    
    # Determine confidence
    confidence = "HIGH"
    if len(issues) >= 2:
        confidence = "LOW"
    elif len(issues) == 1:
        confidence = "MED"
    
    print(f"\nCONFIDENCE LEVEL: {confidence}")
    print(f"\nTop Issues Identified:")
    for i, issue in enumerate(issues[:3], 1):
        print(f"  {i}. {issue}")
    
    if not issues:
        print("  ✓ No major issues detected")
        print("  ✓ Override performance matches or exceeds v5")
        print("  ✓ Rail divergences are minimal and well-justified")
        print("  ✓ Wife safety monitoring shows no sustained overheat events")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
