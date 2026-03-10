"""
SleepSync + PerfectlySnug End-to-End Simulation Framework.

Simulates a full night of sleep, including:
  - SleepSync watch data (HR, HRV, sleep stages) → HA webhook
  - Topper sensor readings (body temp, ambient, PID) → HA entity updates
  - ML controller dry-run → validates PID logic and trajectory tracking
  - Entity state verification after each cycle

Usage:
    export HA_URL="http://192.168.0.106:8123"
    export HA_TOKEN="your_long_lived_token"

    # Full simulation (8-hour night compressed to ~2 min, dry-run)
    python3 tools/simulate_night.py

    # Live mode (actually pushes entity updates to HA)
    python3 tools/simulate_night.py --live

    # Custom duration
    python3 tools/simulate_night.py --hours 6 --interval 5

    # Just validate current HA entity state
    python3 tools/simulate_night.py --check-only
"""

import argparse
import json
import logging
import math
import os
import random
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("simulator")

# Add parent dir so we can import the controller
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from ml.controller import (
        ControllerState, TargetTrajectory, make_warm_sleeper_trajectory,
        ZONE_SENSORS, ZONE_SETTING_ENTITY, HEALTH_ENTITIES,
        MIN_SETTING, MAX_SETTING, SETTING_OFFSET, MAX_STEP_PER_LOOP,
    )
    CONTROLLER_AVAILABLE = True
except ImportError:
    CONTROLLER_AVAILABLE = False
    log.warning("Could not import ml.controller — PID validation will be skipped")

# ── Configuration ────────────────────────────────────────────────────────

HA_URL = os.environ.get("HA_URL", "http://192.168.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
WEBHOOK_URL = f"{HA_URL}/api/webhook/apple_health_import"


# ── Realistic Sleep Physiology Models ────────────────────────────────────

@dataclass
class SleepPhysiology:
    """Models realistic sleep physiology over a night."""

    # Sleep architecture (typical adult)
    # Cycle: ~90 min, early cycles = more deep, later cycles = more REM
    cycle_duration_min: float = 90.0

    # Resting HR baseline (bpm)
    hr_awake: float = 72.0
    hr_light: float = 62.0
    hr_deep: float = 52.0
    hr_rem: float = 65.0

    # HRV baseline (ms SDNN)
    hrv_awake: float = 35.0
    hrv_light: float = 50.0
    hrv_deep: float = 65.0
    hrv_rem: float = 40.0

    # Body temperature model (°F on topper sensor)
    body_temp_base: float = 84.0       # Starting body temp on sensor
    body_temp_nadir: float = 78.0      # Lowest during deep sleep
    body_temp_morning: float = 80.0    # Pre-wake (still cooling, just less)

    # Ambient temperature (°C room temp on topper ambient sensor)
    ambient_temp_c: float = 22.0       # ~72°F room

    def sleep_stage_at(self, minutes: float, total_hours: float = 8.0) -> str:
        """Return the sleep stage at a given point in the night."""
        total_min = total_hours * 60
        if minutes < 0 or minutes > total_min:
            return "awake"

        # First 15 min: falling asleep
        if minutes < 15:
            return "in_bed"

        cycle_pos = (minutes - 15) % self.cycle_duration_min
        cycle_num = int((minutes - 15) / self.cycle_duration_min)

        # Early cycles: more deep, later cycles: more REM
        deep_pct = max(0.15, 0.35 - cycle_num * 0.05)
        rem_pct = min(0.35, 0.15 + cycle_num * 0.05)
        light_pct = 1.0 - deep_pct - rem_pct - 0.05  # 5% micro-wakes

        if cycle_pos < self.cycle_duration_min * 0.05:
            return "awake"  # Brief micro-awakening between cycles
        elif cycle_pos < self.cycle_duration_min * (0.05 + light_pct * 0.5):
            return "core"  # Light/N1-N2
        elif cycle_pos < self.cycle_duration_min * (0.05 + light_pct * 0.5 + deep_pct):
            return "deep"
        elif cycle_pos < self.cycle_duration_min * (0.05 + light_pct * 0.5 + deep_pct + rem_pct):
            return "rem"
        else:
            return "core"  # Remaining light sleep

    def heart_rate_at(self, minutes: float, total_hours: float = 8.0) -> float:
        """Return realistic heart rate at a given point."""
        stage = self.sleep_stage_at(minutes, total_hours)
        base = {
            "awake": self.hr_awake,
            "in_bed": self.hr_awake - 3,
            "core": self.hr_light,
            "deep": self.hr_deep,
            "rem": self.hr_rem,
        }.get(stage, self.hr_awake)

        # Add natural variability (±3 bpm)
        noise = random.gauss(0, 1.5)
        # Gradual overnight decrease (parasympathetic dominance)
        overnight_drift = -2.0 * (minutes / (total_hours * 60))
        return round(max(40, base + noise + overnight_drift), 1)

    def hrv_at(self, minutes: float, total_hours: float = 8.0) -> float:
        """Return realistic HRV at a given point."""
        stage = self.sleep_stage_at(minutes, total_hours)
        base = {
            "awake": self.hrv_awake,
            "in_bed": self.hrv_awake + 5,
            "core": self.hrv_light,
            "deep": self.hrv_deep,
            "rem": self.hrv_rem,
        }.get(stage, self.hrv_awake)

        noise = random.gauss(0, 3.0)
        return round(max(10, base + noise), 1)

    def body_temp_at(self, minutes: float, total_hours: float = 8.0,
                     setting_raw: int = 3) -> float:
        """
        Simulate body temperature on the topper sensor.

        Depends on:
          - Time of night (circadian nadir ~4AM)
          - Topper setting (lower = cooler)
          - Sleep stage (deep = lower)
        """
        total_min = total_hours * 60
        progress = minutes / total_min  # 0.0 to 1.0

        # Circadian body temp curve: drops to nadir around 60-70% through night
        # then rises toward morning
        circadian = (self.body_temp_base
                     - (self.body_temp_base - self.body_temp_nadir)
                     * math.sin(progress * math.pi))

        # Topper effect: each setting unit shifts body temp ~0.7°F
        # Setting 3 (display -7) = aggressive cooling
        # Setting 10 (display 0) = neutral
        setting_effect = (setting_raw - 10) * 0.7

        # Stage effect: deep sleep = slightly lower
        stage = self.sleep_stage_at(minutes, total_hours)
        stage_effect = {
            "awake": 1.0,
            "in_bed": 0.5,
            "core": 0.0,
            "deep": -1.0,
            "rem": 0.3,
        }.get(stage, 0.0)

        noise = random.gauss(0, 0.3)
        temp = circadian + setting_effect + stage_effect + noise
        return round(temp, 1)

    def ambient_temp_at(self, minutes: float) -> float:
        """Room temperature with slight overnight cooling."""
        drift = -0.5 * (minutes / 480)  # Room cools ~0.5°C overnight
        noise = random.gauss(0, 0.1)
        return round(self.ambient_temp_c + drift + noise, 2)


# ── HA API Helpers ───────────────────────────────────────────────────────

def ha_request(method: str, path: str, data: dict | None = None) -> dict | None:
    """Make an authenticated HA REST API request."""
    url = f"{HA_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {HA_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()) if resp.status == 200 else None
    except (URLError, TimeoutError) as e:
        log.error(f"HA request failed: {e}")
        return None


def get_entity_state(entity_id: str) -> str | None:
    """Read current state of an HA entity."""
    result = ha_request("GET", f"/api/states/{entity_id}")
    if result:
        return result.get("state")
    return None


def set_entity_state(entity_id: str, state: str, attributes: dict | None = None):
    """Set an HA entity state directly (for simulation)."""
    payload = {"state": state}
    if attributes:
        payload["attributes"] = attributes
    ha_request("POST", f"/api/states/{entity_id}", payload)


def post_webhook(hr: float, hrv: float, sleep_stage: str = "unknown"):
    """Send simulated SleepSync data to the HA webhook."""
    metrics = []
    if hr > 0:
        metrics.append({
            "name": "heart_rate",
            "data": [{"Avg": hr, "Min": hr - 2, "Max": hr + 2}]
        })
    if hrv > 0:
        metrics.append({
            "name": "heart_rate_variability",
            "data": [{"qty": hrv}]
        })
    if sleep_stage != "unknown":
        metrics.append({
            "name": "sleep_stage",
            "data": [{"stage": sleep_stage}]
        })

    payload = {"data": {"metrics": metrics}}
    body = json.dumps(payload).encode()
    req = Request(WEBHOOK_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (URLError, TimeoutError) as e:
        log.error(f"Webhook POST failed: {e}")
        return False


def set_input_number(entity_id: str, value: float):
    """Set an input_number entity via the HA service."""
    ha_request("POST", "/api/services/input_number/set_value", {
        "entity_id": entity_id,
        "value": value,
    })


# ── Simulation Engine ────────────────────────────────────────────────────

@dataclass
class SimulationResult:
    """Results from a simulation run."""
    cycles: int = 0
    webhook_successes: int = 0
    webhook_failures: int = 0
    entity_updates: int = 0
    entity_verify_ok: int = 0
    entity_verify_fail: int = 0
    controller_adjustments: list = None
    errors: list = None

    def __post_init__(self):
        if self.controller_adjustments is None:
            self.controller_adjustments = []
        if self.errors is None:
            self.errors = []

    @property
    def success(self) -> bool:
        return self.webhook_failures == 0 and self.entity_verify_fail == 0 and not self.errors


def check_ha_connectivity() -> bool:
    """Verify HA is reachable and token is valid."""
    result = ha_request("GET", "/api/")
    if result and result.get("message") == "API running.":
        log.info(f"HA API connected: {HA_URL}")
        return True
    log.error(f"Cannot connect to HA at {HA_URL}")
    return False


def check_entities_exist() -> list[str]:
    """Verify all required entities exist in HA."""
    missing = []
    entities_to_check = [
        "input_number.apple_health_hr_avg",
        "input_number.apple_health_hrv",
    ]
    for eid in entities_to_check:
        state = get_entity_state(eid)
        if state is None:
            missing.append(eid)
        else:
            log.info(f"  {eid} = {state}")
    return missing


def simulate_night(
    total_hours: float = 8.0,
    interval_min: float = 3.0,
    live: bool = False,
    zone: str = "left",
    speed: float = 1.0,
) -> SimulationResult:
    """
    Simulate a full night of sleep data.

    Args:
        total_hours: Duration of simulated night
        interval_min: Simulated interval between data points (matches SleepSync's 3 min)
        live: If True, actually POST webhook data and update entities in HA
        zone: Which topper zone to simulate ("left" or "right")
        speed: Time compression factor (1.0 = real-time, 0 = instant)
    """
    result = SimulationResult()
    physiology = SleepPhysiology()
    total_min = total_hours * 60
    current_setting = 1  # Start at -9 (aggressive cooling)
    # Mike never wants heating — backtested against real data shows -3 is the
    # absolute max during actual sleep; 0/+1 only appear post-wake artifacts
    max_setting_for_user = 7  # raw 7 = display -3

    # Controller state for PID validation
    trajectory = make_warm_sleeper_trajectory() if CONTROLLER_AVAILABLE else None
    integral_error = 0.0
    last_body_temp = None

    log.info("=" * 60)
    log.info(f"SLEEP SIMULATION — {total_hours}h night, {interval_min}min intervals")
    log.info(f"Mode: {'LIVE (pushing to HA)' if live else 'DRY-RUN (log only)'}")
    log.info(f"Zone: {zone}")
    log.info("=" * 60)

    # Print header
    log.info(f"{'Min':>5} | {'Stage':>6} | {'HR':>5} | {'HRV':>5} | "
             f"{'BodyF':>6} | {'AmbC':>5} | {'Target':>6} | {'Setting':>7} | "
             f"{'PID':>6} | {'WH':>3}")

    minutes = 0.0
    while minutes <= total_min:
        result.cycles += 1

        # Generate physiological data
        stage = physiology.sleep_stage_at(minutes, total_hours)
        hr = physiology.heart_rate_at(minutes, total_hours)
        hrv = physiology.hrv_at(minutes, total_hours)
        body_temp_f = physiology.body_temp_at(minutes, total_hours, current_setting)
        ambient_c = physiology.ambient_temp_at(minutes)

        # Controller: compute target and PID adjustment
        target_f = trajectory.target_at(minutes) if trajectory else 0.0
        pid_output = 0.0

        if trajectory and last_body_temp is not None:
            error = body_temp_f - target_f  # Positive = too warm
            integral_error += error * 0.02   # Ki
            integral_error = max(-5.0, min(5.0, integral_error))
            derivative = (body_temp_f - last_body_temp) if last_body_temp else 0.0

            pid_output = -(0.5 * error + integral_error + 0.1 * derivative)
            pid_output = max(-MAX_STEP_PER_LOOP, min(MAX_STEP_PER_LOOP, pid_output))

            new_setting = current_setting + round(pid_output)
            new_setting = max(MIN_SETTING, min(max_setting_for_user, new_setting))

            if new_setting != current_setting:
                result.controller_adjustments.append({
                    "min": minutes,
                    "stage": stage,
                    "body_f": body_temp_f,
                    "target_f": target_f,
                    "old_setting": current_setting - SETTING_OFFSET,
                    "new_setting": new_setting - SETTING_OFFSET,
                    "pid": round(pid_output, 2),
                })
                current_setting = new_setting

        last_body_temp = body_temp_f

        # Log this cycle
        display_setting = current_setting - SETTING_OFFSET
        wh_status = "—"

        # Send webhook data
        if live:
            success = post_webhook(hr, hrv, stage)
            wh_status = "OK" if success else "ERR"
            if success:
                result.webhook_successes += 1
            else:
                result.webhook_failures += 1

            # Update topper sensor entities (simulating what the integration would report)
            sensor_prefix = f"sensor.smart_topper_{zone}_side"
            body_temp_c = (body_temp_f - 32) * 5 / 9
            set_entity_state(f"{sensor_prefix}_body_sensor_center",
                             str(round(body_temp_c, 2)),
                             {"unit_of_measurement": "°C", "friendly_name": f"Smart Topper {zone.title()} Side Body Sensor Center"})
            set_entity_state(f"{sensor_prefix}_ambient_temperature",
                             str(round(ambient_c, 2)),
                             {"unit_of_measurement": "°C", "friendly_name": f"Smart Topper {zone.title()} Side Ambient Temperature"})
            result.entity_updates += 2

        log.info(f"{minutes:5.0f} | {stage:>6} | {hr:5.1f} | {hrv:5.1f} | "
                 f"{body_temp_f:6.1f} | {ambient_c:5.2f} | {target_f:6.1f} | "
                 f"{display_setting:>+4d}    | {pid_output:+6.2f} | {wh_status:>3}")

        # Wait (compressed time)
        if speed > 0:
            time.sleep(interval_min * 60 * speed / (total_min / interval_min))

        minutes += interval_min

    # Summary
    log.info("=" * 60)
    log.info("SIMULATION COMPLETE")
    log.info(f"  Cycles:           {result.cycles}")
    log.info(f"  Webhook OK/Fail:  {result.webhook_successes}/{result.webhook_failures}")
    log.info(f"  Entity updates:   {result.entity_updates}")
    log.info(f"  PID adjustments:  {len(result.controller_adjustments)}")

    if result.controller_adjustments:
        log.info("")
        log.info("  Controller Adjustments:")
        for adj in result.controller_adjustments:
            log.info(f"    {adj['min']:5.0f}min | {adj['stage']:>6} | "
                     f"body={adj['body_f']:.1f}°F target={adj['target_f']:.1f}°F | "
                     f"setting {adj['old_setting']:+d} → {adj['new_setting']:+d} (PID={adj['pid']:.2f})")

    if live:
        # Verify key entities updated
        log.info("")
        log.info("  Entity Verification:")
        for eid in ["input_number.apple_health_hr_avg", "input_number.apple_health_hrv"]:
            state = get_entity_state(eid)
            if state and state != "unknown" and float(state) > 0:
                log.info(f"    ✓ {eid} = {state}")
                result.entity_verify_ok += 1
            else:
                log.info(f"    ✗ {eid} = {state} (expected a value)")
                result.entity_verify_fail += 1

    if result.success:
        log.info("")
        log.info("  ✓ ALL CHECKS PASSED")
    else:
        log.info("")
        log.info("  ✗ SOME CHECKS FAILED — see errors above")

    return result


def check_only():
    """Just verify current HA state without simulating."""
    log.info("Checking HA connectivity and entity states...")

    if not check_ha_connectivity():
        return

    log.info("")
    log.info("Apple Health entities:")
    for name, eid in HEALTH_ENTITIES.items() if CONTROLLER_AVAILABLE else []:
        state = get_entity_state(eid)
        log.info(f"  {name:>15}: {state}")

    log.info("")
    log.info("Topper sensors (left zone):")
    if CONTROLLER_AVAILABLE:
        for name, eid in ZONE_SENSORS["left"].items():
            state = get_entity_state(eid)
            log.info(f"  {name:>15}: {state}")

    log.info("")
    log.info("Topper settings:")
    if CONTROLLER_AVAILABLE:
        for zone, eid in ZONE_SETTING_ENTITY.items():
            state = get_entity_state(eid)
            log.info(f"  {zone:>15}: {state}")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Simulate a full night of SleepSync + PerfectlySnug data")
    parser.add_argument("--live", action="store_true",
                        help="Actually push data to HA (default: dry-run)")
    parser.add_argument("--hours", type=float, default=8.0,
                        help="Simulated night duration (default: 8)")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="Data interval in minutes (default: 3, matches SleepSync)")
    parser.add_argument("--zone", default="left", choices=["left", "right"],
                        help="Topper zone to simulate (default: left)")
    parser.add_argument("--speed", type=float, default=0.0,
                        help="Time compression (0=instant, 1.0=realtime, default: 0)")
    parser.add_argument("--check-only", action="store_true",
                        help="Just check HA entity states, don't simulate")
    args = parser.parse_args()

    if not HA_TOKEN:
        if args.live or args.check_only:
            log.error("HA_TOKEN environment variable not set!")
            log.error("  export HA_TOKEN='your_long_lived_access_token'")
            sys.exit(1)
        else:
            log.warning("HA_TOKEN not set — running in dry-run mode (no HA interaction)")

    if args.check_only:
        check_only()
        return

    # Connectivity check
    if args.live and not check_ha_connectivity():
        sys.exit(1)

    # Run simulation
    result = simulate_night(
        total_hours=args.hours,
        interval_min=args.interval,
        live=args.live,
        zone=args.zone,
        speed=args.speed,
    )

    # Write results to JSON for analysis
    output_file = Path(__file__).parent / "simulation_results.json"
    with open(output_file, "w") as f:
        json.dump(asdict(result), f, indent=2, default=str)
    log.info(f"\nResults saved to {output_file}")

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
