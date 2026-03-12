"""
Data-Driven Sleep Temperature Controller — AppDaemon App
=========================================================

Reactive controller that adjusts the PerfectlySnug topper setting (-10 to +10)
in real-time based on:
  - Current sleep stage (from Apple Watch via SleepSync)
  - Current body sensor temperature
  - Learned body temp targets per sleep stage (from historical data)
  - Learned transfer function (how setting changes affect body temp)
  - Manual adjustment history (your preference corrections)

The controller does NOT hardcode temperature targets. Instead, it learns
from correlation analysis and continuously adapts from manual overrides.

Control loop (every 5 min):
  1. Read body temp, sleep stage, ambient temp
  2. Look up target body temp for current stage
  3. Compute error (actual - target)
  4. Use transfer function to convert temp error → setting adjustment
  5. Apply to the active preset (bedtime/sleep/wake) based on phase
  6. Detect & learn from manual overrides
"""

import json
from datetime import datetime
from pathlib import Path

import hassapi as hass

# ── Configuration ────────────────────────────────────────────────────────

LOOP_INTERVAL_SEC = 180     # 3 min control loop (fast enough for stage reactivity)
MAX_STEP_PER_LOOP = 1       # Conservative: ±1 per cycle (Eight Sleep style)
OVERRIDE_LEARNING_RATE = 0.3  # How fast targets adapt from manual overrides
STAGE_STALE_MINUTES = 30    # Max age of sleep stage reading before fallback
STAGE_COOLDOWN_SEC = 180    # Don't adjust for 3 min after a stage transition
OUTLIER_THRESHOLD_F = 5.0   # Ignore body temp readings >5°F from rolling avg
DEADBAND_F = 1.5            # Don't adjust if error is within this range (°F)
OCCUPANCY_THRESHOLD_F = 78.0  # Body temp below this = nobody in bed
WAKE_RAMP_MINUTES = 25      # Start warming this many min before wake phase
WAKE_RAMP_SETTING = -3      # Target setting at wake (warmer than sleep)
CONTINUOUS_LEARN_RATE = 0.01  # Slow continuous adaptation per loop

# Kill switch: rapid button presses to disable controller for the night
# 3+ setting changes within 20 seconds = kill switch activated
KILL_SWITCH_CHANGES = 3
KILL_SWITCH_WINDOW_SEC = 20

# Ambient temperature compensation
# Reference ambient: what the room was during our training data
AMBIENT_REFERENCE_F = 74.0  # From data: avg ambient ~74°F
# How much to shift setting per °F of ambient deviation
# Warmer room → need colder setting. ~0.3 setting points per °F ambient.
AMBIENT_COMPENSATION = 0.3

# Guardrails for learned targets
TARGET_MIN_F = 78.0
TARGET_MAX_F = 88.0

# Time-of-night target adjustment (#12)
# Body temp naturally rises ~0.5°F per hour through the night.
# Allow targets to drift warmer as the night progresses.
TIME_DRIFT_F_PER_HOUR = 0.3

# Setting → body temp lag (#13)
# Changes take ~15 min to show in body temp. The PID compares
# current temp against target, but current temp reflects the OLD
# setting. Offset PID input by estimated lag.
SETTING_LAG_MINUTES = 15

# Multi-night trend penalty (#14)
# Penalize settings that deviate from the 7-night rolling average
TREND_PENALTY_WEIGHT = 0.2

# User's preferred baseline settings (-10 to +10)
# The controller applies bounded offsets around these,
# never wholesale overrides (Eight Sleep approach).
USER_BASELINE = {
    "bedtime": -8,
    "sleep":   -6,
    "wake":    -5,
}
# Max offset the controller can apply around baseline
MAX_OFFSET_FROM_BASELINE = 3

# Prior-night deficit compensation
# If deep < 15% or REM < 20%, increase cooling/warming
DEEP_DEFICIT_THRESHOLD = 0.15   # 15% of night
REM_DEFICIT_THRESHOLD = 0.20    # 20% of night
DEFICIT_EXTRA_OFFSET = 1        # extra setting points

# Sleep onset → phase transition delay
# Hold bedtime temp for 15 min after first real sleep stage
ONSET_HOLD_MINUTES = 15

# State dir: try container path first, fall back to host
_container = Path("/config/apps")
_host = Path("/addon_configs/a0d7b954_appdaemon/apps")
STATE_DIR = _container if _container.exists() else _host
STATE_FILE = STATE_DIR / "controller_state.json"
STAGE_MODEL_FILE = STATE_DIR / "stage_classifier.json"

# Minimum confidence to trust the ML classifier over the heuristic
STAGE_CLASSIFIER_MIN_CONFIDENCE = 0.45

# Body temp targets per sleep stage (°F) — learned from correlation analysis
# These represent what body temp should be during each stage.
# The controller adjusts the -10/+10 setting to achieve these.
# Values are from 5 nights of data (Mar 4-9, 2026).
STAGE_BODY_TARGETS = {
    "deep":    82.0,   # Coolest — from data: avg 82.5°F during deep
    "core":    83.0,   # Moderate — from data: avg 83.0°F during core
    "rem":     83.5,   # Warmest — from data: avg 83.4°F during REM
    "awake":   82.0,   # Comfort — from data: avg 81.9°F during awake
    "in_bed":  82.0,   # Pre-sleep — similar to deep for onset
    "unknown": 83.0,   # Fallback — use core as default
}

# Transfer function: how much body temp changes per setting point
# From data: setting -10 → 81.8°F, setting -6 → 83.6°F
# That's ~0.45°F per setting point. Conservative estimate.
DEGREES_PER_SETTING_POINT = 0.45

# PID gains (halved to reduce oscillation)
PID_KP = 0.5 / DEGREES_PER_SETTING_POINT
PID_KI = 0.02 / DEGREES_PER_SETTING_POINT
PID_KD = 0.1 / DEGREES_PER_SETTING_POINT

# ── Entity Mappings ──────────────────────────────────────────────────────

SLEEP_STAGE_ENTITY = "input_text.apple_health_sleep_stage"

ZONE_SENSORS = {
    "left": {
        "body_right":   "sensor.smart_topper_left_side_body_sensor_right",
        "body_center":  "sensor.smart_topper_left_side_body_sensor_center",
        "body_left":    "sensor.smart_topper_left_side_body_sensor_left",
        "ambient":      "sensor.smart_topper_left_side_ambient_temperature",
        "run_progress":  "sensor.smart_topper_left_side_run_progress",
    },
    "right": {
        "body_right":   "sensor.smart_topper_right_side_body_sensor_right",
        "body_center":  "sensor.smart_topper_right_side_body_sensor_center",
        "body_left":    "sensor.smart_topper_right_side_body_sensor_left",
        "ambient":      "sensor.smart_topper_right_side_ambient_temperature",
        "run_progress":  "sensor.smart_topper_right_side_run_progress",
    },
}

# Preset entities: the -10 to +10 controls for each phase
ZONE_PRESETS = {
    "left": {
        "bedtime": "number.smart_topper_left_side_bedtime_temperature",
        "sleep":   "number.smart_topper_left_side_sleep_temperature",
        "wake":    "number.smart_topper_left_side_wake_temperature",
    },
    "right": {
        "bedtime": "number.smart_topper_right_side_bedtime_temperature",
        "sleep":   "number.smart_topper_right_side_sleep_temperature",
        "wake":    "number.smart_topper_right_side_wake_temperature",
    },
}

# Schedule length entities (for phase boundary calculation)
ZONE_SCHEDULE = {
    "left": {
        "start_length": "number.smart_topper_left_side_start_length_minutes",
        "wake_length":  "number.smart_topper_left_side_wake_length_minutes",
    },
    "right": {
        "start_length": "number.smart_topper_right_side_start_length_minutes",
        "wake_length":  "number.smart_topper_right_side_wake_length_minutes",
    },
}

HEALTH_ENTITIES = {
    "hr_avg": "input_number.apple_health_hr_avg",
    "hrv":    "input_number.apple_health_hrv",
}

# Notification entity for alerts
NOTIFY_SERVICE = "notify/mobile_app_mike_mones_iphone_14"
# Alert if no body data for this many consecutive loops
NO_DATA_ALERT_LOOPS = 6  # 6 × 5 min = 30 min


# ── Main AppDaemon App ──────────────────────────────────────────────────

class SleepController(hass.Hass):

    def initialize(self):
        self.log("=" * 60)
        self.log("Data-Driven Sleep Controller v2 initializing")

        self.zones = ["left"]   # Add "right" when needed
        self.zone_state = {}
        self.learned_targets = {}
        self.transfer_rate = DEGREES_PER_SETTING_POINT
        self.nightly_history = {}
        # Prior-night sleep stage percentages
        self.prior_night_stages = {}  # zone -> {deep_pct, rem_pct}
        # ML sleep stage classifier (loaded from JSON, no sklearn needed)
        self.stage_classifier = None

        self._load_state()
        self._load_stage_classifier()

        for zone in self.zones:
            if zone not in self.zone_state:
                self.zone_state[zone] = self._fresh_zone_state()
            if zone not in self.learned_targets:
                self.learned_targets[zone] = dict(STAGE_BODY_TARGETS)

        # Control loop
        self.run_every(self._control_loop, "now", LOOP_INTERVAL_SEC)

        # Listen for real-time setting changes (for kill switch)
        for zone in self.zones:
            for phase, entity in ZONE_PRESETS[zone].items():
                self.listen_state(
                    self._on_setting_changed,
                    entity,
                    zone=zone, phase=phase)

        targets = self.learned_targets.get("left", {})
        self.log(f"  Zones: {', '.join(self.zones)}")
        self.log(f"  Body temp targets: {targets}")
        self.log(f"  Transfer rate: {self.transfer_rate:.2f} °F/setting-point")
        clf_status = (f"{self.stage_classifier['n_trees']} trees, "
                      f"classes={self.stage_classifier['classes']}"
                      if self.stage_classifier else "not loaded (using heuristic)")
        self.log(f"  Stage classifier: {clf_status}")
        self.log("Controller ready — reactive mode")
        self.log("=" * 60)

    def _fresh_zone_state(self):
        return {
            "bedtime_ts": None,
            "last_settings_pushed": {},
            "integral_error": 0.0,
            "last_body_temp": None,
            "body_temp_history": [],
            "last_stage": "unknown",
            "stage_changed_at": None,
            "override_history": [],
            "manual_mode": False,
            "recent_setting_changes": [],
            # HR/HRV baseline tracking
            "hr_samples": [],
            "hrv_samples": [],
            "hr_baseline": None,
            "hrv_baseline": None,
            # Transfer function tracking (#9)
            "setting_change_log": [],
            # Stage classifier training data (#11)
            "stage_training_data": [],
            # Nightly setting history for trend (#14)
            "nightly_setting_avg": None,
            # Alerting
            "no_data_count": 0,
            "alert_sent": False,
            # Sleep onset tracking
            "sleep_onset_ts": None,
            "onset_phase_done": False,
            # Anomaly tracking
            "recent_settings": [],  # last N settings
            "anomaly_count": 0,
        }

    # ── Phase Detection ──────────────────────────────────────────────

    def _get_active_phase(self, zone, state):
        """Determine which preset phase is currently active: bedtime, sleep, or wake."""
        if state["bedtime_ts"] is None:
            return None

        bedtime = datetime.fromisoformat(state["bedtime_ts"])
        now = datetime.now()
        elapsed_min = (now - bedtime).total_seconds() / 60.0

        start_len = self._read_entity(ZONE_SCHEDULE[zone]["start_length"]) or 60
        wake_len = self._read_entity(ZONE_SCHEDULE[zone]["wake_length"]) or 30

        # Estimate total run from progress (if near 100%, wake phase)
        progress = self._read_entity(ZONE_SENSORS[zone]["run_progress"])
        if progress is not None and progress >= 100 - (wake_len / start_len * 100):
            # Use progress to detect wake phase more accurately
            pass

        # Simple boundary model: bedtime → sleep → wake
        if elapsed_min < start_len:
            return "bedtime"

        # Estimate total run length from progress
        if progress is not None and progress > 0:
            total_min = elapsed_min / (progress / 100.0)
            remaining_min = total_min - elapsed_min
            if remaining_min <= wake_len:
                return "wake"

        return "sleep"

    # ── Control Loop ─────────────────────────────────────────────────

    def _control_loop(self, kwargs):
        now = datetime.now()

        for zone in self.zones:
            state = self.zone_state[zone]
            is_sleeping = self._is_sleeping(zone)

            # ── Bedtime detection ──
            if is_sleeping and state["bedtime_ts"] is None:
                state["bedtime_ts"] = now.isoformat()
                state["integral_error"] = 0.0
                state["last_body_temp"] = None
                state["last_stage"] = "unknown"
                state["stage_changed_at"] = None
                state["last_settings_pushed"] = {}
                state["override_history"] = []

                # Read current settings as starting point
                for phase, entity in ZONE_PRESETS[zone].items():
                    val = self._read_entity(entity)
                    if val is not None:
                        state["last_settings_pushed"][phase] = int(val)

                self.log(f"[{zone}] *** BEDTIME *** "
                         f"presets: {state['last_settings_pushed']}")
                state["manual_mode"] = False
                state["recent_setting_changes"] = []
                state["body_temp_history"] = []
                state["sleep_onset_ts"] = None
                state["onset_phase_done"] = False
                continue

            # ── Wake detection ──
            if not is_sleeping and state["bedtime_ts"] is not None:
                bedtime = datetime.fromisoformat(state["bedtime_ts"])
                duration = (now - bedtime).total_seconds() / 3600
                overrides = len(state["override_history"])

                # Nightly summary (#8)
                self._log_nightly_summary(zone, state, duration)

                state["bedtime_ts"] = None
                state["last_settings_pushed"] = {}
                state["integral_error"] = 0.0
                state["override_history"] = []
                self._save_state()
                continue

            if not is_sleeping:
                continue

            # ── Kill switch check: manual mode for the night ──
            if state["manual_mode"]:
                continue

            # ── Active sleep control ──

            # 1. Determine active phase
            phase = self._get_active_phase(zone, state)
            if phase is None:
                continue

            # 2. Read body sensors
            sensors = self._read_sensors(zone)
            body_avg = sensors.get("body_avg")
            ambient = sensors.get("ambient")

            # Occupancy check: if body temp is below
            # threshold, nobody is in bed yet
            if (body_avg is not None
                    and body_avg < OCCUPANCY_THRESHOLD_F):
                self.log(
                    f"[{zone}] Empty bed "
                    f"({body_avg:.1f}°F < "
                    f"{OCCUPANCY_THRESHOLD_F}°F) "
                    f"— skipping")
                continue

            if body_avg is None:
                state["no_data_count"] += 1
                if (state["no_data_count"] >= NO_DATA_ALERT_LOOPS
                        and not state["alert_sent"]):
                    self._notify(
                        "Sleep Controller: no body sensor data "
                        f"for {state['no_data_count'] * 5} min")
                    state["alert_sent"] = True
                self.log(f"[{zone}] No body temp — skipping",
                         level="WARNING")
                continue
            state["no_data_count"] = 0

            # Outlier protection: ignore readings that deviate too much
            # from recent history (e.g., getting up to use bathroom)
            history = state["body_temp_history"]
            if len(history) >= 3:
                rolling_avg = sum(history[-6:]) / len(history[-6:])
                if abs(body_avg - rolling_avg) > OUTLIER_THRESHOLD_F:
                    self.log(f"[{zone}] Outlier rejected: {body_avg:.1f}°F "
                             f"(rolling avg {rolling_avg:.1f}°F)")
                    continue
            history.append(body_avg)
            if len(history) > 12:  # Keep ~1 hour at 5-min intervals
                state["body_temp_history"] = history[-12:]

            # 3. Read sleep stage (with HR/HRV fallback)
            hr = self._read_entity(HEALTH_ENTITIES["hr_avg"])
            hrv = self._read_entity(HEALTH_ENTITIES["hrv"])
            self._update_hr_baseline(state, hr, hrv)
            stage = self._read_sleep_stage(state, hr, hrv)

            apple_stage = self.get_state(SLEEP_STAGE_ENTITY)
            stage_source = "apple" if apple_stage == stage and stage in STAGE_BODY_TARGETS else "hr/hrv"

            # Log stage transitions
            if stage != state["last_stage"]:
                self.log(f"[{zone}] STAGE: "
                         f"{state['last_stage']} -> "
                         f"{stage} ({stage_source})")
                state["last_stage"] = stage
                state["stage_changed_at"] = now.isoformat()
                state["integral_error"] = 0.0

                # Track sleep onset (first non-awake stage)
                if (stage in ("deep", "core", "rem")
                        and state["sleep_onset_ts"] is None):
                    state["sleep_onset_ts"] = now.isoformat()
                    self.log(f"[{zone}] Sleep onset detected")

            # Awake = freeze setting (don't chase temperature)
            # Log context for model tuning
            if stage == "awake":
                bedtime_dt = datetime.fromisoformat(
                    state["bedtime_ts"])
                mins_in = (now - bedtime_dt).total_seconds() / 60
                preset = ZONE_PRESETS[zone].get(phase)
                cur_set = (self._read_entity(preset)
                           if preset else None)
                self.log(
                    f"[{zone}] AWAKE t+{mins_in:.0f}m | "
                    f"body={body_avg:.1f}°F "
                    f"setting={cur_set} "
                    f"ambient={ambient}")
                state["last_body_temp"] = body_avg
                continue

            # Sleep onset hold: keep bedtime setting for 15 min
            # after actual sleep is detected, then allow control
            if (state["sleep_onset_ts"]
                    and not state["onset_phase_done"]):
                onset = datetime.fromisoformat(
                    state["sleep_onset_ts"])
                onset_elapsed = (now - onset).total_seconds() / 60
                if onset_elapsed < ONSET_HOLD_MINUTES:
                    self.log(
                        f"[{zone}] Onset hold: "
                        f"{onset_elapsed:.0f}/"
                        f"{ONSET_HOLD_MINUTES}m")
                    state["last_body_temp"] = body_avg
                    continue
                else:
                    state["onset_phase_done"] = True
                    self.log(f"[{zone}] Onset hold complete"
                             " — control active")

            # Stage transition cooldown: don't adjust for a few minutes
            # after a stage change to let the reading stabilize
            if state["stage_changed_at"]:
                changed_at = datetime.fromisoformat(state["stage_changed_at"])
                cooldown_remaining = STAGE_COOLDOWN_SEC - (now - changed_at).total_seconds()
                if 0 < cooldown_remaining:
                    self.log(f"[{zone}] Stage cooldown: {cooldown_remaining:.0f}s remaining")
                    continue

            # 4. Check for manual override BEFORE computing adjustment
            override = self._detect_override(zone, state, phase)
            if override:
                # Record change time for kill switch detection
                state["recent_setting_changes"].append(
                    datetime.now().timestamp())
                # Check if kill switch triggered
                if self._check_kill_switch(zone, state):
                    continue
                self._learn_from_override(
                    zone, state, stage, phase, override)
                self._save_state()
                continue

            # 5. Compute target body temp for current stage
            targets = self.learned_targets.get(
                zone, STAGE_BODY_TARGETS)
            target_temp = targets.get(
                stage, targets.get("unknown", 83.0))

            # 5b. Time-of-night adjustment
            bedtime = datetime.fromisoformat(
                state["bedtime_ts"])
            hours_in = (
                (now - bedtime).total_seconds() / 3600.0)
            time_adj = hours_in * TIME_DRIFT_F_PER_HOUR
            target_temp += time_adj

            # 5c. Lag compensation
            lag_samples = int(SETTING_LAG_MINUTES / 5)
            hist = state["body_temp_history"]
            if len(hist) > lag_samples:
                effective_body = hist[-lag_samples]
            else:
                effective_body = body_avg

            # 6. PID: compute offset from baseline
            error = effective_body - target_temp

            # Deadband: don't adjust if error is small
            if abs(error) < DEADBAND_F:
                self.log(
                    f"[{zone}] t+{hours_in * 60:.0f}m "
                    f"body={body_avg:.1f}°F "
                    f"target={target_temp:.1f}°F "
                    f"err={error:+.1f}°F "
                    f"DEADBAND — no change")
                state["last_body_temp"] = body_avg
                continue

            # Track persistent large errors for anomaly
            if abs(error) > 5.0:
                state["anomaly_count"] = (
                    state.get("anomaly_count", 0) + 1)
            else:
                state["anomaly_count"] = 0

            p_term = PID_KP * error
            state["integral_error"] += error
            state["integral_error"] = max(
                -10.0, min(10.0, state["integral_error"]))
            i_term = PID_KI * state["integral_error"]

            d_term = 0.0
            if state["last_body_temp"] is not None:
                rate = body_avg - state["last_body_temp"]
                d_term = PID_KD * rate
            state["last_body_temp"] = body_avg

            pid_offset = p_term + i_term + d_term

            # 6b. Ambient compensation
            ambient_adj = 0.0
            if ambient is not None:
                ambient_adj = (
                    (ambient - AMBIENT_REFERENCE_F)
                    * AMBIENT_COMPENSATION)
            pid_offset += ambient_adj

            # 6c. Prior-night deficit compensation
            deficit_adj = 0
            prior = self.prior_night_stages.get(zone, {})
            if prior.get("deep_pct", 1.0) < DEEP_DEFICIT_THRESHOLD:
                # Low deep sleep last night -> cool more
                deficit_adj -= DEFICIT_EXTRA_OFFSET
                self.log(f"[{zone}] Deep deficit "
                         f"({prior['deep_pct']:.0%}) "
                         f"-> extra cooling")
            if prior.get("rem_pct", 1.0) < REM_DEFICIT_THRESHOLD:
                # Low REM last night -> warm more
                deficit_adj += DEFICIT_EXTRA_OFFSET
                self.log(f"[{zone}] REM deficit "
                         f"({prior['rem_pct']:.0%}) "
                         f"-> extra warming")

            # 7. Compute new setting as bounded offset
            # from user's preferred baseline
            baseline = USER_BASELINE.get(phase, -6)
            # PID offset: negative = need colder
            raw_offset = round(-pid_offset + deficit_adj)
            # Clamp offset to bounded range
            clamped_offset = max(
                -MAX_OFFSET_FROM_BASELINE,
                min(MAX_OFFSET_FROM_BASELINE, raw_offset))
            new_setting = baseline + clamped_offset
            new_setting = max(-10, min(10, new_setting))

            # Hard ceiling: NEVER go into heating (>= 0)
            if new_setting > 0:
                self.log(
                    f"[{zone}] BUG: setting would be "
                    f"{new_setting:+d} (heating!) "
                    f"— clamping to 0")
                self._notify(
                    f"Controller bug: tried to set "
                    f"{new_setting:+d} (heating). "
                    f"Clamped to 0. Check logs.")
                new_setting = 0

            # Read current for comparison
            preset_entity = ZONE_PRESETS[zone][phase]
            current_setting = self._read_entity(preset_entity)
            if current_setting is None:
                self.log(
                    f"[{zone}] Can't read {phase} "
                    f"preset", level="WARNING")
                continue
            current_setting = int(current_setting)

            # 7a. Multi-night trend penalty
            night_hist = self.nightly_history.get(zone, [])
            if len(night_hist) >= 3:
                trend_avg = (
                    sum(night_hist[-7:])
                    / len(night_hist[-7:]))
                trend_pull = (
                    (trend_avg - new_setting)
                    * TREND_PENALTY_WEIGHT)
                new_setting = max(-10, min(10,
                    round(new_setting + trend_pull)))

            # 7b. Wake-up ramp
            minutes_in = hours_in * 60.0
            progress = self._read_entity(
                ZONE_SENSORS[zone]["run_progress"])
            wake_len = (self._read_entity(
                ZONE_SCHEDULE[zone]["wake_length"]) or 30)
            if progress is not None and progress >= 5:
                total_min = minutes_in / (progress / 100.0)
                min_until_wake = total_min - minutes_in - wake_len
                if 0 < min_until_wake <= WAKE_RAMP_MINUTES and phase == "sleep":
                    # Linearly ramp toward WAKE_RAMP_SETTING
                    ramp_pct = 1.0 - (min_until_wake / WAKE_RAMP_MINUTES)
                    ramp_target = current_setting + ramp_pct * (WAKE_RAMP_SETTING - current_setting)
                    # Blend: don't override PID entirely, nudge toward ramp
                    new_setting = round(new_setting * 0.5 + ramp_target * 0.5)
                    new_setting = max(-10, min(10, new_setting))
                    self.log(f"[{zone}] Wake ramp: {min_until_wake:.0f}m to wake, "
                             f"ramp {ramp_pct:.0%} → nudging to {new_setting:+d}")

            # Rate limit
            delta = new_setting - current_setting
            if abs(delta) > MAX_STEP_PER_LOOP:
                new_setting = current_setting + (
                    MAX_STEP_PER_LOOP if delta > 0
                    else -MAX_STEP_PER_LOOP)

            # Final hard clamp: cooling only, never heating
            new_setting = min(0, new_setting)

            # 7d. Anomaly detection & auto-remediation
            anomaly = self._check_anomaly(
                zone, state, new_setting,
                current_setting, body_avg, stage,
                ambient, hours_in)
            if anomaly:
                # Reset to baseline
                new_setting = USER_BASELINE.get(
                    phase, -6)
                self.log(
                    f"[{zone}] ANOMALY: {anomaly} "
                    f"-> reset to baseline "
                    f"{new_setting:+d}")

            hr_str = (f"HR={hr:.0f}" if hr
                      else "HR=?")
            hrv_str = f"HRV={hrv:.0f}" if hrv else "HRV=?"
            amb_str = f"amb={ambient:.1f}°F({ambient_adj:+.1f})" if ambient else "amb=?"

            self.log(f"[{zone}] t+{minutes_in:.0f}m {phase} stage={stage}({stage_source}) | "
                     f"body={body_avg:.1f}°F target={target_temp:.1f}°F "
                     f"err={error:+.1f}°F | "
                     f"PID={p_term:+.1f}/{i_term:+.1f}/{d_term:+.1f} {amb_str} | "
                     f"setting: {current_setting:+d}→{new_setting:+d} | "
                     f"{hr_str} {hrv_str}")

            # 8. Apply if changed — write to ALL presets so the
            # topper uses the right value regardless of its phase
            if new_setting != current_setting:
                for p_name, p_entity in ZONE_PRESETS[zone].items():
                    self.call_service(
                        "number/set_value",
                        entity_id=p_entity,
                        value=new_setting)
                    state["last_settings_pushed"][p_name] = new_setting
                self.log(f"[{zone}] SET all presets = {new_setting:+d}")

                # 8b. Log for transfer function learning (#9)
                state["setting_change_log"].append({
                    "ts": now.isoformat(),
                    "old": current_setting,
                    "new": new_setting,
                    "body_before": body_avg,
                })
                if len(state["setting_change_log"]) > 200:
                    state["setting_change_log"] = (
                        state["setting_change_log"][-200:])

            # 9. Stage classifier training data (#11)
            # When Apple Watch provides a real stage, log
            # HR/HRV so we can train a personalized classifier
            if (stage_source == "apple"
                    and hr and hrv
                    and state.get("hr_baseline")):
                hr_bl = state["hr_baseline"]
                hrv_bl = state.get("hrv_baseline") or 1
                state["stage_training_data"].append({
                    "stage": stage,
                    "hr": hr, "hrv": hrv,
                    "hr_pct": (hr - hr_bl) / hr_bl,
                    "hrv_pct": (hrv - hrv_bl) / hrv_bl,
                    "hours_in": hours_in,
                })
                if len(state["stage_training_data"]) > 200:
                    state["stage_training_data"] = (
                        state["stage_training_data"][-200:])

            # 10. Continuous learning
            if abs(error) < 1.0 and abs(pid_offset) < 1.0:
                # System is near equilibrium — nudge target
                drift = (body_avg - target_temp) * CONTINUOUS_LEARN_RATE
                old_t = targets.get(stage, target_temp)
                new_t = max(TARGET_MIN_F,
                            min(TARGET_MAX_F, old_t + drift))
                targets[stage] = new_t

        # Periodic save
        if not hasattr(self, "_loop_count"):
            self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 10 == 0:
            self._save_state()

    # ── Nightly Summary & Adaptation ─────────────────────────────────

    def _log_nightly_summary(self, zone, state, duration):
        """Log a human-readable summary at wake (#8)
        and update adaptive models (#9, #14)."""
        overrides = len(state["override_history"])
        temps = state["body_temp_history"]
        changes = state["setting_change_log"]
        training = state["stage_training_data"]

        # Basic stats
        avg_t = (sum(temps) / len(temps)) if temps else 0
        min_t = min(temps) if temps else 0
        max_t = max(temps) if temps else 0

        # Compute nightly avg setting from change log
        settings_used = [c["new"] for c in changes]
        avg_setting = (
            sum(settings_used) / len(settings_used)
            if settings_used else 0)

        # Stage distribution
        stages_seen = {}
        for td in training:
            s = td["stage"]
            stages_seen[s] = stages_seen.get(s, 0) + 1

        # Compute stage percentages for deficit compensation
        total_stage_min = sum(stages_seen.values())
        if total_stage_min > 0:
            deep_pct = stages_seen.get("deep", 0) / total_stage_min
            rem_pct = stages_seen.get("rem", 0) / total_stage_min
            self.prior_night_stages[zone] = {
                "deep_pct": deep_pct,
                "rem_pct": rem_pct,
            }
            self.log(
                f"[{zone}] Stage %: deep={deep_pct:.0%} "
                f"rem={rem_pct:.0%} "
                f"(thresholds: deep<{DEEP_DEFICIT_THRESHOLD:.0%} "
                f"rem<{REM_DEFICIT_THRESHOLD:.0%})")

        self.log(
            f"[{zone}] *** WAKE SUMMARY *** "
            f"{duration:.1f}h | "
            f"body {avg_t:.1f}°F "
            f"({min_t:.1f}-{max_t:.1f}) | "
            f"avg setting {avg_setting:+.1f} | "
            f"{overrides} overrides | "
            f"stages: {stages_seen} | "
            f"{len(training)} training samples")

        targets = self.learned_targets.get(zone, {})
        self.log(
            f"[{zone}] Learned targets: {targets}")
        self.log(
            f"[{zone}] Transfer rate: "
            f"{self.transfer_rate:.3f} °F/pt")

        # Update nightly history for trend (#14)
        hist = self.nightly_history.setdefault(zone, [])
        if avg_setting != 0:
            hist.append(avg_setting)
        if len(hist) > 30:
            self.nightly_history[zone] = hist[-30:]

        # Adaptive transfer function (#9)
        # Look at setting changes and the body temp ~15min later
        self._update_transfer_function(zone, state)

    def _update_transfer_function(self, zone, state):
        """Refine transfer rate from observed setting→temp
        changes (#9)."""
        changes = state.get("setting_change_log", [])
        temps = state.get("body_temp_history", [])
        if len(changes) < 3 or len(temps) < 6:
            return

        # Simple approach: correlate setting deltas with
        # body temp deltas (lag-adjusted)
        deltas = []
        for c in changes:
            s_delta = c["new"] - c["old"]
            if s_delta == 0:
                continue
            # Find body temp ~15 min after the change
            # (rough: 3 samples later in history)
            idx = None
            for i, t in enumerate(temps):
                if i >= 3 and abs(t - c["body_before"]) < 10:
                    idx = i
                    break
            if idx and idx + 3 < len(temps):
                t_delta = temps[idx + 3] - temps[idx]
                rate = t_delta / s_delta
                if 0.1 < abs(rate) < 2.0:  # sanity
                    deltas.append(rate)

        if len(deltas) >= 3:
            new_rate = sum(deltas) / len(deltas)
            # Blend with existing rate (slow adaptation)
            old_rate = self.transfer_rate
            self.transfer_rate = old_rate * 0.8 + new_rate * 0.2
            self.transfer_rate = max(0.1, min(1.5,
                                              self.transfer_rate))
            self.log(
                f"[{zone}] Transfer rate updated: "
                f"{old_rate:.3f} → {self.transfer_rate:.3f} "
                f"°F/pt (from {len(deltas)} samples)")

    # ── Override Detection & Learning ────────────────────────────────

    def _detect_override(self, zone, state, current_phase):
        """Check if user manually changed the active preset since our last write."""
        last_pushed = state["last_settings_pushed"].get(current_phase)
        if last_pushed is None:
            return None

        preset_entity = ZONE_PRESETS[zone][current_phase]
        actual = self._read_entity(preset_entity)
        if actual is None:
            return None
        actual = int(actual)

        if actual != last_pushed:
            # Full context log for model tuning
            sensors = self._read_sensors(zone)
            body = sensors.get("body_avg")
            amb = sensors.get("ambient")
            bedtime_dt = datetime.fromisoformat(
                state["bedtime_ts"])
            mins = ((datetime.now() - bedtime_dt)
                    .total_seconds() / 60)
            self.log(
                f"[{zone}] MANUAL OVERRIDE "
                f"t+{mins:.0f}m | "
                f"{current_phase}: "
                f"{last_pushed:+d}->{actual:+d} "
                f"(delta={actual - last_pushed:+d}) | "
                f"body={body:.1f if body else '?'}°F "
                f"ambient={amb} "
                f"stage={state.get('last_stage','?')}")
            return {
                "phase": current_phase,
                "expected": last_pushed,
                "actual": actual,
                "delta": actual - last_pushed,
                "timestamp": datetime.now().isoformat(),
                "body_temp": body,
                "ambient": amb,
            }
        return None

    def _learn_from_override(self, zone, state, stage, phase, override):
        """Shift the body temp target for this stage based on manual override.

        If user made it colder (delta < 0): they want lower body temp,
        so reduce the target for this stage.
        If user made it warmer (delta > 0): they want higher body temp,
        so increase the target for this stage.
        """
        delta = override["delta"]
        # Convert setting delta → body temp shift using transfer function
        temp_shift = delta * self.transfer_rate * OVERRIDE_LEARNING_RATE

        targets = self.learned_targets.setdefault(zone, dict(STAGE_BODY_TARGETS))
        old_target = targets.get(stage, STAGE_BODY_TARGETS.get(stage, 83.0))

        # Don't learn from overrides when stage is unknown
        if stage == "unknown":
            self.log(f"[{zone}] Override during unknown stage — "
                     f"accepting but not learning")
            state["last_settings_pushed"][phase] = override["actual"]
            return

        targets[stage] = max(TARGET_MIN_F,
                             min(TARGET_MAX_F,
                                 old_target + temp_shift))

        direction = "colder" if delta < 0 else "warmer"
        self.log(f"[{zone}] LEARNED from {direction} override: "
                 f"stage={stage} target {old_target:.1f}→{targets[stage]:.1f}°F "
                 f"(setting delta={delta:+d})")

        # Update tracking
        state["last_settings_pushed"][phase] = override["actual"]

        bedtime = datetime.fromisoformat(state["bedtime_ts"])
        minutes_in = (datetime.now() - bedtime).total_seconds() / 60.0
        state["override_history"].append({
            "timestamp": override["timestamp"],
            "stage": stage,
            "phase": phase,
            "delta": delta,
            "direction": direction,
            "minutes_in": round(minutes_in),
            "body_temp": override.get("body_temp"),
            "ambient": override.get("ambient"),
        })

    def _on_setting_changed(self, entity, attribute,
                             old, new, kwargs):
        """Real-time listener for preset changes (kill switch)."""
        zone = kwargs.get("zone")
        if zone not in self.zone_state:
            return
        state = self.zone_state[zone]
        if state["bedtime_ts"] is None:
            return  # Not sleeping
        if state["manual_mode"]:
            return  # Already in manual mode

        # Was this change made by us?
        phase = kwargs.get("phase")
        pushed = state["last_settings_pushed"].get(phase)
        try:
            new_val = int(float(new))
        except (ValueError, TypeError):
            return
        if pushed is not None and new_val == pushed:
            return  # Our own write, ignore

        # External change — record for kill switch
        # Only count changes on the currently active phase
        active_phase = self._get_active_phase(
            zone, state)
        if phase != active_phase:
            return  # Topper's own phase transition, not user

        state["recent_setting_changes"].append(
            datetime.now().timestamp())
        self._check_kill_switch(zone, state)

    def _check_kill_switch(self, zone, state):
        """Detect rapid button presses as a kill switch signal.

        If 3+ setting changes happen within 20 seconds, the user is
        signaling they want manual control for the rest of the night.
        """
        now_ts = datetime.now().timestamp()
        cutoff = now_ts - KILL_SWITCH_WINDOW_SEC
        recent = [t for t in state["recent_setting_changes"]
                  if t > cutoff]
        state["recent_setting_changes"] = recent

        if len(recent) >= KILL_SWITCH_CHANGES:
            state["manual_mode"] = True
            state["recent_setting_changes"] = []
            self.log(
                f"[{zone}] *** KILL SWITCH *** "
                f"{len(recent)} changes in "
                f"{KILL_SWITCH_WINDOW_SEC}s — "
                f"controller disabled for tonight")
            self._notify("Sleep Controller: kill switch "
                         "activated — manual mode for tonight")
            return True
        return False

    # ── Sleep Stage Reading ──────────────────────────────────────────

    def _read_sleep_stage(self, zone_state=None, hr=None, hrv=None):
        """Read sleep stage from HA, fallback to HR/HRV estimation."""
        state = self.get_state(SLEEP_STAGE_ENTITY)
        if state not in (None, "", "unavailable", "unknown"):
            last_changed = self.get_state(SLEEP_STAGE_ENTITY, attribute="last_changed")
            if last_changed:
                try:
                    changed_dt = datetime.fromisoformat(
                        last_changed.replace("Z", "+00:00"))
                    age_min = (datetime.now(changed_dt.tzinfo) - changed_dt).total_seconds() / 60
                    if age_min <= STAGE_STALE_MINUTES and state in STAGE_BODY_TARGETS:
                        return state
                except (ValueError, TypeError):
                    pass
            elif state in STAGE_BODY_TARGETS:
                return state

        return self._estimate_stage_from_hr(zone_state, hr, hrv)

    def _update_hr_baseline(self, state, hr, hrv):
        """Track rolling HR/HRV and compute baseline."""
        if hr is not None and hr > 30:
            state["hr_samples"].append(hr)
            if len(state["hr_samples"]) > 60:
                state["hr_samples"] = state["hr_samples"][-60:]
        if hrv is not None and hrv > 0:
            state["hrv_samples"].append(hrv)
            if len(state["hrv_samples"]) > 60:
                state["hrv_samples"] = state["hrv_samples"][-60:]

        if state["hr_baseline"] is None and len(state["hr_samples"]) >= 3:
            state["hr_baseline"] = sum(state["hr_samples"]) / len(state["hr_samples"])
            self.log(f"HR baseline: {state['hr_baseline']:.1f} bpm")
        if state["hrv_baseline"] is None and len(state["hrv_samples"]) >= 3:
            state["hrv_baseline"] = sum(state["hrv_samples"]) / len(state["hrv_samples"])
            self.log(f"HRV baseline: {state['hrv_baseline']:.1f} ms")

    def _estimate_stage_from_hr(self, state, hr, hrv):
        """Estimate sleep stage from HR/HRV deviation.

        Uses the trained ML classifier if available, falling
        back to hardcoded thresholds if not.
        """
        if state is None or hr is None or state.get("hr_baseline") is None:
            return "unknown"

        baseline_hr = state["hr_baseline"]
        baseline_hrv = state.get("hrv_baseline")

        hr_pct = (hr - baseline_hr) / baseline_hr
        hrv_pct = 0.0
        if baseline_hrv and baseline_hrv > 0 and hrv is not None:
            hrv_pct = (hrv - baseline_hrv) / baseline_hrv

        # Calculate hours_in for ML features
        hours_in = 0.0
        for zone in self.zones:
            zs = self.zone_state.get(zone, {})
            bt = zs.get("bedtime_ts")
            if bt:
                try:
                    hours_in = (datetime.now() - datetime.fromisoformat(bt)).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass
                break

        # Try ML classifier first
        if self.stage_classifier is not None:
            features = {
                "hr_pct": hr_pct,
                "hrv_pct": hrv_pct,
                "hours_in": hours_in,
            }
            ml_stage, confidence = self._predict_stage_ml(features)
            if ml_stage and confidence >= STAGE_CLASSIFIER_MIN_CONFIDENCE:
                self.log(f"Stage ML: {ml_stage} ({confidence:.0%})",
                         level="DEBUG")
                return ml_stage
            # Low confidence — fall through to heuristic
            self.log(f"Stage ML low-conf: {ml_stage} ({confidence:.0%})"
                     f" — using heuristic", level="DEBUG")

        # Heuristic fallback
        if hr_pct < -0.10 and hrv_pct > 0.10:
            return "deep"
        elif hr_pct < -0.15:
            return "deep"
        elif -0.05 < hr_pct < 0.05 and hrv_pct < -0.10:
            return "rem"
        elif hr_pct > 0.05:
            return "awake"
        else:
            return "core"

    # ── ML Stage Classifier ──────────────────────────────────────────

    def _load_stage_classifier(self):
        """Load the JSON sleep stage classifier (trained offline)."""
        if not STAGE_MODEL_FILE.exists():
            self.log(f"No stage classifier at {STAGE_MODEL_FILE} "
                     f"— using HR/HRV heuristic")
            return
        try:
            model = json.loads(STAGE_MODEL_FILE.read_text())
            if (model.get("type") == "random_forest"
                    and model.get("trees")
                    and model.get("features")
                    and model.get("classes")):
                self.stage_classifier = model
                self.log(f"Loaded stage classifier: "
                         f"{model['n_trees']} trees, "
                         f"classes={model['classes']}")
            else:
                self.log(f"Invalid stage classifier format",
                         level="WARNING")
        except (json.JSONDecodeError, OSError) as e:
            self.log(f"Failed to load stage classifier: {e}",
                     level="WARNING")

    def _predict_stage_ml(self, features: dict) -> tuple:
        """Predict sleep stage from feature dict using JSON model.

        Returns (stage, confidence). No sklearn needed — pure
        Python tree evaluation.
        """
        model = self.stage_classifier
        if model is None:
            return None, 0.0

        classes = model["classes"]
        totals = {c: 0.0 for c in classes}

        for tree in model["trees"]:
            probs = self._walk_tree(tree, features)
            for cls, prob in probs.items():
                totals[cls] += prob

        # Normalize by number of trees
        n_trees = model["n_trees"]
        for cls in totals:
            totals[cls] /= n_trees

        best_cls = max(totals, key=totals.get)
        confidence = totals[best_cls]
        return best_cls, confidence

    def _walk_tree(self, node: dict, features: dict) -> dict:
        """Walk a JSON decision tree using named features."""
        if node.get("leaf"):
            return node["probs"]
        val = features.get(node["feature"], 0.0)
        if val <= node["threshold"]:
            return self._walk_tree(node["left"], features)
        else:
            return self._walk_tree(node["right"], features)

    # ── Sensor Reading ───────────────────────────────────────────────

    def _read_entity(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(state)
        except (ValueError, TypeError):
            return None

    def _read_sensors(self, zone):
        sensors = {}
        for key, entity_id in ZONE_SENSORS[zone].items():
            sensors[key] = self._read_entity(entity_id)
        body_vals = [v for k in ("body_right", "body_center", "body_left")
                     if (v := sensors.get(k)) is not None]
        if body_vals:
            sensors["body_avg"] = sum(body_vals) / len(body_vals)
        return sensors

    def _is_sleeping(self, zone):
        progress = self._read_entity(ZONE_SENSORS[zone]["run_progress"])
        return progress is not None and progress > 0

    def _notify(self, message):
        """Send a push notification to Mike's iPhone."""
        try:
            self.call_service(
                NOTIFY_SERVICE,
                message=message,
                title="SleepSync")
        except Exception as e:
            self.log(f"Notify failed: {e}", level="WARNING")

    # ── Anomaly Detection ────────────────────────────────────────────

    def _check_anomaly(self, zone, state, new_setting,
                       current_setting, body_temp, stage,
                       ambient, hours_in):
        """Detect anomalous controller behavior.

        Returns anomaly description string, or None.
        On anomaly: notifies, creates GitHub issue, resets.
        """
        # Track recent settings for oscillation detection
        state["recent_settings"].append(new_setting)
        if len(state["recent_settings"]) > 12:
            state["recent_settings"] = (
                state["recent_settings"][-12:])

        recent = state["recent_settings"]
        anomaly = None

        # 1. Extreme: hitting min/max repeatedly
        if (len(recent) >= 3
                and all(s in (-10, 10) for s in recent[-3:])):
            anomaly = (
                f"stuck at extreme ({recent[-1]:+d}) "
                f"for 3+ cycles")

        # 2. Oscillation: alternating direction 4+ times
        if len(recent) >= 6:
            dirs = []
            for i in range(1, len(recent)):
                d = recent[i] - recent[i - 1]
                if d > 0:
                    dirs.append(1)
                elif d < 0:
                    dirs.append(-1)
            # Count direction changes
            changes = sum(
                1 for i in range(1, len(dirs))
                if dirs[i] != dirs[i - 1])
            if changes >= 4:
                anomaly = (
                    f"oscillating ({changes} direction "
                    f"changes in {len(recent)} cycles)")

        # 3. Large error persisting: body temp > 5°F from
        # target for 6+ cycles (30 min)
        if state.get("anomaly_count", 0) >= 6:
            anomaly = (
                f"large error persisting for "
                f"{state['anomaly_count'] * 5} min")

        if anomaly:
            # Build context for the issue
            context = {
                "anomaly": anomaly,
                "zone": zone,
                "setting": new_setting,
                "body_temp": body_temp,
                "stage": stage,
                "ambient": ambient,
                "hours_in": round(hours_in, 1),
                "recent_settings": recent[-6:],
                "targets": self.learned_targets.get(
                    zone, {}),
                "transfer_rate": self.transfer_rate,
                "timestamp": datetime.now().isoformat(),
            }

            self._notify(
                f"Controller anomaly: {anomaly}. "
                f"Auto-reset to baseline.")
            self._create_github_issue(anomaly, context)

            # Reset state
            state["anomaly_count"] = 0
            state["recent_settings"] = []
            state["integral_error"] = 0.0

            return anomaly

        return None

    def _create_github_issue(self, anomaly, context):
        """Create a GitHub issue with anomaly report."""
        import urllib.request
        import urllib.error

        # Read GitHub token — try both container and
        # host paths since it differs
        gh_token = None
        for tp in [
            STATE_DIR / ".github_token",
            Path("/config/apps/.github_token"),
        ]:
            if tp.exists():
                try:
                    gh_token = tp.read_text().strip()
                    break
                except Exception:
                    pass

        if not gh_token:
            self.log("No GitHub token — skipping issue",
                     level="WARNING")
            return

        body = (
            f"## Anomaly Detected\n\n"
            f"**Type:** {anomaly}\n"
            f"**Time:** {context['timestamp']}\n\n"
            f"### Context\n"
            f"- Zone: {context['zone']}\n"
            f"- Setting: {context['setting']:+d}\n"
            f"- Body temp: {context['body_temp']:.1f}°F\n"
            f"- Stage: {context['stage']}\n"
            f"- Ambient: {context['ambient']}\n"
            f"- Hours in: {context['hours_in']}\n"
            f"- Recent settings: {context['recent_settings']}\n"
            f"- Transfer rate: "
            f"{context['transfer_rate']:.3f}\n\n"
            f"### Learned Targets\n"
            f"```json\n"
            f"{json.dumps(context['targets'], indent=2)}\n"
            f"```\n\n"
            f"### Action Taken\n"
            f"Auto-reset to baseline. Controller continues "
            f"operating with default settings.\n"
        )

        payload = json.dumps({
            "title": f"[Anomaly] {anomaly}",
            "body": body,
            "labels": ["anomaly", "auto-generated"],
        }).encode()

        url = ("https://api.github.com/repos/"
               "mike-mones/PerfectlySnug/issues")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"token {gh_token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST")

        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                self.log(
                    f"GitHub issue created: "
                    f"#{result.get('number', '?')}")
        except urllib.error.URLError as e:
            self.log(
                f"GitHub issue failed: {e}",
                level="WARNING")

    # ── State Persistence ────────────────────────────────────────────

    def _save_state(self):
        data = {}
        for zone in self.zones:
            data[zone] = {
                "state": {
                    k: v for k, v in
                    self.zone_state[zone].items()
                },
                "learned_targets": (
                    self.learned_targets.get(zone, {})),
                "transfer_rate": self.transfer_rate,
                "nightly_history": (
                    self.nightly_history.get(zone, [])),
                "prior_night_stages": (
                    self.prior_night_stages.get(zone, {})),
            }
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps(data, indent=2, default=str))
        except OSError as e:
            self.log(f"Failed to save state: {e}",
                     level="WARNING")

    def _load_state(self):
        if not STATE_FILE.exists():
            self.log("No saved state — starting fresh")
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            for zone, zdata in data.items():
                self.zone_state[zone] = self._fresh_zone_state()
                # Restore learned targets
                saved = zdata.get("learned_targets", {})
                if saved:
                    merged = dict(STAGE_BODY_TARGETS)
                    merged.update(saved)
                    self.learned_targets[zone] = merged
                # Restore transfer rate
                tr = zdata.get("transfer_rate")
                if tr:
                    self.transfer_rate = float(tr)
                # Restore nightly history (#14)
                nh = zdata.get("nightly_history", [])
                if nh:
                    self.nightly_history[zone] = nh
                # Restore prior night stages
                pns = zdata.get("prior_night_stages", {})
                if pns:
                    self.prior_night_stages[zone] = pns
            self.log(f"Loaded state from {STATE_FILE}")
        except (json.JSONDecodeError, KeyError) as e:
            self.log(f"Failed to load state: {e}",
                     level="WARNING")
