"""
Deep analysis of why long overheat stretches occur despite rail-firing.
Check: Is the rail actually being applied? Is the setting actually -10?
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np
from ml.policy import controller_decision
from ml.policy import BODY_OVERHEAT_HARD_F, BODY_OVERHEAT_SOFT_F


def main() -> int:
    csv_path = Path("/tmp/controller_data.csv")
    df = pd.read_csv(csv_path)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    
    # Focus on wife's long overheat stretch
    right_df = df[df["zone"] == "right"].copy()
    
    # 2026-04-28 01:19:22 → 85 min at 96.6°F max
    mask = (right_df["ts"] >= "2026-04-28 00:00") & (right_df["ts"] <= "2026-04-28 03:00")
    window = right_df[mask].copy()
    
    print("=" * 80)
    print("DEEP DIVE: Wife overheat stretch 2026-04-28 (85 min @ 96.6°F max)")
    print("=" * 80)
    
    for _, row in window.iterrows():
        body = row["body_right_f"]
        room = row["room_temp_f"]
        elapsed = row["elapsed_min"]
        v5_setting = row["setting"]
        
        # Compute what new policy should do
        new_setting, rail_name = controller_decision(
            zone="right",
            elapsed_min=elapsed,
            room_temp_f=room,
            body_f=body
        )
        
        status = ""
        if body >= BODY_OVERHEAT_HARD_F:
            status = f"HARD OVERHEAT ({body:.1f}°F ≥ {BODY_OVERHEAT_HARD_F}°F)"
        elif body >= BODY_OVERHEAT_SOFT_F:
            status = f"SOFT OVERHEAT ({body:.1f}°F ≥ {BODY_OVERHEAT_SOFT_F}°F)"
        
        print(f"{row['ts']} | Body: {body:5.1f}°F | Room: {room:5.1f}°F")
        print(f"  v5={v5_setting:2d} | NEW={new_setting:2d} | Rail: {rail_name if rail_name else 'NONE'}")
        print(f"  {status}")
        print()
    
    # Check if the settings are actually being clipped somehow
    print("\n" + "=" * 80)
    print("CHECK: Are rails being applied correctly?")
    print("=" * 80)
    
    # Manual check on first hard overheat row
    first_hard = right_df[right_df["body_right_f"] >= BODY_OVERHEAT_HARD_F].iloc[0]
    print(f"\nFirst hard overheat: {first_hard['ts']}")
    print(f"  body_right_f={first_hard['body_right_f']:.1f}°F")
    print(f"  room_temp_f={first_hard['room_temp_f']:.1f}°F")
    print(f"  elapsed_min={first_hard['elapsed_min']:.1f}")
    print(f"  v5 setting={first_hard['setting']}")
    
    new_set, rail = controller_decision(
        zone="right",
        elapsed_min=first_hard['elapsed_min'],
        room_temp_f=first_hard['room_temp_f'],
        body_f=first_hard['body_right_f']
    )
    print(f"  NEW controller_decision: setting={new_set}, rail={rail}")
    print(f"  Expected: setting=-10 (hard rail should fire)")
    
    # Count how many hard overheat readings happened with v5=-10 vs v5 < -7
    hard_overheat = right_df[right_df["body_right_f"] >= BODY_OVERHEAT_HARD_F]
    print(f"\nHard overheat events (body ≥ {BODY_OVERHEAT_HARD_F}°F):")
    print(f"  Total rows: {len(hard_overheat)}")
    print(f"  v5=-10 count: {(hard_overheat['setting'] == -10).sum()}")
    print(f"  v5<-7 count: {(hard_overheat['setting'] < -7).sum()}")
    print(f"  v5≥-7 count: {(hard_overheat['setting'] >= -7).sum()}")
    
    print(f"\nSoft overheat events (body 87-90°F):")
    soft_overheat = right_df[(right_df["body_right_f"] >= BODY_OVERHEAT_SOFT_F) & (right_df["body_right_f"] < BODY_OVERHEAT_HARD_F)]
    print(f"  Total rows: {len(soft_overheat)}")
    print(f"  v5=-10 count: {(soft_overheat['setting'] == -10).sum()}")
    print(f"  v5=-8 or -9 count: {(soft_overheat['setting'].isin([-8, -9])).sum()}")
    print(f"  v5≥-7 count: {(soft_overheat['setting'] >= -7).sum()}")
    
    # Distribution of v5 settings during overheat
    print(f"\nv5 setting distribution during any overheat (body ≥87°F):")
    any_overheat = right_df[right_df["body_right_f"] >= BODY_OVERHEAT_SOFT_F]
    print(any_overheat['setting'].value_counts().sort_index())
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
