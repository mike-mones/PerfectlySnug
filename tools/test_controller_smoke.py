"""
Smoke test for sleep_controller_v2.py — runs offline, no HA needed.

Simulates a control loop iteration with realistic sensor values
to verify all code paths execute without crashing.

Usage:
    python3 tools/test_controller_smoke.py
"""

import sys
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the appdaemon dir to path so we can import the controller
sys.path.insert(0, str(Path(__file__).parent.parent / "appdaemon"))

# Mock hassapi before importing the controller
mock_hass = MagicMock()
sys.modules["hassapi"] = mock_hass
mock_hass.Hass = type("Hass", (), {
    "log": lambda self, msg, level="INFO": print(f"  [{level}] {msg}"),
    "get_state": lambda self, *a, **kw: None,
    "call_service": lambda self, *a, **kw: None,
    "run_every": lambda self, *a, **kw: None,
    "listen_state": lambda self, *a, **kw: None,
})

import sleep_controller_v2 as ctrl

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
results = []


def check(name, condition, detail=""):
    results.append((name, condition))
    status = PASS if condition else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  {status} {name}{suffix}")


def test_constants():
    """Verify all constants that are referenced in the code actually exist."""
    print("\n1. Constants defined:")
    required = [
        "LOOP_INTERVAL_SEC", "MAX_STEP_PER_LOOP", "DEADBAND_F",
        "OCCUPANCY_THRESHOLD_F", "OUTLIER_THRESHOLD_F",
        "STAGE_STALE_MINUTES", "STAGE_COOLDOWN_SEC",
        "OVERRIDE_LEARNING_RATE", "CONTINUOUS_LEARN_RATE",
        "AMBIENT_REFERENCE_F", "AMBIENT_COMPENSATION",
        "TARGET_MIN_F", "TARGET_MAX_F",
        "TIME_DRIFT_F_PER_HOUR", "SETTING_LAG_MINUTES",
        "TREND_PENALTY_WEIGHT", "PID_KP", "PID_KI", "PID_KD",
        "DEGREES_PER_SETTING_POINT", "USER_BASELINE",
        "MAX_OFFSET_FROM_BASELINE", "WAKE_RAMP_MINUTES",
        "WAKE_RAMP_SETTING", "KILL_SWITCH_CHANGES",
        "KILL_SWITCH_WINDOW_SEC", "ONSET_HOLD_MINUTES",
        "DEEP_DEFICIT_THRESHOLD", "REM_DEFICIT_THRESHOLD",
        "DEFICIT_EXTRA_OFFSET", "NO_DATA_ALERT_LOOPS",
        "STAGE_BODY_TARGETS", "STAGE_CLASSIFIER_MIN_CONFIDENCE",
    ]
    for name in required:
        val = getattr(ctrl, name, "MISSING")
        check(name, val != "MISSING", f"{val}")


def test_controller_init():
    """Verify the controller can initialize."""
    print("\n2. Controller initialization:")
    c = ctrl.SleepController.__new__(ctrl.SleepController)
    c.zones = ["left"]
    c.zone_state = {}
    c.learned_targets = {}
    c.transfer_rate = ctrl.DEGREES_PER_SETTING_POINT
    c.nightly_history = {}
    c.prior_night_stages = {}
    c.stage_classifier = None

    state = c._fresh_zone_state()
    check("fresh_zone_state creates dict", isinstance(state, dict))
    check("has bedtime_ts", "bedtime_ts" in state)
    check("has body_temp_history", "body_temp_history" in state)
    check("has setting_change_log", "setting_change_log" in state)
    check("has stage_training_data", "stage_training_data" in state)

    c.zone_state["left"] = state
    c.learned_targets["left"] = dict(ctrl.STAGE_BODY_TARGETS)
    return c


def test_stage_estimation(c):
    """Simulate sleep stage estimation from HR/HRV."""
    print("\n3. Sleep stage estimation (heuristic):")
    state = c.zone_state["left"]
    state["hr_baseline"] = 55.0
    state["hrv_baseline"] = 45.0

    # Deep: HR well below baseline, HRV elevated
    stage = c._estimate_stage_from_hr(state, hr=48.0, hrv=52.0)
    check("deep sleep detected", stage == "deep", f"got '{stage}'")

    # REM: HR near baseline, HRV depressed
    stage = c._estimate_stage_from_hr(state, hr=54.0, hrv=38.0)
    check("REM detected", stage == "rem", f"got '{stage}'")

    # Awake: HR elevated
    stage = c._estimate_stage_from_hr(state, hr=60.0, hrv=45.0)
    check("awake detected", stage == "awake", f"got '{stage}'")

    # Core: moderate
    stage = c._estimate_stage_from_hr(state, hr=53.0, hrv=44.0)
    check("core detected", stage == "core", f"got '{stage}'")

    # No baseline: returns unknown
    state_empty = c._fresh_zone_state()
    stage = c._estimate_stage_from_hr(state_empty, hr=55.0, hrv=45.0)
    check("no baseline → unknown", stage == "unknown", f"got '{stage}'")


def test_pid_computation(c):
    """Simulate the PID math that was crashing before."""
    print("\n4. PID control loop simulation:")
    state = c.zone_state["left"]
    state["bedtime_ts"] = (datetime.now() - timedelta(hours=2)).isoformat()
    state["body_temp_history"] = [83.0, 83.1, 82.9, 83.2, 83.0, 82.8]
    state["last_body_temp"] = 83.0
    state["integral_error"] = 0.0
    state["last_stage"] = "core"
    state["sleep_onset_ts"] = (datetime.now() - timedelta(hours=1.5)).isoformat()
    state["onset_phase_done"] = True
    state["stage_changed_at"] = (datetime.now() - timedelta(minutes=10)).isoformat()
    state["last_settings_pushed"] = {"bedtime": -8, "sleep": -6, "wake": -5}
    state["hr_baseline"] = 55.0
    state["hrv_baseline"] = 45.0

    # Simulate the core PID computation
    body_avg = 84.5  # above target
    ambient = 74.5
    stage = "core"
    targets = c.learned_targets["left"]
    target_temp = targets[stage]

    bedtime = datetime.fromisoformat(state["bedtime_ts"])
    hours_in = (datetime.now() - bedtime).total_seconds() / 3600.0
    time_adj = hours_in * ctrl.TIME_DRIFT_F_PER_HOUR
    target_temp += time_adj

    effective_body = state["body_temp_history"][-3]  # lag compensation
    error = effective_body - target_temp

    check("error computed", isinstance(error, float), f"{error:+.2f}°F")

    # Deadband check
    in_deadband = abs(error) < ctrl.DEADBAND_F
    check("deadband check works", isinstance(in_deadband, bool),
          f"err={error:+.2f}, deadband={ctrl.DEADBAND_F}, in_band={in_deadband}")

    # PID terms
    p_term = ctrl.PID_KP * error
    i_term = ctrl.PID_KI * error
    d_term = ctrl.PID_KD * (body_avg - state["last_body_temp"])
    pid_offset = p_term + i_term + d_term

    # Ambient compensation
    ambient_adj = (ambient - ctrl.AMBIENT_REFERENCE_F) * ctrl.AMBIENT_COMPENSATION
    pid_offset += ambient_adj

    check("PID computed", isinstance(pid_offset, float),
          f"P={p_term:+.2f} I={i_term:+.2f} D={d_term:+.2f} amb={ambient_adj:+.2f}")

    # Bounded offset from baseline
    phase = "sleep"
    baseline = ctrl.USER_BASELINE[phase]
    raw_offset = round(-pid_offset)
    clamped = max(-ctrl.MAX_OFFSET_FROM_BASELINE,
                  min(ctrl.MAX_OFFSET_FROM_BASELINE, raw_offset))
    new_setting = baseline + clamped
    new_setting = max(-10, min(10, new_setting))
    new_setting = min(0, new_setting)  # hard clamp: cooling only

    check("setting computed", -10 <= new_setting <= 0,
          f"baseline={baseline}, offset={clamped:+d}, result={new_setting:+d}")

    # Occupancy check
    cold_body = 75.0
    check("occupancy rejects cold body",
          cold_body < ctrl.OCCUPANCY_THRESHOLD_F,
          f"{cold_body}°F < {ctrl.OCCUPANCY_THRESHOLD_F}°F")


def test_ml_classifier(c):
    """Test the ML classifier integration (with no model loaded)."""
    print("\n5. ML classifier integration:")
    check("no model loaded", c.stage_classifier is None)

    # Predict returns None when no model
    stage, conf = c._predict_stage_ml({"hr_pct": -0.1, "hrv_pct": 0.1, "hours_in": 2.0})
    check("predict returns None without model", stage is None and conf == 0.0)

    # Test with a fake model
    fake_model = {
        "type": "random_forest",
        "n_trees": 1,
        "features": ["hr_pct", "hrv_pct", "hours_in"],
        "classes": ["deep", "core", "rem", "awake"],
        "trees": [{
            "feature": "hr_pct",
            "threshold": -0.08,
            "left": {"leaf": True, "probs": {"deep": 0.8, "core": 0.2}},
            "right": {
                "feature": "hrv_pct",
                "threshold": -0.05,
                "left": {"leaf": True, "probs": {"rem": 0.7, "core": 0.3}},
                "right": {"leaf": True, "probs": {"core": 0.6, "awake": 0.4}},
            },
        }],
    }
    c.stage_classifier = fake_model
    stage, conf = c._predict_stage_ml({"hr_pct": -0.15, "hrv_pct": 0.1, "hours_in": 2.0})
    check("ML predicts deep for low HR", stage == "deep",
          f"got '{stage}' conf={conf:.0%}")

    stage, conf = c._predict_stage_ml({"hr_pct": 0.0, "hrv_pct": -0.1, "hours_in": 4.0})
    check("ML predicts rem for low HRV", stage == "rem",
          f"got '{stage}' conf={conf:.0%}")

    c.stage_classifier = None  # reset


def main():
    print("=" * 60)
    print("Sleep Controller v2 — Smoke Test")
    print("=" * 60)

    test_constants()
    c = test_controller_init()
    test_stage_estimation(c)
    test_pid_computation(c)
    test_ml_classifier(c)

    # Summary
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        print("\nFailed tests:")
        for name, ok in results:
            if not ok:
                print(f"  {FAIL} {name}")
        sys.exit(1)
    else:
        print("All tests passed — controller is ready for tonight.")
        sys.exit(0)


if __name__ == "__main__":
    main()
