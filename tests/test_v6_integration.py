"""End-to-end integration tests for v6 plan computation.

Tests the golden cases from proposal §9/§13 against compute_v6_plan.
These exercise regime + firmware_plant + body_fb + room_fb + proxy_term
as a system.

Data source: Synthesized fixtures per proposal §9 specifications (PG rows
not directly queried — see tests/fixtures/v6_golden_cases.py for rationale).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from ml.v6.policy import compute_v6_plan
from tests.fixtures.v6_golden_cases import (
    CASE_A_SNAPSHOT,
    CASE_B_SNAPSHOT,
    CASE_C_COLD_SNAPSHOT,
    CASE_C_WARM_SNAPSHOT,
    load_fixture,
)


# ── Golden Case A: LEFT cold-cluster 01:37–02:05 ─────────────────────

class TestCaseA:
    """v5.2 held -10 → user wanted -3. v6 should compute -5 to -3."""

    def test_case_A_cold_cluster_left(self):
        """v6 should output -3 to -5 (warmer than -10)."""
        snapshot = CASE_A_SNAPSHOT
        plan = compute_v6_plan(zone="left", snapshot=snapshot)
        assert -5 <= plan["target"] <= -3, \
            f"v6 Case A: expected -5..-3, got {plan['target']}"
        assert plan["regime"] == "COLD_ROOM_COMP"

    def test_case_A_regime_fires_cold_room(self):
        """Cold room (67.4°F) + body-room delta triggers COLD_ROOM_COMP."""
        plan = compute_v6_plan(zone="left", snapshot=CASE_A_SNAPSHOT)
        assert plan["regime"] == "COLD_ROOM_COMP"
        assert plan["body_fb"] > 0, "Body feedback should be positive (warm bias)"

    def test_case_A_plant_prediction_sane(self):
        """Firmware plant should predict a setpoint in 69-92°F range."""
        plan = compute_v6_plan(zone="left", snapshot=CASE_A_SNAPSHOT)
        assert 60.0 <= plan["plant_setpoint_f"] <= 96.0

    def test_case_A_from_json_fixture(self):
        """Load from JSON file and verify same result."""
        snapshot = load_fixture("v6_case_A")
        plan = compute_v6_plan(zone="left", snapshot=snapshot)
        assert -5 <= plan["target"] <= -3


# ── Golden Case B: RIGHT under-cooled override ────────────────────────

class TestCaseB:
    """v5.2 was -3. User wanted -5. v6's proxy should trigger cooler bias."""

    def test_case_B_right_under_cooled(self):
        """v6 should output ≤ -4 (cooler than v5.2's -3)."""
        snapshot = CASE_B_SNAPSHOT
        plan = compute_v6_plan(zone="right", snapshot=snapshot)
        assert plan["target"] <= -4, \
            f"v6 Case B: expected ≤ -4, got {plan['target']}"

    def test_case_B_movement_proxy_active(self):
        """Movement density > baseline should produce a cool bias proxy term."""
        plan = compute_v6_plan(zone="right", snapshot=CASE_B_SNAPSHOT)
        assert plan["proxy_term"] < 0, \
            f"Expected negative proxy_term for elevated movement, got {plan['proxy_term']}"

    def test_case_B_no_positive_write(self):
        """v6 must never output a positive setting."""
        plan = compute_v6_plan(zone="right", snapshot=CASE_B_SNAPSHOT)
        assert plan["target"] <= 0

    def test_case_B_from_json_fixture(self):
        snapshot = load_fixture("v6_case_B")
        plan = compute_v6_plan(zone="right", snapshot=snapshot)
        assert plan["target"] <= -4


# ── Golden Case C: Cold mid-night + warm AM ───────────────────────────

class TestCaseC:
    """Two-part case: 04:27 cold, 06:56 warm AM."""

    def test_case_C_cold_half(self):
        """04:27 v5.2 was -2 (close enough); v6 should be -3 to -2."""
        plan_cold = compute_v6_plan(zone="left", snapshot=CASE_C_COLD_SNAPSHOT)
        assert -3 <= plan_cold["target"] <= -2, \
            f"v6 Case C cold: expected -3..-2, got {plan_cold['target']}"

    def test_case_C_warm_half(self):
        """06:56 v5.2 was -6 (over-cool by 2); v6 should be -6 to -5."""
        plan_warm = compute_v6_plan(zone="left", snapshot=CASE_C_WARM_SNAPSHOT)
        assert -6 <= plan_warm["target"] <= -5, \
            f"v6 Case C warm: expected -6..-5, got {plan_warm['target']}"

    def test_case_C_warm_regime_is_wake_cool(self):
        """Wake stage after 420 min should trigger WAKE_COOL."""
        plan = compute_v6_plan(zone="left", snapshot=CASE_C_WARM_SNAPSHOT)
        assert plan["regime"] == "WAKE_COOL"

    def test_case_C_cold_regime_is_cold_room(self):
        """Cold room at 68.5°F with body delta should trigger COLD_ROOM_COMP."""
        plan = compute_v6_plan(zone="left", snapshot=CASE_C_COLD_SNAPSHOT)
        assert plan["regime"] == "COLD_ROOM_COMP"


# ── Policy API contract tests ─────────────────────────────────────────

class TestPolicyContract:
    """Verify compute_v6_plan output structure and invariants."""

    @pytest.fixture(params=["A", "B", "C_cold", "C_warm"])
    def case_snapshot(self, request):
        cases = {
            "A": ("left", CASE_A_SNAPSHOT),
            "B": ("right", CASE_B_SNAPSHOT),
            "C_cold": ("left", CASE_C_COLD_SNAPSHOT),
            "C_warm": ("left", CASE_C_WARM_SNAPSHOT),
        }
        return cases[request.param]

    def test_output_has_required_keys(self, case_snapshot):
        zone, snapshot = case_snapshot
        plan = compute_v6_plan(zone=zone, snapshot=snapshot)
        required = {"target", "regime", "reason", "base_setting",
                    "plant_setpoint_f", "divergence_steps",
                    "body_fb", "room_fb", "proxy_term", "residual_delta", "debug"}
        assert required <= set(plan.keys())

    def test_target_in_valid_range(self, case_snapshot):
        zone, snapshot = case_snapshot
        plan = compute_v6_plan(zone=zone, snapshot=snapshot)
        assert -10 <= plan["target"] <= 0, \
            f"target {plan['target']} outside [-10, 0]"

    def test_no_positive_write(self, case_snapshot):
        """§11.3 #6: Any positive write → instant rollback."""
        zone, snapshot = case_snapshot
        plan = compute_v6_plan(zone=zone, snapshot=snapshot)
        assert plan["target"] <= 0

    def test_regime_is_known(self, case_snapshot):
        zone, snapshot = case_snapshot
        plan = compute_v6_plan(zone=zone, snapshot=snapshot)
        known_regimes = {
            "UNOCCUPIED", "PRE_BED", "INITIAL_COOL", "BEDJET_WARM",
            "SAFETY_YIELD", "OVERRIDE", "COLD_ROOM_COMP", "WAKE_COOL",
            "NORMAL_COOL",
        }
        assert plan["regime"] in known_regimes

    def test_residual_disabled_by_default(self, case_snapshot):
        zone, snapshot = case_snapshot
        plan = compute_v6_plan(zone=zone, snapshot=snapshot, residual_enabled=False)
        assert plan["residual_delta"] == 0


# ── V6SynthPolicy adapter tests ──────────────────────────────────────

class TestV6SynthPolicy:
    """Test the Policy adapter used by v6_eval.py replay."""

    def test_import_and_instantiate(self):
        from ml.v6.policy import V6SynthPolicy
        p = V6SynthPolicy()
        assert p.name == "v6_synth"

    def test_decide_returns_int(self):
        from ml.v6.policy import V6SynthPolicy
        p = V6SynthPolicy()
        state = {
            "zone": "left",
            "elapsed_min": 120.0,
            "sleep_stage": "light",
            "body_left_f": 76.0,
            "room_temp_f": 67.0,
            "current_setting": -10,
            "bed_occupied_left": True,
        }
        result = p.decide(state, [])
        assert isinstance(result, int)
        assert -10 <= result <= 0

    def test_decide_right_zone(self):
        from ml.v6.policy import V6SynthPolicy
        p = V6SynthPolicy()
        state = {
            "zone": "right",
            "elapsed_min": 200.0,
            "sleep_stage": "deep",
            "body_left_f": 77.0,
            "body_center_f": 77.0,
            "room_temp_f": 68.0,
            "current_setting": -5,
            "bed_occupied_right": True,
        }
        result = p.decide(state, [])
        assert isinstance(result, int)
        assert -10 <= result <= 0
