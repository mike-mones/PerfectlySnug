#!/usr/bin/env python3
"""
PerfectlySnug — A/B comparison harness over v6_nightly_summary.

Per docs/proposals/2026-05-04_evaluation.md §4 + §5.

Reads two cohorts of (night, zone) rows from v6_nightly_summary, computes
per-metric medians, paired bootstrap 95% CI, permutation p-values, and
prints a single decision-grade table. Exits:
    0 = ACCEPT (per §5.1)
    1 = HOLD   (per §5.3)
    2 = REVERT (per §5.2)

Usage:
    # Date-range mode
    python tools/eval_compare.py \\
        --A "2026-04-25..2026-05-01" --A-label "v5.2" \\
        --B "2026-05-02..2026-05-08" --B-label "v6_state" \\
        --zone left

    # controller_version-tag mode (LIKE patterns)
    python tools/eval_compare.py \\
        --A-version "v5_2_rc_off%" --B-version "v6_state%" --zone left

When the same (night, zone) appears in both cohorts (e.g. a future
shadow-vs-live comparison with two controller_version values), pairing is
automatic.
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import psycopg2
import psycopg2.extras

# Lower = better for these (we sign-flip the % improvement check accordingly).
LOWER_IS_BETTER = {
    "adj_count_per_night", "adj_magnitude_sum", "adj_weighted_score",
    "oscillation_count", "overcorrection_rate", "setting_total_variation",
    "discomfort_event_count", "time_to_correct_median_min",
    "unaddressed_discomfort_min",
    "cold_minutes", "warm_minutes",
}
# Higher = better for these.
HIGHER_IS_BETTER = {"body_in_target_band_pct"}

ALL_METRICS = [
    "adj_weighted_score",
    "adj_count_per_night",
    "adj_magnitude_sum",
    "oscillation_count",
    "overcorrection_rate",
    "setting_total_variation",
    "discomfort_event_count",
    "time_to_correct_median_min",
    "unaddressed_discomfort_min",
    "body_in_target_band_pct",
    "cold_minutes",
    "warm_minutes",
]

# ACCEPT/REVERT thresholds per evaluation.md §5.
ACCEPT_PCT_IMPROVEMENT = 0.15           # adj_weighted_score must improve ≥15%
ACCEPT_COLD_WORSEN_MAX_MIN = 10         # cold_minutes can't worsen by >10 min
ACCEPT_OSC_WORSEN_MAX_PCT = 0.25        # oscillation_count can't worsen by >25%
REVERT_PCT_DEGRADATION = 0.20           # adj_weighted_score worsens ≥20% → revert
REVERT_COLD_INCREASE_MIN = 30           # cold_minutes increases ≥30 min → revert
REVERT_BAND_PCT_FLOOR = 50.0            # band% < 50% on >50% of B nights → revert

PG_HOST = os.environ.get("PG_HOST", "192.168.0.3")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "sleepdata")
PG_USER = os.environ.get("PG_USER", "sleepsync")
PG_PASS = os.environ.get("PG_PASS", "sleepsync_local")


def get_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS, connect_timeout=10,
    )


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_range(s: str) -> tuple[date, date]:
    a, b = s.split("..", 1)
    return parse_date(a), parse_date(b)


@dataclass
class Cohort:
    label: str
    rows: list[dict]   # one dict per (night, zone) row from v6_nightly_summary

    @property
    def nights(self) -> list[date]:
        return [r["night"] for r in self.rows]


def fetch_cohort(conn, *, label: str, zone: str,
                 date_range: Optional[tuple[date, date]] = None,
                 version_like: Optional[str] = None) -> Cohort:
    where = ["zone = %s",
             "metrics_schema_version IS NOT NULL"]
    params: list = [zone]
    if date_range:
        where.append("night BETWEEN %s AND %s")
        params.extend(date_range)
    if version_like:
        where.append("controller_version LIKE %s")
        params.append(version_like)
    sql = f"""
        SELECT night, zone, controller_version, in_bed_minutes,
               adj_count_per_night, adj_magnitude_sum, adj_weighted_score,
               oscillation_count, overcorrection_rate, setting_total_variation,
               discomfort_event_count, time_to_correct_median_min,
               unaddressed_discomfort_min,
               body_in_target_band_pct, cold_minutes, warm_minutes
        FROM v6_nightly_summary
        WHERE {' AND '.join(where)}
        ORDER BY night
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    return Cohort(label=label, rows=rows)


def _values(rows: list[dict], metric: str) -> list[float]:
    return [float(r[metric]) for r in rows if r[metric] is not None]


def _median(xs: list[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def _bootstrap_ci(diffs: list[float], iters: int = 10000,
                  seed: int = 0) -> tuple[float, float]:
    if not diffs:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(diffs)
    boot = []
    for _ in range(iters):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        boot.append(statistics.median(sample))
    boot.sort()
    lo = boot[int(0.025 * iters)]
    hi = boot[int(0.975 * iters)]
    return lo, hi


def _permutation_p(values_a: list[float], values_b: list[float],
                   iters: int = 10000, seed: int = 1) -> float:
    if not values_a or not values_b:
        return float("nan")
    obs = statistics.median(values_b) - statistics.median(values_a)
    pool = values_a + values_b
    n_a = len(values_a)
    rng = random.Random(seed)
    extreme = 0
    for _ in range(iters):
        rng.shuffle(pool)
        a = pool[:n_a]; b = pool[n_a:]
        d = statistics.median(b) - statistics.median(a)
        if abs(d) >= abs(obs):
            extreme += 1
    return (extreme + 1) / (iters + 1)


@dataclass
class MetricResult:
    metric: str
    a_median: Optional[float]
    b_median: Optional[float]
    delta: Optional[float]
    ci_lo: Optional[float]
    ci_hi: Optional[float]
    p_value: Optional[float]
    n_a: int
    n_b: int
    paired_n: int


def compare_metric(a: Cohort, b: Cohort, metric: str,
                   paired_keys: list[date]) -> MetricResult:
    a_vals = _values(a.rows, metric)
    b_vals = _values(b.rows, metric)
    a_med = _median(a_vals)
    b_med = _median(b_vals)

    # Paired bootstrap if we have paired nights, else cluster-bootstrap of B-A.
    a_by_night = {r["night"]: r[metric] for r in a.rows if r[metric] is not None}
    b_by_night = {r["night"]: r[metric] for r in b.rows if r[metric] is not None}
    paired = [(a_by_night[k], b_by_night[k]) for k in paired_keys
              if k in a_by_night and k in b_by_night]

    if paired:
        diffs = [b_v - a_v for a_v, b_v in paired]
        ci_lo, ci_hi = _bootstrap_ci(diffs)
        a_only = [a_v for a_v, _ in paired]
        b_only = [b_v for _, b_v in paired]
        p = _permutation_p(a_only, b_only)
    else:
        # Unpaired: bootstrap B - A medians.
        if a_vals and b_vals:
            rng = random.Random(2)
            iters = 10000
            boot = []
            for _ in range(iters):
                a_s = [a_vals[rng.randrange(len(a_vals))] for _ in range(len(a_vals))]
                b_s = [b_vals[rng.randrange(len(b_vals))] for _ in range(len(b_vals))]
                boot.append(statistics.median(b_s) - statistics.median(a_s))
            boot.sort()
            ci_lo, ci_hi = boot[int(0.025 * iters)], boot[int(0.975 * iters)]
            p = _permutation_p(a_vals, b_vals)
        else:
            ci_lo = ci_hi = p = None

    delta = (b_med - a_med) if (a_med is not None and b_med is not None) else None
    return MetricResult(
        metric=metric, a_median=a_med, b_median=b_med, delta=delta,
        ci_lo=ci_lo, ci_hi=ci_hi, p_value=p,
        n_a=len(a_vals), n_b=len(b_vals), paired_n=len(paired),
    )


def decide(results: dict[str, MetricResult], cohort_b: Cohort) -> tuple[str, list[str]]:
    """Return (decision, reasons). decision ∈ {ACCEPT, HOLD, REVERT}."""
    reasons: list[str] = []
    aw = results.get("adj_weighted_score")

    # REVERT checks (any one triggers).
    revert_reasons: list[str] = []
    if aw and aw.a_median and aw.b_median is not None:
        if aw.a_median > 0:
            pct = (aw.b_median - aw.a_median) / aw.a_median
            if pct >= REVERT_PCT_DEGRADATION and (aw.ci_lo is not None and aw.ci_lo > 0):
                revert_reasons.append(
                    f"adj_weighted_score worsened by {pct:+.1%} (CI lower {aw.ci_lo:+.2f} > 0)")
    cm = results.get("cold_minutes")
    if cm and cm.delta is not None and cm.delta >= REVERT_COLD_INCREASE_MIN:
        revert_reasons.append(f"cold_minutes increased by {cm.delta:+.0f} ≥ {REVERT_COLD_INCREASE_MIN}")
    band_below = sum(1 for r in cohort_b.rows
                     if r["body_in_target_band_pct"] is not None
                     and r["body_in_target_band_pct"] < REVERT_BAND_PCT_FLOOR)
    if cohort_b.rows and band_below / len(cohort_b.rows) > 0.5:
        revert_reasons.append(
            f"body_in_target_band_pct < {REVERT_BAND_PCT_FLOOR:.0f}% on "
            f"{band_below}/{len(cohort_b.rows)} cohort-B nights (>50%)")

    if revert_reasons:
        return "REVERT", revert_reasons

    # ACCEPT checks (all three must hold).
    if not aw or aw.a_median is None or aw.b_median is None:
        return "HOLD", ["insufficient adj_weighted_score data"]
    if aw.a_median <= 0:
        # If A had perfect score, can't compute % improvement; treat as HOLD.
        return "HOLD", [f"A cohort adj_weighted_score median={aw.a_median:.2f} (≤0)"]
    pct_improve = (aw.a_median - aw.b_median) / aw.a_median
    accept_ok = True
    if pct_improve < ACCEPT_PCT_IMPROVEMENT:
        accept_ok = False
        reasons.append(
            f"adj_weighted_score improved {pct_improve:+.1%} (need ≥{ACCEPT_PCT_IMPROVEMENT:+.1%})")
    if aw.ci_hi is None or aw.ci_hi >= 0:
        accept_ok = False
        reasons.append(
            f"adj_weighted_score CI upper {aw.ci_hi if aw.ci_hi is not None else 'NA':+.2f} "
            f"is not < 0 (improvement not significant)")
    if cm and cm.delta is not None and cm.delta > ACCEPT_COLD_WORSEN_MAX_MIN:
        accept_ok = False
        reasons.append(
            f"cold_minutes worsened by {cm.delta:+.0f} > {ACCEPT_COLD_WORSEN_MAX_MIN} min limit")
    osc = results.get("oscillation_count")
    if osc and osc.a_median is not None and osc.b_median is not None and osc.a_median > 0:
        osc_worsen = (osc.b_median - osc.a_median) / osc.a_median
        if osc_worsen > ACCEPT_OSC_WORSEN_MAX_PCT:
            accept_ok = False
            reasons.append(
                f"oscillation_count worsened by {osc_worsen:+.1%} > "
                f"{ACCEPT_OSC_WORSEN_MAX_PCT:+.1%} limit")

    if accept_ok:
        return "ACCEPT", reasons or ["all §5.1 conditions met"]
    return "HOLD", reasons


# ── Output ────────────────────────────────────────────────────────────
def fmt(v: Optional[float], width: int, prec: int = 2) -> str:
    if v is None:
        return f"{'NA':>{width}}"
    if abs(v) >= 100:
        return f"{v:>{width}.0f}"
    return f"{v:>{width}.{prec}f}"


def print_table(a: Cohort, b: Cohort, results: dict[str, MetricResult],
                paired_n: int, zone: str) -> None:
    header = (f"\nZone: {zone:<6} |  paired nights: {paired_n}  |  "
              f"A={a.label} ({len(a.rows)} nights)  "
              f"B={b.label} ({len(b.rows)} nights)\n")
    print(header)
    print(f"{'metric':32}  {'A':>8}  {'B':>8}  {'Δ':>8}  "
          f"{'95% CI':>18}  {'p':>6}")
    print("-" * 90)
    for m in ALL_METRICS:
        r = results[m]
        ci = (f"[{fmt(r.ci_lo, 6)},{fmt(r.ci_hi, 6)}]"
              if r.ci_lo is not None else f"{'NA':>18}")
        p_str = "<.01" if (r.p_value is not None and r.p_value < 0.01) else (
            f"{r.p_value:>6.2f}" if r.p_value is not None else f"{'NA':>6}")
        print(f"{m:32}  {fmt(r.a_median, 8)}  {fmt(r.b_median, 8)}  "
              f"{fmt(r.delta, 8)}  {ci:>18}  {p_str:>6}")


def main() -> int:
    p = argparse.ArgumentParser(description="A/B compare two cohorts of nights.")
    p.add_argument("--A", help="A cohort date range YYYY-MM-DD..YYYY-MM-DD")
    p.add_argument("--B", help="B cohort date range YYYY-MM-DD..YYYY-MM-DD")
    p.add_argument("--A-label", default="A")
    p.add_argument("--B-label", default="B")
    p.add_argument("--A-version", help="A cohort controller_version LIKE pattern")
    p.add_argument("--B-version", help="B cohort controller_version LIKE pattern")
    p.add_argument("--zone", choices=["left", "right"], default="left")
    args = p.parse_args()

    if not (args.A or args.A_version) or not (args.B or args.B_version):
        print("ERROR: must specify both --A/--B (date range) and/or --A-version/--B-version",
              file=sys.stderr)
        return 3

    conn = get_conn()
    try:
        a = fetch_cohort(
            conn, label=args.A_label, zone=args.zone,
            date_range=parse_range(args.A) if args.A else None,
            version_like=args.A_version,
        )
        b = fetch_cohort(
            conn, label=args.B_label, zone=args.zone,
            date_range=parse_range(args.B) if args.B else None,
            version_like=args.B_version,
        )
    finally:
        conn.close()

    if not a.rows:
        print(f"ERROR: cohort A is empty (label={args.A_label}, zone={args.zone})", file=sys.stderr)
        return 3
    if not b.rows:
        print(f"ERROR: cohort B is empty (label={args.B_label}, zone={args.zone})", file=sys.stderr)
        return 3

    paired_keys = sorted(set(a.nights) & set(b.nights))
    results = {m: compare_metric(a, b, m, paired_keys) for m in ALL_METRICS}
    print_table(a, b, results, len(paired_keys), args.zone)

    decision, reasons = decide(results, b)
    print(f"\nDecision: {decision}")
    for r in reasons:
        print(f"  - {r}")
    print()
    return {"ACCEPT": 0, "HOLD": 1, "REVERT": 2}[decision]


if __name__ == "__main__":
    sys.exit(main())
