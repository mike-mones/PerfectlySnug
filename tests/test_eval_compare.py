"""Tests for tools/eval_compare.py decision logic with synthetic cohorts."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eval_compare", ROOT / "tools" / "eval_compare.py")
EC = importlib.util.module_from_spec(SPEC)
sys.modules["eval_compare"] = EC
SPEC.loader.exec_module(EC)


def _row(night: date, **metrics):
    base = {
        "night": night, "zone": "left", "controller_version": "test_v",
        "in_bed_minutes": 480,
        "adj_count_per_night": 0, "adj_magnitude_sum": 0, "adj_weighted_score": 0.0,
        "oscillation_count": 0, "overcorrection_rate": None, "setting_total_variation": 0,
        "discomfort_event_count": 0, "time_to_correct_median_min": None,
        "unaddressed_discomfort_min": 0,
        "body_in_target_band_pct": 80.0, "cold_minutes": 0, "warm_minutes": 0,
    }
    base.update(metrics)
    return base


def _cohort(label: str, rows: list[dict]) -> EC.Cohort:
    return EC.Cohort(label=label, rows=rows)


def test_decide_accept_when_all_three_conditions_met():
    a_rows = [_row(date(2026, 4, 1) + i_to_td(i), adj_weighted_score=4.0,
                   oscillation_count=8, cold_minutes=10, body_in_target_band_pct=60.0)
              for i in range(5)]
    b_rows = [_row(date(2026, 4, 10) + i_to_td(i), adj_weighted_score=2.0,
                   oscillation_count=2, cold_minutes=12, body_in_target_band_pct=80.0)
              for i in range(5)]
    a = _cohort("A", a_rows); b = _cohort("B", b_rows)
    paired = []  # unpaired
    results = {m: EC.compare_metric(a, b, m, paired) for m in EC.ALL_METRICS}
    decision, reasons = EC.decide(results, b)
    assert decision == "ACCEPT", reasons


def test_decide_revert_when_score_worsens_significantly():
    # Wide-margin worse cohort: B median 8.0 vs A 4.0 with no overlap.
    a_rows = [_row(date(2026, 4, 1) + i_to_td(i),
                   adj_weighted_score=4.0 + (i * 0.05), body_in_target_band_pct=70.0)
              for i in range(8)]
    b_rows = [_row(date(2026, 4, 10) + i_to_td(i),
                   adj_weighted_score=8.0 + (i * 0.05), body_in_target_band_pct=70.0)
              for i in range(8)]
    a = _cohort("A", a_rows); b = _cohort("B", b_rows)
    results = {m: EC.compare_metric(a, b, m, []) for m in EC.ALL_METRICS}
    decision, reasons = EC.decide(results, b)
    assert decision == "REVERT", reasons


def test_decide_revert_when_cold_minutes_jump_30():
    a_rows = [_row(date(2026, 4, 1) + i_to_td(i), adj_weighted_score=2.0,
                   cold_minutes=10) for i in range(5)]
    b_rows = [_row(date(2026, 4, 10) + i_to_td(i), adj_weighted_score=1.5,
                   cold_minutes=50) for i in range(5)]
    a = _cohort("A", a_rows); b = _cohort("B", b_rows)
    results = {m: EC.compare_metric(a, b, m, []) for m in EC.ALL_METRICS}
    decision, reasons = EC.decide(results, b)
    assert decision == "REVERT", reasons


def test_decide_revert_when_majority_band_below_floor():
    a_rows = [_row(date(2026, 4, 1) + i_to_td(i), adj_weighted_score=2.0,
                   body_in_target_band_pct=70.0) for i in range(5)]
    b_rows = [_row(date(2026, 4, 10) + i_to_td(i), adj_weighted_score=1.5,
                   body_in_target_band_pct=30.0) for i in range(5)]
    a = _cohort("A", a_rows); b = _cohort("B", b_rows)
    results = {m: EC.compare_metric(a, b, m, []) for m in EC.ALL_METRICS}
    decision, reasons = EC.decide(results, b)
    assert decision == "REVERT", reasons


def test_decide_hold_when_marginal():
    # Improvement is small (<15%) but no regressions trigger REVERT.
    a_rows = [_row(date(2026, 4, 1) + i_to_td(i), adj_weighted_score=4.0,
                   body_in_target_band_pct=70.0) for i in range(5)]
    b_rows = [_row(date(2026, 4, 10) + i_to_td(i), adj_weighted_score=3.7,
                   body_in_target_band_pct=70.0) for i in range(5)]
    a = _cohort("A", a_rows); b = _cohort("B", b_rows)
    results = {m: EC.compare_metric(a, b, m, []) for m in EC.ALL_METRICS}
    decision, reasons = EC.decide(results, b)
    assert decision == "HOLD", reasons


def test_paired_bootstrap_when_same_nights_in_both():
    # Same dates with two different controller_versions.
    nights = [date(2026, 4, 1) + i_to_td(i) for i in range(6)]
    a_rows = [_row(n, adj_weighted_score=4.0) for n in nights]
    b_rows = [_row(n, adj_weighted_score=2.0) for n in nights]
    a = _cohort("A", a_rows); b = _cohort("B", b_rows)
    paired = nights
    res = EC.compare_metric(a, b, "adj_weighted_score", paired)
    assert res.paired_n == 6
    # Δ = -2 with low variance → CI should be tight around -2 and exclude 0
    assert res.delta == pytest.approx(-2.0)
    assert res.ci_hi is not None and res.ci_hi <= 0


def test_lower_is_better_set_does_not_overlap_higher():
    assert EC.LOWER_IS_BETTER.isdisjoint(EC.HIGHER_IS_BETTER)


def test_metric_list_subsets():
    assert set(EC.ALL_METRICS) == EC.LOWER_IS_BETTER | EC.HIGHER_IS_BETTER


# helper
def i_to_td(i: int):
    from datetime import timedelta
    return timedelta(days=i)
