"""
Reactive Sleep Temperature Controller for Perfectly Snug Smart Topper.

A closed-loop controller that reads body sensors in real-time and adjusts
the topper setting to track an ideal body temperature trajectory through
the night. Learns from manual overrides to personalize over time.

Architecture:
  - Runs as a persistent service on the HA Green (or anywhere with HA access)
  - Every LOOP_INTERVAL_SEC, reads sensors via HA REST API
  - Computes ideal setting from target trajectory + sensor feedback
  - Pushes setting via HA number.set_value service
  - Detects manual overrides and shifts the target trajectory

The sleep curve defines what body temperature SHOULD be at each point.
The controller's job is to find the topper SETTING that achieves that target.

Usage:
    export HA_TOKEN='...'
    python3 ml/controller.py                   # dry-run (logs only, no changes)
    python3 ml/controller.py --live             # live mode (pushes settings)
    python3 ml/controller.py --live --zone left # single zone
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("controller")

# ── Configuration ────────────────────────────────────────────────────────

HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

LOOP_INTERVAL_SEC = 300   # 5 minutes between adjustments
MIN_SETTING = 0           # Raw 0 = display -10 (max cooling)
MAX_SETTING = 20          # Raw 20 = display +10 (max warming)
SETTING_OFFSET = 10       # raw - 10 = display value

# How much to change per loop iteration (prevents jarring jumps)
MAX_STEP_PER_LOOP = 2     # max 2 raw units (= 2 display degrees) per cycle

# How quickly to respond to overrides (0=ignore, 1=instant, 0.5=blend)
OVERRIDE_LEARNING_RATE = 0.7

# State file for persistence across restarts
STATE_DIR = Path(__file__).parent / "state"
STATE_FILE = STATE_DIR / "controller_state.json"

# ── Entity Mappings ──────────────────────────────────────────────────────

ZONE_SENSORS = {
    "left": {
        "body_right":  "sensor.smart_topper_left_side_body_sensor_right",
        "body_center": "sensor.smart_topper_left_side_body_sensor_center",
        "body_left":   "sensor.smart_topper_left_side_body_sensor_left",
        "ambient":     "sensor.smart_topper_left_side_ambient_temperature",
        "setpoint":    "sensor.smart_topper_left_side_temperature_setpoint",
        "pid_output":  "sensor.smart_topper_left_side_pid_control_output",
        "pid_iterm":   "sensor.smart_topper_left_side_pid_integral_term",
        "pid_pterm":   "sensor.smart_topper_left_side_pid_proportional_term",
        "blower":      "sensor.smart_topper_left_side_blower_output",
        "heater_head": "sensor.smart_topper_left_side_heater_head_raw",
        "heater_foot": "sensor.smart_topper_left_side_heater_foot_raw",
        "run_progress": "sensor.smart_topper_left_side_run_progress",
    },
    "right": {
        "body_right":  "sensor.smart_topper_right_side_body_sensor_right",
        "body_center": "sensor.smart_topper_right_side_body_sensor_center",
        "body_left":   "sensor.smart_topper_right_side_body_sensor_left",
        "ambient":     "sensor.smart_topper_right_side_ambient_temperature",
        "setpoint":    "sensor.smart_topper_right_side_temperature_setpoint",
        "pid_output":  "sensor.smart_topper_right_side_pid_control_output",
        "pid_iterm":   "sensor.smart_topper_right_side_pid_integral_term",
        "pid_pterm":   "sensor.smart_topper_right_side_pid_proportional_term",
        "blower":      "sensor.smart_topper_right_side_blower_output",
        "heater_head": "sensor.smart_topper_right_side_heater_head_raw",
        "heater_foot": "sensor.smart_topper_right_side_heater_foot_raw",
        "run_progress": "sensor.smart_topper_right_side_run_progress",
    },
}

# Where we write the setting (L1 = bedtime temp, but we override it continuously)
ZONE_SETTING_ENTITY = {
    "left": "number.smart_topper_left_side_bedtime_temperature",
    "right": "number.smart_topper_right_side_bedtime_temperature",
}

# Apple Health entities (updated daily from Health Auto Export)
HEALTH_ENTITIES = {
    "wrist_temp":   "input_number.apple_health_wrist_temp",
    "hr_avg":       "input_number.apple_health_hr_avg",
    "hrv":          "input_number.apple_health_hrv",
    "resting_hr":   "input_number.apple_health_resting_hr",
    "spo2":         "input_number.apple_health_spo2",
    "resp_rate":    "input_number.apple_health_respiratory_rate",
    "sleep_deep":   "input_number.apple_health_sleep_deep_hrs",
    "sleep_rem":    "input_number.apple_health_sleep_rem_hrs",
    "sleep_core":   "input_number.apple_health_sleep_core_hrs",
    "sleep_awake":  "input_number.apple_health_sleep_awake_hrs",
}


# ── Target Body Temperature Trajectory ──────────────────────────────────
#
# This is the core of the science-based model. Instead of directly outputting
# a topper SETTING, we define what body temperature we WANT at each point
# in the night, and let the PID controller figure out the setting.
#
# Why body temp targets instead of settings?
#   - The same setting produces different body temps depending on ambient,
#     body composition, blankets, pajamas, hydration, etc.
#   - Body temp is what actually determines sleep quality.
#   - The sensor gives us ground truth to close the loop.
#
# Target body temps are based on sleep thermophysiology research:
#   - Sleep onset: rapid surface cooling needed (lower body temp = faster onset)
#   - Deep sleep: body at nadir, minimal intervention
#   - REM: thermoregulation impaired, avoid overcooling
#   - Pre-wake: natural warming, gentle assist

@dataclass
class TargetTrajectory:
    """Body temperature targets through the night (°F on the body sensor)."""

    # Target body temps at key points (what the body sensor should read)
    onset_target_f: float = 76.0    # Sleep onset: cool aggressively
    deep_target_f: float = 78.0     # Deep sleep nadir: moderate
    rem_target_f: float = 80.0      # REM: warmer to prevent cold waking
    prewake_target_f: float = 81.0  # Pre-wake: gentle warming

    # Timing (minutes after bedtime)
    onset_end_min: int = 60         # L1 phase (sleep onset)
    deep_end_min: int = 180         # ~3 hours in, first 2 deep sleep cycles done
    rem_heavy_start_min: int = 300  # ~5 hours in, REM cycles dominate
    prewake_start_min: int = 420    # ~7 hours in, pre-wake
    total_sleep_min: int = 480      # 8 hours total

    # Personal adjustments learned from overrides (accumulated offsets in °F)
    # These shift the entire trajectory or specific phases
    learned_offset_f: float = 0.0        # Global offset
    learned_onset_offset_f: float = 0.0  # Phase-specific
    learned_deep_offset_f: float = 0.0
    learned_rem_offset_f: float = 0.0
    learned_prewake_offset_f: float = 0.0

    def target_at(self, minutes_since_bedtime: float) -> float:
        """Get target body temperature at a given point in the night."""
        t = minutes_since_bedtime

        if t <= self.onset_end_min:
            # Sleep onset: linear ramp from onset to deep
            base = self.onset_target_f
            phase_offset = self.learned_onset_offset_f
        elif t <= self.deep_end_min:
            # Deep sleep dominant: interpolate onset→deep
            progress = (t - self.onset_end_min) / (self.deep_end_min - self.onset_end_min)
            base = self.onset_target_f + progress * (self.deep_target_f - self.onset_target_f)
            phase_offset = self.learned_deep_offset_f
        elif t <= self.rem_heavy_start_min:
            # Mixed deep + REM: interpolate deep→rem
            progress = (t - self.deep_end_min) / (self.rem_heavy_start_min - self.deep_end_min)
            base = self.deep_target_f + progress * (self.rem_target_f - self.deep_target_f)
            # Blend deep and REM offsets
            phase_offset = (self.learned_deep_offset_f * (1 - progress) +
                          self.learned_rem_offset_f * progress)
        elif t <= self.prewake_start_min:
            # REM-heavy: interpolate rem→prewake
            progress = (t - self.rem_heavy_start_min) / (self.prewake_start_min - self.rem_heavy_start_min)
            base = self.rem_target_f + progress * (self.prewake_target_f - self.rem_target_f)
            phase_offset = self.learned_rem_offset_f
        else:
            # Pre-wake
            base = self.prewake_target_f
            phase_offset = self.learned_prewake_offset_f

        return base + self.learned_offset_f + phase_offset


# ── Sleeper Profile (Mike's initial seed) ────────────────────────────────

def make_warm_sleeper_trajectory() -> TargetTrajectory:
    """
    Seed for a warm sleeper who prefers aggressive cooling (-7 to -9)
    but wakes up cold mid-night.

    Based on Mike's data:
      - Body sensor reads 80-85°F even at -9 setting
      - Wakes cold around 3AM (REM phase)
      - Runs warm (sweats without cooling)

    Target body temps are calibrated from his overnight data:
      - At -9, body sensor stabilized ~78-80°F during deep sleep
      - Rose to 83-85°F toward morning
      - 78°F seems to be his comfortable deep sleep temp
      - Cold waking happens when body drops below ~76°F during REM
    """
    return TargetTrajectory(
        # Aggressive cooling at onset — he likes it cold to fall asleep
        onset_target_f=76.0,
        # Deep sleep: 78°F felt comfortable based on his data
        deep_target_f=78.0,
        # REM: warm up to prevent cold waking (the key insight)
        rem_target_f=80.0,
        # Pre-wake: gentle warming
        prewake_target_f=82.0,
        onset_end_min=60,
        deep_end_min=180,
        rem_heavy_start_min=300,
        prewake_start_min=420,
        total_sleep_min=480,
    )


# ── PID-style Controller ────────────────────────────────────────────────

@dataclass
class ControllerState:
    """Persistent state for a single zone's controller."""
    zone: str
    bedtime_ts: str | None = None        # ISO timestamp of when sleep started
    current_raw_setting: int = 3         # Current topper setting (raw 0-20, 3 = display -7)
    last_setting_we_pushed: int | None = None  # What we last sent
    integral_error: float = 0.0          # Accumulated error for I-term
    last_body_temp: float | None = None  # Previous body temp reading
    trajectory: dict = field(default_factory=dict)  # Serialized TargetTrajectory
    override_history: list = field(default_factory=list)  # Recent overrides

    # PID tuning
    kp: float = 0.5    # Proportional gain (setting units per °F error)
    ki: float = 0.02   # Integral gain (slow drift correction)
    kd: float = 0.1    # Derivative gain (rate of change damping)
    integral_limit: float = 5.0  # Anti-windup


# ── HA API Helpers ───────────────────────────────────────────────────────

def ha_get_state(entity_id: str) -> float | None:
    """Read a single entity's state from HA REST API."""
    try:
        req = Request(
            f"{HA_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            val = data.get("state")
            if val in ("unknown", "unavailable", None, ""):
                return None
            return float(val)
    except (URLError, ValueError, KeyError) as e:
        log.debug("Failed to read %s: %s", entity_id, e)
        return None


def ha_set_number(entity_id: str, value: float) -> bool:
    """Set a number entity via HA REST API."""
    try:
        payload = json.dumps({
            "entity_id": entity_id,
            "value": str(value),
        }).encode()
        req = Request(
            f"{HA_URL}/api/services/number/set_value",
            data=payload,
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except URLError as e:
        log.error("Failed to set %s to %s: %s", entity_id, value, e)
        return False


def ha_fire_event(event_type: str, event_data: dict) -> bool:
    """Fire an HA event for logging/automation triggers."""
    try:
        payload = json.dumps(event_data).encode()
        req = Request(
            f"{HA_URL}/api/events/{event_type}",
            data=payload,
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except URLError as e:
        log.debug("Failed to fire event %s: %s", event_type, e)
        return False


# ── Sensor Reading ───────────────────────────────────────────────────────

@dataclass
class SensorSnapshot:
    """Point-in-time sensor reading for a zone."""
    timestamp: datetime
    body_right: float | None = None
    body_center: float | None = None
    body_left: float | None = None
    body_avg: float | None = None
    ambient: float | None = None
    setpoint: float | None = None
    pid_output: float | None = None
    blower: float | None = None
    run_progress: float | None = None
    # Apple Health (nightly values)
    wrist_temp: float | None = None
    hr_avg: float | None = None
    hrv: float | None = None


def read_sensors(zone: str) -> SensorSnapshot:
    """Read all sensors for a zone from HA."""
    snap = SensorSnapshot(timestamp=datetime.now())
    entities = ZONE_SENSORS[zone]

    for key, entity_id in entities.items():
        val = ha_get_state(entity_id)
        if val is not None and hasattr(snap, key):
            setattr(snap, key, val)

    # Compute body average
    body_vals = [v for v in [snap.body_right, snap.body_center, snap.body_left]
                 if v is not None]
    if body_vals:
        snap.body_avg = sum(body_vals) / len(body_vals)

    # Read Apple Health data
    for key, entity_id in HEALTH_ENTITIES.items():
        val = ha_get_state(entity_id)
        if val is not None and hasattr(snap, key):
            setattr(snap, key, val)

    return snap


# ── Core Control Loop ───────────────────────────────────────────────────

def compute_setting(
    state: ControllerState,
    snap: SensorSnapshot,
    trajectory: TargetTrajectory,
    dry_run: bool = True,
) -> int | None:
    """
    Compute the next topper setting based on sensor feedback and target trajectory.

    Returns the recommended raw setting (0-20), or None if we can't compute.
    """
    if snap.body_avg is None:
        log.warning("[%s] No body temperature available — skipping", state.zone)
        return None

    # Figure out where we are in the night
    if state.bedtime_ts is None:
        log.info("[%s] No bedtime set — cannot compute trajectory position", state.zone)
        return None

    bedtime = datetime.fromisoformat(state.bedtime_ts)
    minutes_in = (snap.timestamp - bedtime).total_seconds() / 60.0

    if minutes_in < 0 or minutes_in > trajectory.total_sleep_min + 60:
        log.info("[%s] Outside sleep window (%.0f min) — controller inactive",
                 state.zone, minutes_in)
        return None

    # Get target body temp for this point in the night
    target_body_f = trajectory.target_at(minutes_in)

    # Error: positive = too warm, negative = too cold
    error = snap.body_avg - target_body_f

    # PID computation
    # P-term: proportional to current error
    p_term = state.kp * error

    # I-term: accumulated error (catches slow drift)
    state.integral_error += error
    state.integral_error = max(-state.integral_limit,
                               min(state.integral_limit, state.integral_error))
    i_term = state.ki * state.integral_error

    # D-term: rate of change (prevents oscillation)
    d_term = 0.0
    if state.last_body_temp is not None:
        rate = snap.body_avg - state.last_body_temp  # °F per loop interval
        d_term = state.kd * rate
    state.last_body_temp = snap.body_avg

    # Total PID output: how much to adjust (positive = need more cooling)
    pid_adjustment = p_term + i_term + d_term

    # Convert PID output to setting change
    # PID says "need X degrees more cooling" → subtract from current setting
    # (lower setting = more cooling)
    new_raw = state.current_raw_setting - pid_adjustment

    # Clamp to valid range
    new_raw = max(MIN_SETTING, min(MAX_SETTING, new_raw))
    new_raw = round(new_raw)

    # Rate-limit: don't jump more than MAX_STEP_PER_LOOP per cycle
    delta = new_raw - state.current_raw_setting
    if abs(delta) > MAX_STEP_PER_LOOP:
        new_raw = state.current_raw_setting + (MAX_STEP_PER_LOOP if delta > 0 else -MAX_STEP_PER_LOOP)

    display_current = state.current_raw_setting - SETTING_OFFSET
    display_new = new_raw - SETTING_OFFSET
    display_target = target_body_f

    phase = "onset" if minutes_in <= trajectory.onset_end_min else \
            "deep" if minutes_in <= trajectory.deep_end_min else \
            "rem" if minutes_in <= trajectory.rem_heavy_start_min else \
            "prewake" if minutes_in <= trajectory.prewake_start_min else "wake"

    log.info(
        "[%s] t+%.0fmin (%s) | body=%.1f°F target=%.1f°F err=%+.1f°F | "
        "P=%+.2f I=%+.2f D=%+.2f | setting: %+d → %+d (raw %d→%d)",
        state.zone, minutes_in, phase,
        snap.body_avg, display_target, error,
        p_term, i_term, d_term,
        display_current, display_new,
        state.current_raw_setting, new_raw,
    )

    return int(new_raw)


def detect_override(state: ControllerState, snap: SensorSnapshot) -> dict | None:
    """
    Detect if the user manually changed the setting since our last push.

    The HA integration fires events for overrides, but we also check here
    for robustness (in case the event was missed).
    """
    if state.last_setting_we_pushed is None:
        return None

    # Read the actual current setting from HA (display value)
    entity = ZONE_SETTING_ENTITY[state.zone]
    current_display = ha_get_state(entity)
    if current_display is None:
        return None

    current_raw = int(current_display) + SETTING_OFFSET
    expected_raw = state.last_setting_we_pushed

    if current_raw != expected_raw:
        delta_display = (current_raw - SETTING_OFFSET) - (expected_raw - SETTING_OFFSET)
        log.info(
            "[%s] MANUAL OVERRIDE detected: expected %+d, found %+d (delta %+d)",
            state.zone, expected_raw - SETTING_OFFSET,
            current_raw - SETTING_OFFSET, delta_display,
        )
        return {
            "zone": state.zone,
            "expected_raw": expected_raw,
            "actual_raw": current_raw,
            "delta_raw": current_raw - expected_raw,
            "timestamp": datetime.now().isoformat(),
        }
    return None


def learn_from_override(
    trajectory: TargetTrajectory,
    state: ControllerState,
    override: dict,
) -> None:
    """
    Adjust the target trajectory based on a manual override.

    Core insight: if the user made it colder, the target body temp at this
    phase was too HIGH (body was comfortable but user wants cooler).
    If they made it warmer, target was too LOW (body was too cold).

    The delta tells us how much to shift the target.
    """
    if state.bedtime_ts is None:
        return

    bedtime = datetime.fromisoformat(state.bedtime_ts)
    minutes_in = (datetime.now() - bedtime).total_seconds() / 60.0

    # The user's adjustment direction: positive delta = they wanted warmer
    # This means our target body temp was too LOW → raise it
    # Negative delta = wanted cooler → target was too HIGH → lower it
    delta_raw = override["delta_raw"]

    # Convert setting delta to approximate body temp delta
    # Empirically, 1 setting unit ≈ 0.5-1.0°F body temp change
    body_temp_shift = delta_raw * 0.7  # °F per setting unit

    # Apply learning rate
    adjustment = body_temp_shift * OVERRIDE_LEARNING_RATE

    # Determine which phase to adjust
    if minutes_in <= trajectory.onset_end_min:
        trajectory.learned_onset_offset_f += adjustment
        phase = "onset"
    elif minutes_in <= trajectory.deep_end_min:
        trajectory.learned_deep_offset_f += adjustment
        phase = "deep"
    elif minutes_in <= trajectory.rem_heavy_start_min:
        trajectory.learned_rem_offset_f += adjustment
        phase = "rem"
    else:
        trajectory.learned_prewake_offset_f += adjustment
        phase = "prewake"

    # Also apply a smaller global shift (entire night moves a bit)
    trajectory.learned_offset_f += adjustment * 0.3

    log.info(
        "[%s] Learned from override: phase=%s delta_raw=%+d → body_temp_shift=%+.1f°F "
        "(learning_rate=%.1f) | New offsets: global=%+.1f, %s=%+.1f",
        state.zone, phase, delta_raw, adjustment,
        OVERRIDE_LEARNING_RATE,
        trajectory.learned_offset_f,
        phase,
        getattr(trajectory, f"learned_{phase}_offset_f"),
    )

    # Update current setting to what user set (trust the user)
    state.current_raw_setting = override["actual_raw"]
    state.last_setting_we_pushed = None  # Reset so we don't detect same override

    # Log override for training data
    state.override_history.append({
        "timestamp": override["timestamp"],
        "phase": phase,
        "delta_raw": delta_raw,
        "adjustment_f": adjustment,
        "minutes_in": minutes_in,
    })

    # Fire HA event for visibility
    ha_fire_event("sleep_controller_learned", {
        "zone": state.zone,
        "phase": phase,
        "delta_raw": delta_raw,
        "body_temp_shift": round(adjustment, 2),
        "new_trajectory_offset": round(trajectory.learned_offset_f, 2),
    })


# ── State Persistence ────────────────────────────────────────────────────

def save_state(states: dict, trajectories: dict) -> None:
    """Save controller state to disk for persistence across restarts."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    for zone in states:
        data[zone] = {
            "state": asdict(states[zone]),
            "trajectory": asdict(trajectories[zone]),
        }
    STATE_FILE.write_text(json.dumps(data, indent=2, default=str))
    log.debug("State saved to %s", STATE_FILE)


def load_state() -> tuple[dict, dict]:
    """Load controller state from disk."""
    states = {}
    trajectories = {}

    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            for zone, zdata in data.items():
                s = zdata.get("state", {})
                states[zone] = ControllerState(
                    zone=zone,
                    bedtime_ts=s.get("bedtime_ts"),
                    current_raw_setting=s.get("current_raw_setting", 3),
                    last_setting_we_pushed=s.get("last_setting_we_pushed"),
                    integral_error=s.get("integral_error", 0.0),
                    last_body_temp=s.get("last_body_temp"),
                    override_history=s.get("override_history", []),
                )
                t = zdata.get("trajectory", {})
                traj = make_warm_sleeper_trajectory()
                # Restore learned offsets
                traj.learned_offset_f = t.get("learned_offset_f", 0.0)
                traj.learned_onset_offset_f = t.get("learned_onset_offset_f", 0.0)
                traj.learned_deep_offset_f = t.get("learned_deep_offset_f", 0.0)
                traj.learned_rem_offset_f = t.get("learned_rem_offset_f", 0.0)
                traj.learned_prewake_offset_f = t.get("learned_prewake_offset_f", 0.0)
                trajectories[zone] = traj
            log.info("Loaded state from %s", STATE_FILE)
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("Failed to load state: %s — starting fresh", e)

    return states, trajectories


# ── Bedtime Detection ────────────────────────────────────────────────────

def detect_bedtime(zone: str) -> bool:
    """
    Detect if the topper schedule is currently running.

    We use run_progress > 0 as a signal that the schedule is active.
    This tells us when to start/stop the controller.
    """
    entity = ZONE_SENSORS[zone].get("run_progress")
    if not entity:
        return False
    progress = ha_get_state(entity)
    return progress is not None and progress > 0


# ── Main Control Loop ────────────────────────────────────────────────────

def run_controller(zones: list[str], dry_run: bool = True) -> None:
    """Main control loop."""
    if not HA_TOKEN:
        log.error("HA_TOKEN not set. Export it first.")
        sys.exit(1)

    log.info("=" * 60)
    log.info("Sleep Temperature Controller starting")
    log.info("  Mode: %s", "DRY RUN (no changes)" if dry_run else "LIVE")
    log.info("  Zones: %s", ", ".join(zones))
    log.info("  HA: %s", HA_URL)
    log.info("  Loop interval: %ds", LOOP_INTERVAL_SEC)
    log.info("=" * 60)

    # Load persistent state
    states, trajectories = load_state()

    # Initialize any missing zones
    for zone in zones:
        if zone not in states:
            states[zone] = ControllerState(zone=zone)
        if zone not in trajectories:
            trajectories[zone] = make_warm_sleeper_trajectory()

    # Graceful shutdown
    running = True
    def handle_signal(signum, frame):
        nonlocal running
        log.info("Shutdown signal received — saving state...")
        running = False
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    loop_count = 0
    while running:
        loop_count += 1
        now = datetime.now()

        for zone in zones:
            state = states[zone]
            trajectory = trajectories[zone]

            # Check if topper schedule is running
            is_sleeping = detect_bedtime(zone)

            if is_sleeping and state.bedtime_ts is None:
                # Just detected bedtime — initialize
                state.bedtime_ts = now.isoformat()
                state.integral_error = 0.0
                state.last_body_temp = None

                # Read current setting as starting point
                entity = ZONE_SETTING_ENTITY[zone]
                current = ha_get_state(entity)
                if current is not None:
                    state.current_raw_setting = int(current) + SETTING_OFFSET

                log.info("[%s] *** BEDTIME DETECTED *** Starting controller "
                         "(current setting: %+d)", zone,
                         state.current_raw_setting - SETTING_OFFSET)

                ha_fire_event("sleep_controller_started", {
                    "zone": zone,
                    "bedtime": state.bedtime_ts,
                    "starting_setting": state.current_raw_setting - SETTING_OFFSET,
                })

            elif not is_sleeping and state.bedtime_ts is not None:
                # Sleep ended — save learned state and reset
                bedtime = datetime.fromisoformat(state.bedtime_ts)
                duration = (now - bedtime).total_seconds() / 3600
                log.info("[%s] *** WAKE DETECTED *** Sleep duration: %.1f hours",
                         zone, duration)

                ha_fire_event("sleep_controller_stopped", {
                    "zone": zone,
                    "duration_hours": round(duration, 2),
                    "overrides": len(state.override_history),
                    "learned_offsets": {
                        "global": round(trajectory.learned_offset_f, 2),
                        "onset": round(trajectory.learned_onset_offset_f, 2),
                        "deep": round(trajectory.learned_deep_offset_f, 2),
                        "rem": round(trajectory.learned_rem_offset_f, 2),
                        "prewake": round(trajectory.learned_prewake_offset_f, 2),
                    },
                })

                state.bedtime_ts = None
                state.last_setting_we_pushed = None
                state.integral_error = 0.0
                state.override_history = []
                save_state(states, trajectories)
                continue

            if not is_sleeping:
                if loop_count % 12 == 0:  # Log every hour when idle
                    log.debug("[%s] Not sleeping — controller idle", zone)
                continue

            # ── Active sleep control ──

            # 1. Read sensors
            snap = read_sensors(zone)

            # 2. Check for manual override
            override = detect_override(state, snap)
            if override:
                learn_from_override(trajectory, state, override)
                save_state(states, trajectories)
                continue  # Skip this cycle, let user's adjustment take effect

            # 3. Compute next setting
            new_raw = compute_setting(state, snap, trajectory, dry_run)
            if new_raw is None:
                continue

            # 4. Apply setting (if changed and not dry run)
            if new_raw != state.current_raw_setting:
                display_val = new_raw - SETTING_OFFSET
                if dry_run:
                    log.info("[%s] DRY RUN: would set to %+d (raw %d)",
                             zone, display_val, new_raw)
                else:
                    entity = ZONE_SETTING_ENTITY[zone]
                    success = ha_set_number(entity, display_val)
                    if success:
                        state.last_setting_we_pushed = new_raw
                        state.current_raw_setting = new_raw
                        log.info("[%s] SET %s = %+d",
                                 zone, entity, display_val)

                        ha_fire_event("sleep_controller_adjusted", {
                            "zone": zone,
                            "setting": display_val,
                            "body_avg": snap.body_avg,
                            "target": round(trajectory.target_at(
                                (snap.timestamp - datetime.fromisoformat(state.bedtime_ts)).total_seconds() / 60), 1),
                            "minutes_in": round(
                                (snap.timestamp - datetime.fromisoformat(state.bedtime_ts)).total_seconds() / 60),
                        })
                    else:
                        log.error("[%s] Failed to set %s", zone, entity)

        # Save state periodically (every 10 loops = ~50 min)
        if loop_count % 10 == 0:
            save_state(states, trajectories)

        # Sleep until next cycle
        try:
            time.sleep(LOOP_INTERVAL_SEC)
        except KeyboardInterrupt:
            break

    # Final save
    save_state(states, trajectories)
    log.info("Controller stopped. State saved.")


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    global HA_URL, LOOP_INTERVAL_SEC

    parser = argparse.ArgumentParser(description="Sleep Temperature Controller")
    parser.add_argument("--live", action="store_true",
                        help="Live mode — actually push settings to topper")
    parser.add_argument("--zone", choices=["left", "right"],
                        help="Single zone (default: both)")
    parser.add_argument("--ha-url", default=None,
                        help=f"HA URL (default: {HA_URL})")
    parser.add_argument("--interval", type=int, default=LOOP_INTERVAL_SEC,
                        help=f"Loop interval in seconds (default: {LOOP_INTERVAL_SEC})")
    parser.add_argument("--show-trajectory", action="store_true",
                        help="Print the target trajectory and exit")
    args = parser.parse_args()

    if args.ha_url:
        HA_URL = args.ha_url

    if args.interval:
        LOOP_INTERVAL_SEC = args.interval

    zones = [args.zone] if args.zone else ["left", "right"]

    if args.show_trajectory:
        _, trajectories = load_state()
        for zone in zones:
            traj = trajectories.get(zone, make_warm_sleeper_trajectory())
            print(f"\n{'=' * 50}")
            print(f"Target Trajectory: {zone} side")
            print(f"{'=' * 50}")
            print(f"  Base targets:")
            print(f"    Onset:   {traj.onset_target_f:.1f}°F (0-{traj.onset_end_min}min)")
            print(f"    Deep:    {traj.deep_target_f:.1f}°F ({traj.onset_end_min}-{traj.deep_end_min}min)")
            print(f"    REM:     {traj.rem_target_f:.1f}°F ({traj.deep_end_min}-{traj.rem_heavy_start_min}min)")
            print(f"    PreWake: {traj.prewake_target_f:.1f}°F ({traj.prewake_start_min}+min)")
            print(f"  Learned offsets:")
            print(f"    Global:  {traj.learned_offset_f:+.2f}°F")
            print(f"    Onset:   {traj.learned_onset_offset_f:+.2f}°F")
            print(f"    Deep:    {traj.learned_deep_offset_f:+.2f}°F")
            print(f"    REM:     {traj.learned_rem_offset_f:+.2f}°F")
            print(f"    PreWake: {traj.learned_prewake_offset_f:+.2f}°F")
            print(f"\n  Minute-by-minute targets (every 30min):")
            for t in range(0, traj.total_sleep_min + 1, 30):
                target = traj.target_at(t)
                h = t // 60
                m = t % 60
                print(f"    +{h}:{m:02d}  →  {target:.1f}°F")
        return

    run_controller(zones, dry_run=not args.live)


if __name__ == "__main__":
    main()
