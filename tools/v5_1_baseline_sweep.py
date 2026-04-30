"""
v5.1 baseline sweep — shrinkage prior selection + held-out replay MAE.

Why this exists
---------------
fit_baselines.py picks a single prior_n=1 with no held-out check. After the
2026-04-29→04-30 user feedback ("woke up cold mid-night and warm pre-wake on
the same night") we needed to (a) choose prior_n by held-out night-fold MAE
rather than gut feel, (b) optionally replay a hand-specified non-monotonic
candidate ([-10,-7,-5,-4,-5,-6] etc.) and compare it head-to-head against v5
and the shrinkage fits, and (c) keep this reusable for the *next* time a
fresh override night lands.

What it does
------------
1. Loads `controller_readings` overrides via ml.data_io (which ssh-tunnels to
   the macmini PG). Falls back to a local CSV cache if `--cache PATH.csv` is
   passed, so analysis can be run without LAN/VPN access.
2. For each prior_n in {0, 1, 2, 5, 10}:
     - Leave-one-night-out CV across all override nights.
     - Fit cycle baselines on the 19 training nights using shrinkage with
       v5 baselines as the prior mean.
     - Score MAE (|fitted − user_pref|) on the held-out night's overrides.
     - Aggregate: held-out MAE, hit-rate (|err|≤1), and per-cycle bias.
3. Replays one or more *candidate* hand-specified baseline lists (passed via
   --candidate "name=v5_1:-10,-8,-7,-5,-5,-6") at every override moment and
   reports the same metrics, so you can A/B "shrinkage best" vs a clinically
   motivated curve.
4. Writes a JSON summary to `ml/state/v5_1_sweep_<isoday>.json` and prints a
   compact table.

Run:
    cd PerfectlySnug && .venv/bin/python tools/v5_1_baseline_sweep.py \\
        --candidate "v5:-10,-9,-8,-7,-6,-5" \\
        --candidate "fitted_p1:-10,-8,-6,-4,-3,-4" \\
        --candidate "v5_1:-10,-8,-7,-5,-5,-6"

    # Offline (no LAN access — use a previously dumped CSV):
    .venv/bin/python tools/v5_1_baseline_sweep.py --cache overrides_cache.csv ...

Constraint: baselines are clamped to [-10, 0] (no heating ever).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

V5_BASELINES = {1: -10, 2: -9, 3: -8, 4: -7, 5: -6, 6: -5}
CYCLE_DURATION_MIN = 90
NIGHT_GAP_HOURS = 2
PRIOR_GRID = (0.0, 1.0, 2.0, 5.0, 10.0)


def cycle_of(elapsed_min: float) -> int:
    cn = int(elapsed_min // CYCLE_DURATION_MIN) + 1
    return max(1, min(cn, max(V5_BASELINES)))


def assign_night(ts: pd.Series) -> pd.Series:
    """Group rows into nights using >NIGHT_GAP_HOURS gap rule."""
    ts_sorted = ts.sort_values()
    gaps = ts_sorted.diff() > pd.Timedelta(hours=NIGHT_GAP_HOURS)
    night_idx = gaps.cumsum()
    return night_idx.reindex(ts.index)


def load_overrides(cache: Path | None) -> pd.DataFrame:
    if cache is not None:
        df = pd.read_csv(cache, parse_dates=["ts"])
    else:
        from ml import data_io
        rd = data_io.load_readings()
        df = rd[(rd["action"] == "override")
                & rd["override_delta"].notna()
                & rd["setting"].notna()
                & rd["elapsed_min"].notna()].copy()
    df["user_pref"] = (df["setting"] + df["override_delta"]).clip(-10, 0)
    df["cycle"] = df["elapsed_min"].apply(cycle_of)
    df = df.sort_values("ts").reset_index(drop=True)
    df["night"] = assign_night(df["ts"])
    return df[["ts", "night", "cycle", "elapsed_min", "room_temp_f",
               "setting", "override_delta", "user_pref"]]


def fit_cycles(overrides: pd.DataFrame, prior_n: float) -> dict[int, int]:
    """Posterior-mean cycle baselines, snapped to ints in [-10, 0]."""
    out = {}
    for c in sorted(V5_BASELINES):
        prior_mu = float(V5_BASELINES[c])
        sub = overrides[overrides["cycle"] == c]
        n = len(sub)
        if n == 0 or prior_n + n == 0:
            posterior = prior_mu
        else:
            xbar = float(sub["user_pref"].mean())
            posterior = (prior_n * prior_mu + n * xbar) / (prior_n + n)
        out[c] = int(round(max(-10, min(0, posterior))))
    return out


def score(baselines: dict[int, int], overrides: pd.DataFrame) -> dict:
    pred = overrides["cycle"].map(baselines).astype(float)
    err = (pred - overrides["user_pref"]).abs()
    return {
        "n": int(len(overrides)),
        "mae": float(err.mean()) if len(err) else float("nan"),
        "hit_rate": float((err <= 1).mean()) if len(err) else float("nan"),
        "bias": float((pred - overrides["user_pref"]).mean())
                if len(err) else float("nan"),
    }


def loocv(overrides: pd.DataFrame, prior_n: float) -> dict:
    nights = sorted(overrides["night"].unique())
    per_night = []
    rows = []
    for held in nights:
        train = overrides[overrides["night"] != held]
        test = overrides[overrides["night"] == held]
        if test.empty:
            continue
        baselines = fit_cycles(train, prior_n=prior_n)
        s = score(baselines, test)
        s["held_night"] = int(held)
        s["fitted"] = baselines
        per_night.append(s)
        for _, r in test.iterrows():
            rows.append({"night": int(held), "cycle": int(r["cycle"]),
                         "user_pref": float(r["user_pref"]),
                         "pred": float(baselines[int(r["cycle"])])})
    df = pd.DataFrame(rows)
    err = (df["pred"] - df["user_pref"]).abs() if not df.empty else pd.Series([])
    overall = {
        "prior_n": prior_n,
        "n_total": int(len(df)),
        "n_nights": len(per_night),
        "held_out_mae": float(err.mean()) if len(err) else float("nan"),
        "held_out_hit_rate": float((err <= 1).mean()) if len(err) else float("nan"),
        "held_out_bias": float((df["pred"] - df["user_pref"]).mean())
                         if len(err) else float("nan"),
    }
    if not df.empty:
        per_cycle = df.groupby("cycle").apply(
            lambda g: pd.Series({
                "n": len(g),
                "mae": (g["pred"] - g["user_pref"]).abs().mean(),
                "bias": (g["pred"] - g["user_pref"]).mean(),
            })).round(3).to_dict("index")
        overall["per_cycle"] = per_cycle
    return overall


def parse_candidate(s: str) -> tuple[str, dict[int, int]]:
    name, vals = s.split("=", 1)
    parts = [int(x) for x in vals.split(",")]
    if len(parts) != 6:
        raise ValueError(f"candidate {name!r} needs 6 ints, got {parts}")
    for v in parts:
        if not (-10 <= v <= 0):
            raise ValueError(f"candidate {name!r} value {v} outside [-10, 0]")
    return name, {i + 1: v for i, v in enumerate(parts)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cache", type=Path, default=None,
                   help="Path to a CSV dump of override rows (columns: ts, "
                        "elapsed_min, setting, override_delta, room_temp_f). "
                        "If omitted, pulls live via ml.data_io (ssh macmini).")
    p.add_argument("--candidate", action="append", default=[],
                   help='Hand-specified baseline to replay, format '
                        '"name=c1,c2,c3,c4,c5,c6". Repeatable.')
    p.add_argument("--out", type=Path,
                   default=ROOT / "ml" / "state"
                           / f"v5_1_sweep_{datetime.now():%Y%m%d}.json")
    args = p.parse_args()

    overrides = load_overrides(args.cache)
    print(f"Loaded {len(overrides)} override events across "
          f"{overrides['night'].nunique()} nights\n")

    print("Override count by cycle:")
    print(overrides.groupby("cycle")["user_pref"].agg(
        n="size", mean="mean", std="std").round(2).to_string())
    print()

    sweep = []
    for prior_n in PRIOR_GRID:
        res = loocv(overrides, prior_n=prior_n)
        sweep.append(res)
        print(f"prior_n={prior_n:>4}  held-out MAE={res['held_out_mae']:.3f}  "
              f"hit={res['held_out_hit_rate']:.1%}  "
              f"bias={res['held_out_bias']:+.2f}  "
              f"(N={res['n_total']})")

    best = min(sweep, key=lambda r: r["held_out_mae"])
    print(f"\nBest prior_n by held-out MAE: {best['prior_n']} "
          f"(MAE={best['held_out_mae']:.3f})")

    # Fit on full data with the best prior, for deployment.
    full_fit = fit_cycles(overrides, prior_n=best["prior_n"])
    print(f"Full-data fit at best prior: {[full_fit[c] for c in sorted(full_fit)]}")

    # Candidate replays (in-sample — fair comparison since v5 is also in-sample).
    candidates = [parse_candidate(c) for c in args.candidate]
    candidates.append(("v5", V5_BASELINES))
    candidates.append((f"fit_p{best['prior_n']:g}_full", full_fit))

    print("\nIn-sample replay at override moments:")
    print(f"{'name':<25} {'MAE':>6} {'hit':>6} {'bias':>6} {'baseline':>30}")
    cand_results = []
    for name, baselines in candidates:
        s = score(baselines, overrides)
        cand_results.append({"name": name, "baselines": baselines, **s})
        bl_str = "[" + ",".join(f"{baselines[c]:+d}" for c in sorted(baselines)) + "]"
        print(f"{name:<25} {s['mae']:6.3f} {s['hit_rate']:6.1%} "
              f"{s['bias']:+6.2f} {bl_str:>30}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_overrides": int(len(overrides)),
        "n_nights": int(overrides["night"].nunique()),
        "prior_grid": list(PRIOR_GRID),
        "loocv": sweep,
        "best_prior_n": best["prior_n"],
        "full_fit_at_best_prior": full_fit,
        "candidates": cand_results,
        "v5_baselines": V5_BASELINES,
    }, indent=2, default=str))
    print(f"\nWrote {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
