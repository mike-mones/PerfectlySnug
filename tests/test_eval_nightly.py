"""Tests for tools/eval_nightly.py — synthetic golden-night metric computation.

These tests exercise the pure metric computation functions with hand-built
controller_readings rows; they do NOT touch Postgres.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eval_nightly", ROOT / "tools" / "eval_nightly.py")
EN = importlib.util.module_from_spec(SPEC)
sys.modules["eval_nightly"] = EN
SPEC.loader.exec_module(EN)


UTC = timezone.utc


def _row(t_min: int, action: str, setting: int, body: float | None = None,
         override_delta: int | None = None, occ_left: bool = True,
         occ_right: bool = False, controller_version: str = "test_v"):
    return {
        "ts": datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC) + timedelta(minutes=t_min),
        "action": action,
        "setting": setting,
        "override_delta": override_delta,
        "body_avg_f": body,
        "bed_occupied_left": occ_left,
        "bed_occupied_right": occ_right,
        "controller_version": controller_version,
    }


def test_discomfort_counts_overrides_only():
    rows = [
        _row(0, "set", -5),
        _row(10, "override", -7, override_delta=-2),
        _row(20, "passive", -7),
        _row(30, "override", -5, override_delta=2),
    ]
    cnt, mag, weighted, overrides = EN._compute_discomfort(rows)
    assert cnt == 2
    assert mag == 4  # |-2| + |2|
    assert weighted == pytest.approx(2 + 0.5 * 4)
    assert len(overrides) == 2


def test_stability_counts_sign_flips_on_set_only():
    # set actions: -5 -> -7 (Δ=-2) -> -5 (Δ=+2) -> -3 (Δ=+2) -> -6 (Δ=-3)
    # sign sequence: -, +, +, -  => flips at idx1 and idx3 => 2 flips
    rows = [
        _row(0,  "set", -5),
        _row(10, "set", -7),
        _row(20, "set", -5),
        _row(30, "passive", -5),  # no-op heartbeat — must be ignored
        _row(40, "set", -3),
        _row(50, "set", -6),
    ]
    flips, _overc, tv, writes = EN._compute_stability(rows)
    assert flips == 2
    assert tv == 2 + 2 + 2 + 3
    assert len(writes) == 4


def test_stability_ignores_safety_writes():
    # If hot_safety were counted, this sequence would show 2 writes with a
    # sign flip (-5 → -10 → -3 = Δ-5, Δ+7). With hot_safety ignored, the
    # only Δ is from the first set (-5) to the third set (-3) = +2, no flip.
    rows = [
        _row(0,  "set", -5),
        _row(10, "hot_safety", -10),
        _row(20, "set", -3),
    ]
    flips, _, tv, writes = EN._compute_stability(rows)
    assert len(writes) == 1
    assert tv == 2
    assert flips == 0


def test_stability_overcorrection_rate():
    # set sequence: -5 -> -8 (Δ=-3) -> -5 (Δ=+3) within 5 min
    # First write has an opposite-sign follower within 10 min → overc_num=1, denom=1
    rows = [
        _row(0,  "set", -5),
        _row(5,  "set", -8),
        _row(10, "set", -5),
    ]
    flips, overc, tv, writes = EN._compute_stability(rows)
    assert flips == 1
    assert overc == pytest.approx(1.0)


def test_comfort_in_band_uses_actual_time_slices():
    # 60 min bed window; 4 rows at 0,15,30,45 min, all occupied.
    # bodies: 80 (in), 80 (in), 85 (warm), 76 (cold)
    end = datetime(2026, 5, 1, 1, 0, 0, tzinfo=UTC)
    rows = [
        _row(0,  "passive", -5, body=80.0),
        _row(15, "passive", -5, body=80.0),
        _row(30, "passive", -5, body=85.0),
        _row(45, "passive", -5, body=76.0),
    ]
    pct, cold, warm, in_bed = EN._compute_comfort(rows, "left", end)
    # Each row covers 15 min. in_band = 30 min, warm = 15, cold = 15, in_bed = 60.
    assert in_bed == 60
    assert cold == 15
    assert warm == 15
    assert pct == pytest.approx(50.0)


def test_comfort_excludes_unoccupied_rows():
    end = datetime(2026, 5, 1, 1, 0, 0, tzinfo=UTC)
    rows = [
        _row(0,  "passive", -5, body=80.0, occ_left=False),
        _row(30, "passive", -5, body=80.0, occ_left=True),
    ]
    pct, cold, warm, in_bed = EN._compute_comfort(rows, "left", end)
    assert in_bed == 30  # only the second 30-minute slice
    assert pct == pytest.approx(100.0)


def test_responsiveness_credits_corrective_write_with_correct_sign():
    # Override at t=10 says "too cold" (delta=+2). A controller write at t=15
    # with Δsetting>0 (warmer) is corrective. Expected ttc = 5 min.
    rows = [
        _row(0,  "set",      -5, body=80.0),
        _row(10, "override", -3, body=80.0, override_delta=+2),
        _row(15, "set",      -3, body=80.0),  # Δ=+2 from prev set (-5 → -3)
    ]
    _cnt, _mag, _w, overrides = EN._compute_discomfort(rows)
    _flips, _overc, _tv, writes = EN._compute_stability(rows)
    end = rows[-1]["ts"] + timedelta(minutes=10)
    n_events, median_ttc, _unaddr = EN._compute_responsiveness(
        rows, overrides, writes, "left", end)
    assert n_events >= 1
    assert median_ttc == pytest.approx(5.0)


def test_responsiveness_censors_at_horizon_when_no_corrective_write():
    # Override "too warm" (delta=-2). Subsequent write goes WARMER (wrong direction).
    rows = [
        _row(0,  "set",      -5, body=82.5),
        _row(10, "override", -7, body=82.5, override_delta=-2),
        _row(15, "set",      -3, body=82.5),  # Δ=+2 — wrong direction
    ]
    _cnt, _mag, _w, overrides = EN._compute_discomfort(rows)
    _flips, _overc, _tv, writes = EN._compute_stability(rows)
    end = rows[-1]["ts"] + timedelta(minutes=60)
    _n, median_ttc, _unaddr = EN._compute_responsiveness(
        rows, overrides, writes, "left", end)
    # No sign-correct write within horizon → censored at 30 min
    assert median_ttc == pytest.approx(30.0)


def test_zone_user_mapping():
    assert EN._zone_user("left") == "mike"
    assert EN._zone_user("right") == "partner"


def test_majority_controller_version():
    rows = [
        _row(0, "set", -5, controller_version="v5_2_rc_off"),
        _row(5, "set", -5, controller_version="v5_2_rc_off"),
        _row(10, "set", -5, controller_version="v6_state"),
    ]
    assert EN._majority_controller_version(rows) == "v5_2_rc_off"


def test_zero_controller_writes_yields_zero_stability_metrics():
    rows = [
        _row(0,  "passive", -5, body=80.0),
        _row(10, "override", -7, body=80.0, override_delta=-2),
    ]
    flips, overc, tv, writes = EN._compute_stability(rows)
    assert flips == 0
    assert overc is None  # no controller writes → undefined
    assert tv == 0
    assert writes == []
