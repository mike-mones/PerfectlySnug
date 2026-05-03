"""Tests for ml.v6.firmware_plant — forward predictor."""

import json
import os
import pytest
from ml.v6.firmware_plant import FirmwarePlant


# ─── Anchor point tests ───────────────────────────────────────────────

class TestAnchorPoints:
    def setup_method(self):
        self.plant = FirmwarePlant()

    def test_anchor_minus_8(self):
        """L1=-8 → 69°F (within 0.1°F)."""
        assert abs(self.plant.predict_setpoint_f(-8) - 69.0) < 0.1

    def test_anchor_zero(self):
        """L1=0 → 91.4°F (within 0.1°F)."""
        assert abs(self.plant.predict_setpoint_f(0) - 91.4) < 0.1

    def test_anchor_plus_5(self):
        """L1=+5 → 95.9°F (within 0.1°F)."""
        assert abs(self.plant.predict_setpoint_f(5) - 95.9) < 0.1


# ─── Interpolation tests ─────────────────────────────────────────────

class TestInterpolation:
    def setup_method(self):
        self.plant = FirmwarePlant()

    def test_midpoint_minus_4(self):
        """L1=-4 should be midpoint between -8 (69°F) and 0 (91.4°F)."""
        expected = 69.0 + (91.4 - 69.0) * (4.0 / 8.0)  # 80.2°F
        actual = self.plant.predict_setpoint_f(-4)
        assert abs(actual - expected) < 0.1

    def test_midpoint_plus_2(self):
        """L1=+2 between 0 (91.4°F) and +5 (95.9°F)."""
        expected = 91.4 + (95.9 - 91.4) * (2.0 / 5.0)  # 93.2°F
        actual = self.plant.predict_setpoint_f(2)
        assert abs(actual - expected) < 0.1


# ─── Monotonicity ─────────────────────────────────────────────────────

class TestMonotonicity:
    def test_setpoint_monotonically_increasing(self):
        """Higher L_active → higher setpoint (warmer)."""
        plant = FirmwarePlant()
        prev = plant.predict_setpoint_f(-10)
        for setting in range(-9, 6):
            curr = plant.predict_setpoint_f(setting)
            assert curr >= prev, f"Monotonicity violation at setting={setting}"
            prev = curr


# ─── Extrapolation ────────────────────────────────────────────────────

class TestExtrapolation:
    def setup_method(self):
        self.plant = FirmwarePlant()

    def test_below_min_anchor(self):
        """Below lowest anchor → constant (69°F)."""
        assert self.plant.predict_setpoint_f(-10) == 69.0
        assert self.plant.predict_setpoint_f(-15) == 69.0

    def test_above_max_anchor(self):
        """Above highest anchor → constant (95.9°F)."""
        assert self.plant.predict_setpoint_f(7) == 95.9
        assert self.plant.predict_setpoint_f(10) == 95.9


# ─── Cap table loading ────────────────────────────────────────────────

class TestCapTableLoading:
    def test_fallback_without_cap_table(self):
        """Falls back gracefully if cap table missing."""
        plant = FirmwarePlant(cap_table_path="/nonexistent/path.json")
        assert not plant.cap_table_loaded
        # Still produces valid predictions from default anchors
        assert abs(plant.predict_setpoint_f(0) - 91.4) < 0.1

    def test_loads_cap_table(self, tmp_path):
        """Loads correctly when cap table present."""
        cap_data = {
            "anchors": [
                {"setting": -10, "setpoint_f": 65.0},
                {"setting": -5, "setpoint_f": 78.0},
                {"setting": 0, "setpoint_f": 90.0},
                {"setting": 5, "setpoint_f": 96.0},
            ]
        }
        cap_file = tmp_path / "cap_table.json"
        cap_file.write_text(json.dumps(cap_data))

        plant = FirmwarePlant(cap_table_path=str(cap_file))
        assert plant.cap_table_loaded
        assert abs(plant.predict_setpoint_f(-10) - 65.0) < 0.1
        assert abs(plant.predict_setpoint_f(0) - 90.0) < 0.1

    def test_invalid_cap_table_format(self, tmp_path):
        """Invalid format falls back to anchors."""
        cap_file = tmp_path / "bad.json"
        cap_file.write_text('{"anchors": []}')  # too few points

        plant = FirmwarePlant(cap_table_path=str(cap_file))
        assert not plant.cap_table_loaded
        assert abs(plant.predict_setpoint_f(0) - 91.4) < 0.1

    def test_loads_table_format_from_fit_tool(self, tmp_path):
        """Round-trip: fit_from_rows → write_table → FirmwarePlant loads it."""
        from tools.firmware_cap_fit import (
            build_table, fit_from_rows, write_table,
        )

        rows = [
            (-8, 69.0), (-8, 70.0),
            (-4, 80.0), (-4, 81.0),
            (0, 91.0), (0, 92.0),
            (5, 95.0), (5, 96.5),
        ]
        points = fit_from_rows(rows)
        table = build_table(points, since=None)
        cap_file = tmp_path / "fit.json"
        write_table(table, cap_file)

        plant = FirmwarePlant(cap_table_path=str(cap_file))
        assert plant.cap_table_loaded
        # Median of (-8 -> 69, 70) ≈ 69.5
        assert abs(plant.predict_setpoint_f(-8) - 69.5) < 0.6

    def test_loads_committed_cap_table(self):
        """The repo's committed cap table file loads cleanly."""
        committed = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ml", "v6", "firmware_cap_table.json",
        )
        if not os.path.isfile(committed):
            pytest.skip("committed cap table not present")
        plant = FirmwarePlant(cap_table_path=committed)
        assert plant.cap_table_loaded


# ─── Blower prediction ───────────────────────────────────────────────

class TestBlowerPrediction:
    def setup_method(self):
        self.plant = FirmwarePlant()

    def test_body_below_setpoint_low_blower(self):
        """Body well below setpoint → low or zero blower."""
        # Setting 0 → setpoint 91.4°F, body at 78°F → error is very negative
        blower = self.plant.predict_blower_pct(0, 72.0, 78.0)
        # Large negative error → target < 16.4 → blower = 0
        assert blower == 0.0

    def test_body_above_setpoint_high_blower(self):
        """Body above setpoint → positive blower."""
        # Setting -8 → setpoint 69°F, body at 80°F → large positive error
        blower = self.plant.predict_blower_pct(-8, 72.0, 80.0)
        assert blower > 0.0

    def test_blower_clamped_100(self):
        """Blower never exceeds 100%."""
        blower = self.plant.predict_blower_pct(-8, 60.0, 95.0)
        assert blower <= 100.0


# ─── Step one minute ──────────────────────────────────────────────────

class TestStepOneMinute:
    def test_state_advances(self):
        plant = FirmwarePlant()
        state = {"setpoint_f": 80.0, "elapsed_sec": 0}
        new_state = plant.step_one_minute(state, -5, 72.0, 79.0)
        assert new_state["elapsed_sec"] == 60
        assert "setpoint_f" in new_state
        assert "blower_pct" in new_state

    def test_setpoint_leaks_down(self):
        """Setpoint should leak down when body is below previous setpoint."""
        plant = FirmwarePlant()
        state = {"setpoint_f": 85.0, "elapsed_sec": 0}
        new_state = plant.step_one_minute(state, -5, 72.0, 78.0)
        # Body (78) < prev setpoint (85) but max(body, leaked) still high
        # leaked = 85 - 0.012 = 84.988
        assert new_state["setpoint_f"] < 85.0
