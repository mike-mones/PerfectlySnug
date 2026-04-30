"""Compare fitted smart_baseline vs v5's actual settings at override events.

The standard 'comfort' metric in training.py is biased toward v5: outside
override windows, the 'preferred' setting defaults to v5's own actual
setting, so any controller that differs from v5 loses points by construction.

The fair question is: at the moments the user expressed a real preference
(override events), which controller's setting was closer to that preference?
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from ml import data_io
from ml.features import smart_baseline, cycle_num_of


def main() -> int:
    rd = data_io.load_readings()
    ov = rd[(rd["action"] == "override")
            & rd["override_delta"].notna()
            & rd["setting"].notna()
            & rd["elapsed_min"].notna()].copy()
    ov["user_pref"] = (ov["setting"] + ov["override_delta"]).clip(-10, 0)
    ov["v5_setting"] = ov["setting"]
    ov["smart_setting"] = ov.apply(
        lambda r: smart_baseline(r["elapsed_min"], r["room_temp_f"]), axis=1)
    ov["cycle"] = ov["elapsed_min"].apply(cycle_num_of)
    ov["v5_err"] = (ov["v5_setting"] - ov["user_pref"]).abs()
    ov["smart_err"] = (ov["smart_setting"] - ov["user_pref"]).abs()
    ov["smart_better"] = ov["smart_err"] < ov["v5_err"]
    ov["v5_better"] = ov["v5_err"] < ov["smart_err"]
    ov["v5_hit"] = ov["v5_err"] <= 1
    ov["smart_hit"] = ov["smart_err"] <= 1

    print(f"Overrides analyzed: {len(ov)}\n")
    print("Per cycle:")
    g = ov.groupby("cycle").agg(
        n=("user_pref", "size"),
        v5_avg=("v5_setting", "mean"),
        smart_avg=("smart_setting", "mean"),
        pref_avg=("user_pref", "mean"),
        v5_mae=("v5_err", "mean"),
        smart_mae=("smart_err", "mean"),
        v5_hit_rate=("v5_hit", "mean"),
        smart_hit_rate=("smart_hit", "mean"),
    ).round(2)
    print(g.to_string())

    print("\nOverall:")
    print(f"  Mean abs error v5:    {ov['v5_err'].mean():.2f}")
    print(f"  Mean abs error smart: {ov['smart_err'].mean():.2f}")
    print(f"  Hit rate (|err|<=1) v5:    {ov['v5_hit'].mean():.1%}")
    print(f"  Hit rate (|err|<=1) smart: {ov['smart_hit'].mean():.1%}")
    print(f"  Smart strictly closer than v5: {ov['smart_better'].mean():.1%}")
    print(f"  v5    strictly closer than smart: {ov['v5_better'].mean():.1%}")
    print(f"  Tie:                          {(ov['v5_err']==ov['smart_err']).mean():.1%}")

    print("\nDirection of v5's miss:")
    too_cold = (ov["v5_setting"] < ov["user_pref"]).sum()
    too_warm = (ov["v5_setting"] > ov["user_pref"]).sum()
    print(f"  v5 was too COLD (user wanted warmer): {too_cold} ({too_cold/len(ov):.0%})")
    print(f"  v5 was too WARM (user wanted cooler): {too_warm} ({too_warm/len(ov):.0%})")

    print("\nDirection of smart's miss:")
    sc = (ov["smart_setting"] < ov["user_pref"]).sum()
    sw = (ov["smart_setting"] > ov["user_pref"]).sum()
    print(f"  smart too COLD: {sc} ({sc/len(ov):.0%})")
    print(f"  smart too WARM: {sw} ({sw/len(ov):.0%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
