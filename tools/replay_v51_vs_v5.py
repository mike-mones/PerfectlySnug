"""
Counterfactual replay: v5 vs v5.1 cycle baselines at every left-zone
override event. The 2026-04-30 ship report claimed in-sample MAE 2.939 →
2.755 and signed bias −1.92 → −1.41 vs v5. This script verifies those
numbers by computing both candidates over the live override corpus
end-to-end, in the same way the controller actually uses CYCLE_SETTINGS.

NOT a hold-out test (in-sample by design — same as v5's "in-sample" comparison).
For held-out LOOCV see tools/v5_1_baseline_sweep.py.

Run:
    cd PerfectlySnug && .venv/bin/python tools/replay_v51_vs_v5.py
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from ml import data_io


V5_BASELINES   = {1: -10, 2: -9, 3: -8, 4: -7, 5: -6, 6: -5}
V5_1_BASELINES = {1: -10, 2: -10, 3: -7, 4: -5, 5: -5, 6: -6}
CYCLE_DURATION_MIN = 90


def cycle_of(elapsed_min: float) -> int:
    return max(1, min(6, int(elapsed_min // CYCLE_DURATION_MIN) + 1))


def score(name: str, baselines: dict, ov: pd.DataFrame) -> dict:
    pred = ov["cycle"].map(baselines).astype(float)
    err = (pred - ov["user_pref"]).abs()
    bias = (pred - ov["user_pref"])
    return {
        "name": name,
        "n": len(ov),
        "mae": float(err.mean()),
        "hit_rate": float((err <= 1).mean()),
        "bias": float(bias.mean()),
        "max_err": float(err.max()),
        "per_cycle_mae": ov.assign(err=err).groupby("cycle")["err"].mean().round(3).to_dict(),
        "per_cycle_bias": ov.assign(b=bias).groupby("cycle")["b"].mean().round(3).to_dict(),
        "per_cycle_hit": ov.assign(h=(err <= 1)).groupby("cycle")["h"].mean().round(3).to_dict(),
    }


def main() -> int:
    rd = data_io.load_readings()
    ov = rd[(rd["action"] == "override")
            & rd["override_delta"].notna()
            & rd["setting"].notna()
            & rd["elapsed_min"].notna()].copy()
    ov["user_pref"] = (ov["setting"] + ov["override_delta"]).clip(-10, 0)
    ov["cycle"] = ov["elapsed_min"].apply(cycle_of)

    print(f"Loaded {len(ov)} left-zone overrides\n")

    v5  = score("v5",   V5_BASELINES,   ov)
    v51 = score("v5.1", V5_1_BASELINES, ov)

    print(f"{'metric':<14} {'v5':>10} {'v5.1':>10} {'delta':>10}")
    for k in ("n", "mae", "hit_rate", "bias", "max_err"):
        d = v51[k] - v5[k]
        a = v5[k]; b = v51[k]
        if isinstance(a, float):
            print(f"{k:<14} {a:>10.3f} {b:>10.3f} {d:>+10.3f}")
        else:
            print(f"{k:<14} {a:>10} {b:>10} {'-':>10}")

    print("\nPer-cycle MAE:")
    print(f"{'cycle':<6} {'n':>4} {'pref_mean':>10} {'v5':>6} {'v5.1':>6} {'v5_mae':>7} {'v51_mae':>8} {'mae_delta':>10}")
    for c in sorted(ov["cycle"].unique()):
        sub = ov[ov["cycle"] == c]
        pref = sub["user_pref"].mean()
        v5b = V5_BASELINES[c]; v51b = V5_1_BASELINES[c]
        v5m = v5["per_cycle_mae"].get(c, float("nan"))
        v51m = v51["per_cycle_mae"].get(c, float("nan"))
        print(f"{c:<6} {len(sub):>4} {pref:>10.2f} {v5b:>6} {v51b:>6} {v5m:>7.3f} {v51m:>8.3f} {v51m - v5m:>+10.3f}")

    # Verdict
    print("\n— Headline claims from 2026-04-30 ship report —")
    print(f"In-sample MAE: claimed 2.939 → 2.755   actual {v5['mae']:.3f} → {v51['mae']:.3f}")
    print(f"Signed bias:   claimed −1.92 → −1.41    actual {v5['bias']:+.3f} → {v51['bias']:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
