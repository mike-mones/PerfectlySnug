"""
Fit per-cycle baselines and room-temp compensation from logged override
events using a small-sample Bayesian shrinkage estimator.

Why this exists
---------------
Empirical analysis of v5_rc_off override events shows two structural
problems with v5's hand-picked constants:

  1. Cycle baselines (1:-10 .. 6:-5) over-cool cycles 3-6 by 2-4 steps
     vs the user's revealed preferences.
  2. Room-comp adds cooling whenever room > 68°F, but the user's bedroom
     averages ~71°F and the user actually requests *warmer* settings in
     67-70°F rooms 13:4. The hot-room slope is wrong for this user.

Rather than hand-tuning new constants, this fitter:
  * Estimates per-cycle posterior mean of user-preferred setting using
    a Normal-Normal shrinkage model with the current v5 baseline as
    prior mean and configurable prior strength.
  * Fits a per-room-band slope only when there is statistically
    meaningful evidence (>=N overrides in the band, |slope|>threshold).
    Otherwise zeros out room comp so it can't actively harm the user.
  * Writes results to ml/state/fitted_baselines.json. features.py loads
    this file if present and falls back to v5 constants otherwise.

Run:
    cd PerfectlySnug && .venv/bin/python tools/fit_baselines.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ml import data_io
from ml.features import CYCLE_BASELINES, CYCLE_DURATION_MIN

OUT_PATH = ROOT / "ml" / "state" / "fitted_baselines.json"

# Shrinkage prior: pretend we have N "virtual" observations at the v5 baseline.
# With ~6 overrides per cycle, prior_n=1 gives data ~85% weight in the posterior.
# We use a weak prior because v5's hand-picked baselines were demonstrably wrong
# in late cycles; a strong prior just pulls fitted values back toward bad defaults.
PRIOR_N = 1.0

# Room comp: only trust a slope if we have this much evidence in a band
# *and* the band's mean preference differs from the global mean by at least
# this much. Otherwise we set the slope to 0 (no compensation).
MIN_OVERRIDES_PER_BAND = 3
MIN_BAND_DELTA_FOR_SLOPE = 0.75


def cycle_of(elapsed_min: float) -> int:
    cn = int(elapsed_min // CYCLE_DURATION_MIN) + 1
    return max(1, min(cn, max(CYCLE_BASELINES)))


def fit_cycle_baselines(overrides: pd.DataFrame) -> dict[int, dict]:
    """Posterior mean per cycle: (prior_n*mu_0 + n*xbar) / (prior_n + n)."""
    out = {}
    for c in sorted(CYCLE_BASELINES):
        prior_mu = float(CYCLE_BASELINES[c])
        sub = overrides[overrides["cycle_num"] == c]
        n = len(sub)
        if n == 0:
            posterior = prior_mu
            xbar = float("nan")
        else:
            xbar = float(sub["user_pref_setting"].mean())
            posterior = (PRIOR_N * prior_mu + n * xbar) / (PRIOR_N + n)
        # Snap to integer settings within the safety clamp.
        snapped = int(round(max(-10, min(0, posterior))))
        out[c] = {
            "prior_mu": prior_mu,
            "n_overrides": int(n),
            "sample_mean": xbar,
            "posterior_raw": round(posterior, 3),
            "fitted": snapped,
        }
    return out


def fit_room_comp(overrides: pd.DataFrame, fitted_cycles: dict[int, dict]
                  ) -> dict:
    """Decide per-band room-temp adjustment based on residual evidence.

    Residual = user_pref - fitted_cycle_baseline. If a band's mean residual
    differs from zero by more than MIN_BAND_DELTA_FOR_SLOPE *and* has enough
    samples, we record an adjustment for that band. Otherwise 0.
    """
    bands = [
        ("cold",     lambda r: r < 67.0),
        ("cool",     lambda r: 67.0 <= r < 69.0),
        ("neutral",  lambda r: 69.0 <= r < 72.0),
        ("warm",     lambda r: 72.0 <= r < 74.0),
        ("heat_on",  lambda r: r >= 74.0),
    ]
    o = overrides.copy()
    o["fitted_baseline"] = o["cycle_num"].map(lambda c: fitted_cycles[c]["fitted"])
    o["residual"] = o["user_pref_setting"] - o["fitted_baseline"]

    band_results = {}
    for name, pred in bands:
        mask = o["room_temp_f"].apply(lambda r: bool(pred(r)) if pd.notna(r) else False)
        sub = o[mask]
        n = len(sub)
        if n == 0:
            band_results[name] = {"n": 0, "mean_residual": None,
                                  "adjustment": 0, "reason": "no data"}
            continue
        mean_resid = float(sub["residual"].mean())
        if n < MIN_OVERRIDES_PER_BAND:
            band_results[name] = {"n": n,
                                  "mean_residual": round(mean_resid, 2),
                                  "adjustment": 0,
                                  "reason": f"n<{MIN_OVERRIDES_PER_BAND}"}
            continue
        if abs(mean_resid) < MIN_BAND_DELTA_FOR_SLOPE:
            band_results[name] = {"n": n,
                                  "mean_residual": round(mean_resid, 2),
                                  "adjustment": 0,
                                  "reason": "residual within noise"}
            continue
        # Apply the residual as an L1 adjustment to that band, snapped.
        band_results[name] = {"n": n,
                              "mean_residual": round(mean_resid, 2),
                              "adjustment": int(round(mean_resid)),
                              "reason": "fitted from data"}
    return band_results


def collect_overrides(readings: pd.DataFrame, *,
                      apply_right_contamination_filter: bool = True
                      ) -> pd.DataFrame:
    rd = readings[readings["action"] == "override"].copy()
    rd = rd[rd["override_delta"].notna() & rd["setting"].notna()
            & rd["elapsed_min"].notna()]
    rd["cycle_num"] = rd["elapsed_min"].apply(cycle_of)
    rd["user_pref_setting"] = (rd["setting"] + rd["override_delta"]).clip(-10, 0)

    cols = ["ts", "cycle_num", "room_temp_f", "setting",
            "override_delta", "user_pref_setting"]
    if "zone" in rd.columns:
        cols = ["zone"] + cols

    if apply_right_contamination_filter and "zone" in rd.columns \
            and "body_f" in rd.columns:
        # Drop right-zone overrides that occurred while the BedJet was
        # contaminating the body sensor (first 30 min after right-bed
        # occupancy onset). Prevents the fitter from learning her preferred
        # operating temperature from BedJet artifacts.
        from ml.contamination import (
            add_minutes_since_onset, is_body_right_valid,
        )
        try:
            rd = add_minutes_since_onset(rd)
            mask_left = rd["zone"].astype(str).str.lower() != "right"
            mask_right_valid = rd.apply(
                lambda r: is_body_right_valid(
                    r.get("minutes_since_onset"), r.get("body_f")),
                axis=1,
            )
            n_before = len(rd)
            rd = rd[mask_left | mask_right_valid].copy()
            dropped = n_before - len(rd)
            if dropped:
                print(f"  contamination filter dropped {dropped} right-zone "
                      f"override row(s) inside the BedJet window")
        except Exception as e:  # pragma: no cover  — defensive
            print(f"  WARN: contamination filter skipped ({e})")

    return rd[cols]


def main() -> int:
    print("Loading controller_readings via PG…")
    readings = data_io.load_readings()
    print(f"  {len(readings)} rows total")

    overrides = collect_overrides(readings)
    print(f"  {len(overrides)} override events for fitting\n")

    print("Override events by cycle:")
    print(overrides.groupby("cycle_num").agg(
        n=("user_pref_setting", "size"),
        pref_mean=("user_pref_setting", "mean"),
        room_mean=("room_temp_f", "mean"),
    ).round(2).to_string())
    print()

    fitted_cycles = fit_cycle_baselines(overrides)
    print("Fitted cycle baselines (prior_n =", PRIOR_N, "):")
    for c, info in fitted_cycles.items():
        print(f"  cycle {c}: v5={info['prior_mu']:+.0f}  "
              f"n={info['n_overrides']:>2}  "
              f"x̄={info['sample_mean'] if not pd.isna(info['sample_mean']) else 'na':>5}  "
              f"posterior={info['posterior_raw']:+.2f}  → {info['fitted']:+d}")

    print()
    room_comp = fit_room_comp(overrides, fitted_cycles)
    print("Fitted room-comp by band (after subtracting fitted baseline):")
    for band, info in room_comp.items():
        print(f"  {band:<5} n={info['n']:>2}  "
              f"residual={info['mean_residual']}  "
              f"adj={info['adjustment']:+d}  ({info['reason']})")

    payload = {
        "prior_n": PRIOR_N,
        "min_overrides_per_band": MIN_OVERRIDES_PER_BAND,
        "min_band_delta_for_slope": MIN_BAND_DELTA_FOR_SLOPE,
        "cycle_baselines_v5": {str(c): v for c, v in CYCLE_BASELINES.items()},
        "cycle_baselines_fitted": {str(c): info["fitted"]
                                   for c, info in fitted_cycles.items()},
        "cycle_diagnostics": {str(c): info for c, info in fitted_cycles.items()},
        "room_comp_band_adjustments": {b: info["adjustment"]
                                       for b, info in room_comp.items()},
        "room_comp_diagnostics": room_comp,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
