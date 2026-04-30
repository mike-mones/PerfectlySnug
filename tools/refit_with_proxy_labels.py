"""
Refit per-cycle baselines using the expanded discomfort corpus
(overrides + proxy + silent-acceptance) and replay against v5 to decide
whether the new fit beats v5.

Pipeline:

  1. Load `ml/state/discomfort_corpus.parquet` (built by
     tools/build_discomfort_corpus.py).
  2. For each labelled minute, derive a "preferred setting":
       - override:   setting + override_delta  (the user's revealed pref)
       - proxy:      setting - 1               (one step cooler / direction
                     of historical override mean for this user, who runs hot)
       - silent:     setting                   (current setting was acceptable)
  3. Posterior-mean per cycle, weighting each row by `label_weight`. Same
     Normal-Normal shrinkage as tools/fit_baselines.py, but driven by the
     full corpus instead of the 47 override events alone.
  4. Counterfactual replay vs v5 at the original 47 override moments using
     ml.features.smart_baseline (which auto-uses the new fitted JSON when
     present).
  5. Write a *candidate* JSON to ml/state/fitted_baselines.candidate.json
     and print the verdict. **Does not** overwrite the live
     fitted_baselines.json — the user must `mv` it after reviewing.
     This guarantees no surprise behavior change to the shadow logger.

Decision rubric (printed in the verdict):
  - SHIP if NEW MAE < v5 MAE - 0.20  AND  NEW hit-rate ≥ v5 hit-rate
  - HOLD otherwise
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
from ml.features import (CYCLE_BASELINES, CYCLE_DURATION_MIN,
                         cycle_num_of)


PRIOR_N = 1.0
PROXY_DIR = -1   # this user runs hot; proxy fires => prefer one step cooler

CORPUS_PATH = ROOT / "ml" / "state" / "discomfort_corpus.parquet"
CANDIDATE_OUT = ROOT / "ml" / "state" / "fitted_baselines.candidate.json"


def derive_preferred_setting(row) -> float | None:
    src = row["label_source"]
    s = row["setting"]
    if pd.isna(s):
        return None
    if src == "override":
        d = row.get("override_delta")
        if pd.isna(d):
            return None
        return float(np.clip(s + d, -10, 0))
    if src == "proxy":
        return float(np.clip(s + PROXY_DIR, -10, 0))
    if src == "silent":
        return float(s)
    return None


def shrink(prior_mu: float, weighted_mean: float, eff_n: float) -> float:
    return (PRIOR_N * prior_mu + eff_n * weighted_mean) / (PRIOR_N + eff_n)


def fit_cycle_baselines(corpus: pd.DataFrame) -> dict[int, dict]:
    out = {}
    for c in sorted(CYCLE_BASELINES):
        prior_mu = float(CYCLE_BASELINES[c])
        sub = corpus[(corpus["cycle_num"] == c) & corpus["pref"].notna()
                     & (corpus["label_weight"] > 0)]
        if sub.empty:
            out[c] = {"prior_mu": prior_mu, "n_rows": 0, "eff_n": 0.0,
                      "weighted_mean": None, "posterior": prior_mu,
                      "fitted": int(round(prior_mu))}
            continue
        w = sub["label_weight"].to_numpy(dtype=float)
        x = sub["pref"].to_numpy(dtype=float)
        eff_n = float(w.sum())
        wmean = float((w * x).sum() / eff_n)
        post = shrink(prior_mu, wmean, eff_n)
        snapped = int(round(max(-10, min(0, post))))
        out[c] = {
            "prior_mu": prior_mu,
            "n_rows": int(len(sub)),
            "eff_n": round(eff_n, 2),
            "weighted_mean": round(wmean, 3),
            "posterior": round(post, 3),
            "n_overrides": int((sub["label_source"] == "override").sum()),
            "n_proxy":     int((sub["label_source"] == "proxy").sum()),
            "n_silent":    int((sub["label_source"] == "silent").sum()),
            "fitted": snapped,
        }
    return out


def replay_vs_v5(rd_overrides: pd.DataFrame, fitted: dict[int, int]) -> dict:
    """MAE & hit-rate of v5 vs the new fitted baselines at override moments."""
    rd = rd_overrides.copy()
    rd["user_pref"] = (rd["setting"] + rd["override_delta"]).clip(-10, 0)
    rd["cycle"] = rd["elapsed_min"].apply(cycle_num_of)
    rd["v5_setting"] = rd["setting"]
    rd["new_setting"] = rd["cycle"].map(lambda c: fitted.get(c, CYCLE_BASELINES[c]))
    rd["v5_err"]  = (rd["v5_setting"]  - rd["user_pref"]).abs()
    rd["new_err"] = (rd["new_setting"] - rd["user_pref"]).abs()
    return {
        "n": int(len(rd)),
        "v5_mae":  float(rd["v5_err"].mean()),
        "new_mae": float(rd["new_err"].mean()),
        "v5_hit":  float((rd["v5_err"] <= 1).mean()),
        "new_hit": float((rd["new_err"] <= 1).mean()),
        "new_better_pct": float((rd["new_err"] < rd["v5_err"]).mean()),
        "v5_better_pct":  float((rd["v5_err"] < rd["new_err"]).mean()),
        "tie_pct":        float((rd["v5_err"] == rd["new_err"]).mean()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mae-margin", type=float, default=0.20,
                    help="ship if new_mae < v5_mae - margin")
    args = ap.parse_args()

    if not CORPUS_PATH.exists():
        print(f"ERROR: {CORPUS_PATH} not found. Run "
              "tools/build_discomfort_corpus.py first.", file=sys.stderr)
        return 2

    print(f"Loading corpus from {CORPUS_PATH.relative_to(ROOT)}…")
    corpus = pd.read_parquet(CORPUS_PATH)
    corpus["pref"] = corpus.apply(derive_preferred_setting, axis=1)
    corpus["cycle_num"] = corpus["elapsed_min"].apply(
        lambda em: cycle_num_of(em) if pd.notna(em) else None)

    print("\nLabel-source distribution:")
    print(corpus["label_source"].value_counts().to_string())
    print(f"\nEffective sample size: {corpus['label_weight'].sum():.1f} "
          f"(vs baseline of 47 overrides)")

    print("\nFitting per-cycle posteriors…")
    fitted_diag = fit_cycle_baselines(corpus)
    fitted = {c: d["fitted"] for c, d in fitted_diag.items()}
    for c, d in fitted_diag.items():
        print(f"  cycle {c}: v5={d['prior_mu']:+.0f}  "
              f"eff_n={d['eff_n']:>6.1f}  "
              f"wmean={d['weighted_mean']}  → fitted={d['fitted']:+d}")

    print("\nReplay vs v5 at override moments…")
    rd = data_io.load_readings()
    ov = rd[(rd["action"] == "override")
            & rd["override_delta"].notna()
            & rd["setting"].notna()
            & rd["elapsed_min"].notna()].copy()
    replay = replay_vs_v5(ov, fitted)
    for k, v in replay.items():
        print(f"  {k:<20} {v}")

    delta = replay["v5_mae"] - replay["new_mae"]
    ship = (delta > args.mae_margin) and (replay["new_hit"] >= replay["v5_hit"])
    verdict = "SHIP" if ship else "HOLD"
    print(f"\nMAE delta (v5 - new): {delta:+.3f}  (margin {args.mae_margin})")
    print(f"VERDICT: {verdict}")

    payload = {
        "source": "tools/refit_with_proxy_labels.py",
        "prior_n": PRIOR_N,
        "proxy_dir": PROXY_DIR,
        "cycle_baselines_v5": {str(c): v for c, v in CYCLE_BASELINES.items()},
        "cycle_baselines_fitted": {str(c): fitted[c]
                                   for c in sorted(fitted)},
        "cycle_diagnostics": {str(c): d for c, d in fitted_diag.items()},
        # leave room-comp at neutral; refit dedicated to cycle baselines.
        "room_comp_band_adjustments": {"cold": 0, "cool": 0, "neutral": 0,
                                       "warm": 0, "heat_on": -1},
        "replay": replay,
        "verdict": verdict,
        "ship_threshold": {"mae_margin": args.mae_margin,
                           "hit_rate_must_not_regress": True},
    }
    CANDIDATE_OUT.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote candidate to {CANDIDATE_OUT.relative_to(ROOT)}")
    if ship:
        print("→ To deploy: "
              f"mv {CANDIDATE_OUT.relative_to(ROOT)} "
              "ml/state/fitted_baselines.json")
    else:
        print("→ Holding. fitted_baselines.json untouched. See verdict above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
