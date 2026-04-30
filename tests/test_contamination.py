"""Tests for ml/contamination.py — BedJet right-zone artifact filter."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ml.contamination import (
    BEDJET_WINDOW_MIN,
    BODY_NATURAL_CEILING_F,
    in_bedjet_window,
    is_body_right_valid,
)


# ── is_body_right_valid ────────────────────────────────────────────────

def test_normal_reading_always_valid_inside_window():
    assert is_body_right_valid(5, 82.0)
    assert is_body_right_valid(0, 75.0)
    assert is_body_right_valid(15, 87.9)


def test_normal_reading_always_valid_outside_window():
    assert is_body_right_valid(120, 82.0)
    assert is_body_right_valid(31, 87.99)


def test_high_reading_invalid_inside_bedjet_window():
    # The whole point.
    assert not is_body_right_valid(0, 88.0)
    assert not is_body_right_valid(10, 95.0)
    assert not is_body_right_valid(BEDJET_WINDOW_MIN, 99.0)


def test_high_reading_valid_just_outside_window():
    # User: anything ≥ 88°F outside window is REAL overheat, must NOT be filtered.
    assert is_body_right_valid(BEDJET_WINDOW_MIN + 0.1, 88.0)
    assert is_body_right_valid(45, 92.0)
    assert is_body_right_valid(180, 99.0)


def test_threshold_boundary():
    # 87.99 is below the natural ceiling, so even at minute 0 it's valid.
    assert is_body_right_valid(0, 87.99)
    # 88.0 is at the ceiling — invalid in window, valid outside.
    assert not is_body_right_valid(0, 88.0)
    assert is_body_right_valid(31, 88.0)


def test_missing_body_invalid():
    assert not is_body_right_valid(45, None)
    assert not is_body_right_valid(45, float("nan"))


def test_missing_minutes_with_high_body_invalid():
    # Conservative: if we don't know when onset was, we can't rule out BedJet.
    assert not is_body_right_valid(None, 90.0)
    assert not is_body_right_valid(float("nan"), 90.0)


def test_missing_minutes_with_normal_body_valid():
    # Normal reading doesn't depend on the window at all.
    assert is_body_right_valid(None, 80.0)


def test_string_inputs_coerced():
    assert is_body_right_valid("45", "92.0")
    assert not is_body_right_valid("0", "92.0")


def test_garbage_inputs_invalid():
    assert not is_body_right_valid("not a number", 90)
    assert not is_body_right_valid(10, "not a number")


# ── in_bedjet_window ───────────────────────────────────────────────────

def test_in_bedjet_window_basic():
    assert in_bedjet_window(0)
    assert in_bedjet_window(15)
    assert in_bedjet_window(BEDJET_WINDOW_MIN)
    assert not in_bedjet_window(BEDJET_WINDOW_MIN + 0.01)
    assert not in_bedjet_window(120)


def test_in_bedjet_window_negative():
    # Pre-onset (clock skew or pre-occupancy reading) → not in window.
    assert not in_bedjet_window(-1)


def test_in_bedjet_window_missing():
    assert not in_bedjet_window(None)
    assert not in_bedjet_window(float("nan"))


# ── filter_dataframe / add_minutes_since_onset (smoke) ─────────────────

def test_dataframe_helpers_smoke():
    pd = __import__("pandas")
    rows = [
        # left-zone rows: passed through unchanged.
        {"zone": "left", "ts": "2026-04-20T22:00:00Z",
         "body_f": 95.0, "bed_right_pressure_pct": 0},
        # right-zone, in BedJet window, high reading → filtered.
        {"zone": "right", "ts": "2026-04-20T22:05:00Z",
         "body_f": 95.0, "bed_right_pressure_pct": 30},
        # right-zone, post window, high reading → kept (real overheat).
        {"zone": "right", "ts": "2026-04-20T23:00:00Z",
         "body_f": 92.0, "bed_right_pressure_pct": 30},
        # right-zone, post window, normal reading → kept.
        {"zone": "right", "ts": "2026-04-20T23:30:00Z",
         "body_f": 82.0, "bed_right_pressure_pct": 30},
    ]
    df = pd.DataFrame(rows)
    from ml.contamination import add_minutes_since_onset, filter_dataframe
    df2 = add_minutes_since_onset(df)
    df3 = filter_dataframe(df2)
    # left-zone preserved, BedJet right-zone artifact removed.
    assert len(df3) == 3
    assert (df3["zone"] == "left").sum() == 1
    # The 95°F right-zone reading at 22:05 (5 min post onset) is gone.
    assert not ((df3["zone"] == "right") & (df3["body_f"] > 94)).any()
