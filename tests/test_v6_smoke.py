"""Smoke test: v6 ML modules compose cleanly under AppDaemon-style context.

This is a "does it boot" test — verifies all v6 ML modules import cleanly,
instantiate without errors, and their public APIs are callable. Independent
of R2A's actual controller code.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest


class TestV6ModuleImports:
    """All v6 ML modules import cleanly."""

    def test_import_regime(self):
        from ml.v6 import regime
        assert hasattr(regime, "classify")
        assert hasattr(regime, "RegimeConfig")
        assert hasattr(regime, "DEFAULT_CONFIG")

    def test_import_firmware_plant(self):
        from ml.v6 import firmware_plant
        assert hasattr(firmware_plant, "FirmwarePlant")

    def test_import_right_comfort_proxy(self):
        from ml.v6 import right_comfort_proxy
        assert hasattr(right_comfort_proxy, "score")
        assert hasattr(right_comfort_proxy, "minutes_score_ge_0_5")

    def test_import_residual_head(self):
        from ml.v6 import residual_head
        assert hasattr(residual_head, "ResidualHead")
        assert hasattr(residual_head, "FEATURE_NAMES")

    def test_import_policy(self):
        from ml.v6 import policy
        assert hasattr(policy, "compute_v6_plan")
        assert hasattr(policy, "V6SynthPolicy")


class TestV6ModuleInstantiation:
    """v6 modules instantiate without errors."""

    def test_regime_config_defaults(self):
        from ml.v6.regime import RegimeConfig, DEFAULT_CONFIG
        config = RegimeConfig()
        assert config.cold_room_threshold_f == 70.0
        assert DEFAULT_CONFIG is not None

    def test_firmware_plant_no_cap_table(self):
        from ml.v6.firmware_plant import FirmwarePlant
        plant = FirmwarePlant()
        assert not plant.cap_table_loaded

    def test_firmware_plant_predict(self):
        from ml.v6.firmware_plant import FirmwarePlant
        plant = FirmwarePlant()
        result = plant.predict_setpoint_f(-5, ambient_f=70.0)
        assert isinstance(result, float)
        assert 40 <= result <= 110

    def test_residual_head_no_model(self):
        from ml.v6.residual_head import ResidualHead
        head = ResidualHead(zone="left")
        assert not head.loaded
        delta, meta = head.predict({"cycle_phase": 2.0, "room_f": 70.0})
        assert delta == 0

    def test_right_comfort_proxy_score(self):
        from ml.v6.right_comfort_proxy import score
        s = score(
            body_left_f=76.0,
            body_avg_f=75.5,
            room_f=68.0,
            movement_density_15m=0.03,
        )
        assert 0.0 <= s <= 1.0

    def test_v6_synth_policy(self):
        from ml.v6.policy import V6SynthPolicy
        p = V6SynthPolicy()
        assert p.name == "v6_synth"


class TestV6UnderMockAppDaemon:
    """v6 modules work in an AppDaemon-like mock context."""

    def test_policy_with_mocked_hass_state(self):
        """Simulate calling compute_v6_plan from an AppDaemon callback."""
        from ml.v6.policy import compute_v6_plan

        # Simulate state coming from HA entity reads
        mock_hass = MagicMock()
        mock_hass.get_state.return_value = "light"

        snapshot = {
            "zone": "left",
            "elapsed_min": 180.0,
            "mins_since_onset": 180.0,
            "post_bedjet_min": None,
            "sleep_stage": "light",
            "bed_occupied": True,
            "room_f": 69.0,
            "body_skin_f": 75.0,
            "body_hot_f": 75.0,
            "body_avg_f": 74.5,
            "override_freeze_active": False,
            "right_rail_engaged": False,
            "pre_sleep_active": False,
            "three_level_off": True,
            "movement_density_15m": 0.02,
            "current_setting": -7,
        }

        plan = compute_v6_plan("left", snapshot)
        assert isinstance(plan["target"], int)
        assert -10 <= plan["target"] <= 0
        assert plan["regime"] in {
            "UNOCCUPIED", "PRE_BED", "INITIAL_COOL", "BEDJET_WARM",
            "SAFETY_YIELD", "OVERRIDE", "COLD_ROOM_COMP", "WAKE_COOL",
            "NORMAL_COOL",
        }

    def test_all_modules_work_together(self):
        """Integration: classify → plant predict → proxy score."""
        from ml.v6.regime import classify, DEFAULT_CONFIG
        from ml.v6.firmware_plant import FirmwarePlant
        from ml.v6.right_comfort_proxy import score

        # Step 1: classify
        result = classify(
            "right",
            elapsed_min=200.0,
            mins_since_onset=200.0,
            post_bedjet_min=None,
            sleep_stage="deep",
            bed_occupied=True,
            room_f=69.0,
            body_skin_f=74.0,
            body_hot_f=77.0,
            body_avg_f=75.5,
            override_freeze_active=False,
            right_rail_engaged=False,
            pre_sleep_active=False,
            three_level_off=True,
        )
        assert result["regime"] in {"NORMAL_COOL", "COLD_ROOM_COMP"}

        # Step 2: plant predict
        plant = FirmwarePlant()
        setpoint = plant.predict_setpoint_f(result["base_setting"])
        assert 40 <= setpoint <= 110

        # Step 3: proxy score
        s = score(
            body_left_f=77.0,
            body_avg_f=75.5,
            room_f=69.0,
            movement_density_15m=0.04,
        )
        assert 0.0 <= s <= 1.0
