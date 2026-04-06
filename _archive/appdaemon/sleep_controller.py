"""
Stage-Reactive Sleep Temperature Controller — AppDaemon App

Reacts to ACTUAL sleep stages reported by Apple Watch via SleepSync.
Instead of guessing what phase you're in based on elapsed time, this
reads input_text.apple_health_sleep_stage and sets the topper target
accordingly.

Each sleep stage maps to a body-sensor target temperature. A PID loop
adjusts the topper setting to close the gap. Manual overrides shift
the target for that stage (learning persists across nights).

Sleep stages from Apple Health:
  deep  — N3 slow-wave sleep → aggressive cooling
  core  — N1+N2 light sleep  → moderate cooling
  rem   — REM sleep          → slightly warmer
  awake — brief awakening    → neutral/warm
  in_bed — lying awake       → moderate cooling (help initiate sleep)
"""

import json
from datetime import datetime
from pathlib import Path

import hassapi as hass

# ── Configuration ────────────────────────────────────────────────────────

LOOP_INTERVAL_SEC = 300   # 5 minutes between adjustments
SETTING_OFFSET = 10       # raw 0-20 → display -10 to +10
MAX_STEP_PER_LOOP = 2     # Max setting change per cycle (prevents jumps)
OVERRIDE_LEARNING_RATE = 0.5  # How fast stage targets adapt from overrides

STATE_DIR = Path("/addon_configs/a0d7b954_appdaemon/apps")
STATE_FILE = STATE_DIR / "controller_state.json"

# Body-sensor target temperatures per sleep stage (°F)
# These are what the body sensor SHOULD read, not topper settings.
# The PID controller figures out the setting to achieve each target.
STAGE_TARGETS = {
    "deep":    76.0,   # Cool — deep sleep benefits most from cooling
    "core":    78.0,   # Moderate — light sleep, moderate comfort
    "rem":     80.0,   # Warm — REM needs warmth, cold disrupts dreams
    "awake":   82.0,   # Warm — comfort during brief awakenings
    "in_bed":  77.0,   # Cool — help initiate sleep onset
    "unknown": 79.0,   # Fallback — middle ground
}

# How long a stage reading remains valid before we consider it stale
STAGE_STALE_MINUTES = 30

# ── Entity Mappings ──────────────────────────────────────────────────────

SLEEP_STAGE_ENTITY = "input_text.apple_health_sleep_stage"

ZONE_SENSORS = {
    "left": {
        "body_right":  "sensor.smart_topper_left_side_body_sensor_right",
        "body_center": "sensor.smart_topper_left_side_body_sensor_center",
        "body_left":   "sensor.smart_topper_left_side_body_sensor_left",
        "ambient":     "sensor.smart_topper_left_side_ambient_temperature",
        "run_progress": "sensor.smart_topper_left_side_run_progress",
    },
    "right": {
        "body_right":  "sensor.smart_topper_right_side_body_sensor_right",
        "body_center": "sensor.smart_topper_right_side_body_sensor_center",
        "body_left":   "sensor.smart_topper_right_side_body_sensor_left",
        "ambient":     "sensor.smart_topper_right_side_ambient_temperature",
        "run_progress": "sensor.smart_topper_right_side_run_progress",
    },
}

ZONE_SETTING_ENTITY = {
    "left": "number.smart_topper_left_side_bedtime_temperature",
    "right": "number.smart_topper_right_side_bedtime_temperature",
}

HEALTH_ENTITIES = {
    "hr_avg": "input_number.apple_health_hr_avg",
    "hrv":    "input_number.apple_health_hrv",
}


# ── Main AppDaemon App ──────────────────────────────────────────────────

class SleepController(hass.Hass):

    def initialize(self):
        self.log("=" * 50)
        self.log("Stage-Reactive Sleep Controller initializing")

        self.zones = ["left"]  # Add "right" when needed
        self.zone_state = {}
        self.learned_offsets = {}  # Per-stage learned temperature offsets

        self._load_state()

        for zone in self.zones:
            if zone not in self.zone_state:
                self.zone_state[zone] = self._fresh_zone_state()
            if zone not in self.learned_offsets:
                self.learned_offsets[zone] = {s: 0.0 for s in STAGE_TARGETS}

        # Control loop
        self.run_every(self._control_loop, "now", LOOP_INTERVAL_SEC)

        # Listen for manual overrides
        self.listen_event(self._on_override_event, "perfectly_snug_manual_override")

        offsets = self.learned_offsets.get("left", {})
        self.log(f"  Zones: {', '.join(self.zones)}")
        self.log(f"  Stage targets: {STAGE_TARGETS}")
        self.log(f"  Learned offsets (left): {offsets}")
        self.log("Controller ready — reacting to sleep stages")
        self.log("=" * 50)

    def _fresh_zone_state(self):
        return {
            "bedtime_ts": None,
            "current_raw_setting": 3,  # display -7
            "last_setting_we_pushed": None,
            "integral_error": 0.0,
            "last_body_temp": None,
            "last_stage": "unknown",
            "stage_changed_at": None,
            "override_history": [],
            # HR/HRV baseline tracking for stage estimation
            "hr_samples": [],     # rolling window of recent HR values
            "hrv_samples": [],    # rolling window of recent HRV values
            "hr_baseline": None,  # computed after first ~15 min of sleep
            "hrv_baseline": None,
        }

    # ── Control Loop ─────────────────────────────────────────────────

    def _control_loop(self, kwargs):
        now = datetime.now()

        for zone in self.zones:
            state = self.zone_state[zone]
            is_sleeping = self._is_sleeping(zone)

            # Bedtime detection
            if is_sleeping and state["bedtime_ts"] is None:
                state["bedtime_ts"] = now.isoformat()
                state["integral_error"] = 0.0
                state["last_body_temp"] = None
                state["last_stage"] = "unknown"
                state["stage_changed_at"] = None

                current_display = self._read_entity(ZONE_SETTING_ENTITY[zone])
                if current_display is not None:
                    state["current_raw_setting"] = int(current_display) + SETTING_OFFSET

                self.log(f"[{zone}] *** BEDTIME *** setting={state['current_raw_setting'] - SETTING_OFFSET:+d}")
                continue

            # Wake detection
            if not is_sleeping and state["bedtime_ts"] is not None:
                bedtime = datetime.fromisoformat(state["bedtime_ts"])
                duration = (now - bedtime).total_seconds() / 3600
                self.log(f"[{zone}] *** WAKE *** {duration:.1f}h, "
                         f"overrides: {len(state['override_history'])}")

                state["bedtime_ts"] = None
                state["last_setting_we_pushed"] = None
                state["integral_error"] = 0.0
                state["override_history"] = []
                self._save_state()
                continue

            if not is_sleeping:
                continue

            # ── Active sleep control ──

            # 1. Read body sensors
            sensors = self._read_sensors(zone)
            body_avg = sensors.get("body_avg")
            if body_avg is None:
                self.log(f"[{zone}] No body temp — skipping", level="WARNING")
                continue

            # 2. Read sleep stage (with HR/HRV fallback)
            hr = self._read_entity(HEALTH_ENTITIES["hr_avg"])
            hrv = self._read_entity(HEALTH_ENTITIES["hrv"])

            # Update HR/HRV baseline
            self._update_hr_baseline(state, hr, hrv)

            stage = self._read_sleep_stage(state, hr, hrv)

            # Track source for logging
            apple_stage = self.get_state(SLEEP_STAGE_ENTITY)
            stage_source = "apple" if apple_stage == stage and apple_stage in STAGE_TARGETS else "hr/hrv"

            # Log stage transitions
            if stage != state["last_stage"]:
                self.log(f"[{zone}] STAGE CHANGE: {state['last_stage']} → {stage}")
                state["last_stage"] = stage
                state["stage_changed_at"] = now.isoformat()
                # Reset integral on stage change to prevent windup carryover
                state["integral_error"] = 0.0

            # 3. Check for manual override
            override = self._detect_override(zone, state)
            if override:
                self._learn_from_override(zone, state, stage, override)
                self._save_state()
                continue

            # 4. Compute target from current stage
            base_target = STAGE_TARGETS.get(stage, STAGE_TARGETS["unknown"])
            learned_offset = self.learned_offsets.get(zone, {}).get(stage, 0.0)
            target_f = base_target + learned_offset

            # 5. PID control
            error = body_avg - target_f  # positive = too warm → need more cooling

            kp, ki, kd = 0.5, 0.02, 0.1
            p_term = kp * error

            state["integral_error"] += error
            state["integral_error"] = max(-5.0, min(5.0, state["integral_error"]))
            i_term = ki * state["integral_error"]

            d_term = 0.0
            if state["last_body_temp"] is not None:
                rate = body_avg - state["last_body_temp"]
                d_term = kd * rate
            state["last_body_temp"] = body_avg

            pid_output = p_term + i_term + d_term

            # PID output > 0 means body is too warm → decrease setting (more cooling)
            new_raw = state["current_raw_setting"] - pid_output
            new_raw = max(0, min(20, round(new_raw)))

            # Rate limit
            delta = new_raw - state["current_raw_setting"]
            if abs(delta) > MAX_STEP_PER_LOOP:
                new_raw = state["current_raw_setting"] + (MAX_STEP_PER_LOOP if delta > 0 else -MAX_STEP_PER_LOOP)

            display_current = state["current_raw_setting"] - SETTING_OFFSET
            display_new = new_raw - SETTING_OFFSET

            bedtime = datetime.fromisoformat(state["bedtime_ts"])
            minutes_in = (now - bedtime).total_seconds() / 60.0

            # Read HR/HRV for logging context (already read above)
            hr_str = f"HR={hr:.0f}" if hr else "HR=?"
            hrv_str = f"HRV={hrv:.0f}" if hrv else "HRV=?"

            self.log(f"[{zone}] t+{minutes_in:.0f}m stage={stage}({stage_source}) | "
                     f"body={body_avg:.1f}°F target={target_f:.1f}°F "
                     f"err={error:+.1f}°F | "
                     f"PID={p_term:+.2f}/{i_term:+.2f}/{d_term:+.2f} | "
                     f"setting: {display_current:+d}→{display_new:+d} | "
                     f"{hr_str} {hrv_str}")

            # 6. Apply if changed
            if new_raw != state["current_raw_setting"]:
                entity = ZONE_SETTING_ENTITY[zone]
                self.call_service("number/set_value",
                                entity_id=entity, value=display_new)
                state["last_setting_we_pushed"] = new_raw
                state["current_raw_setting"] = new_raw
                self.log(f"[{zone}] SET {entity} = {display_new:+d}")

        # Periodic save
        if not hasattr(self, "_loop_count"):
            self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 10 == 0:
            self._save_state()

    # ── Sleep Stage Reading ──────────────────────────────────────────

    def _read_sleep_stage(self, zone_state=None, hr=None, hrv=None):
        """Read current sleep stage from HA entity, fall back to HR/HRV estimation."""
        state = self.get_state(SLEEP_STAGE_ENTITY)
        if state not in (None, "", "unavailable", "unknown"):
            # Check staleness via last_changed
            last_changed = self.get_state(SLEEP_STAGE_ENTITY, attribute="last_changed")
            if last_changed:
                try:
                    changed_dt = datetime.fromisoformat(last_changed.replace("Z", "+00:00"))
                    age_min = (datetime.now(changed_dt.tzinfo) - changed_dt).total_seconds() / 60
                    if age_min <= STAGE_STALE_MINUTES:
                        if state in STAGE_TARGETS:
                            return state
                    else:
                        self.log(f"Sleep stage '{state}' is {age_min:.0f}min old — falling back to HR/HRV",
                                 level="WARNING")
                except (ValueError, TypeError):
                    pass
            else:
                # No last_changed metadata, trust it
                if state in STAGE_TARGETS:
                    return state

        # ── HR/HRV fallback estimation ──
        return self._estimate_stage_from_hr(zone_state, hr, hrv)

    def _update_hr_baseline(self, state, hr, hrv):
        """Track rolling HR/HRV samples and compute baseline."""
        if hr is not None and hr > 30:  # sanity check
            state["hr_samples"].append(hr)
            if len(state["hr_samples"]) > 60:  # ~5h at 5min intervals
                state["hr_samples"] = state["hr_samples"][-60:]
        if hrv is not None and hrv > 0:
            state["hrv_samples"].append(hrv)
            if len(state["hrv_samples"]) > 60:
                state["hrv_samples"] = state["hrv_samples"][-60:]

        # Set baseline after ~15 min (3 samples)
        if state["hr_baseline"] is None and len(state["hr_samples"]) >= 3:
            state["hr_baseline"] = sum(state["hr_samples"]) / len(state["hr_samples"])
            self.log(f"HR baseline set: {state['hr_baseline']:.1f} bpm")
        if state["hrv_baseline"] is None and len(state["hrv_samples"]) >= 3:
            state["hrv_baseline"] = sum(state["hrv_samples"]) / len(state["hrv_samples"])
            self.log(f"HRV baseline set: {state['hrv_baseline']:.1f} ms")

    def _estimate_stage_from_hr(self, state, hr, hrv):
        """
        Estimate sleep stage from HR/HRV deviation from baseline.

        Physiology:
        - Deep sleep: HR drops 10-20% below baseline, HRV rises significantly
        - REM sleep:  HR variable/near baseline, HRV drops
        - Core/light: HR slightly below baseline, HRV near baseline
        - Awake:      HR at or above baseline, HRV low
        """
        if state is None or hr is None or state.get("hr_baseline") is None:
            return "unknown"

        baseline_hr = state["hr_baseline"]
        baseline_hrv = state.get("hrv_baseline")

        hr_pct = (hr - baseline_hr) / baseline_hr  # negative = slower than baseline

        hrv_pct = 0.0
        if baseline_hrv and baseline_hrv > 0 and hrv is not None:
            hrv_pct = (hrv - baseline_hrv) / baseline_hrv  # positive = higher than baseline

        # Deep sleep: HR well below baseline + HRV elevated
        if hr_pct < -0.10 and hrv_pct > 0.10:
            estimated = "deep"
        # Deep (HR-only): very low HR even without HRV data
        elif hr_pct < -0.15:
            estimated = "deep"
        # REM: HR near baseline but variable, HRV suppressed
        elif -0.05 < hr_pct < 0.05 and hrv_pct < -0.10:
            estimated = "rem"
        # Awake: HR above baseline
        elif hr_pct > 0.05:
            estimated = "awake"
        # Default: core/light sleep
        else:
            estimated = "core"

        self.log(f"Stage estimated from HR/HRV: {estimated} "
                 f"(HR={hr:.0f}/{baseline_hr:.0f} [{hr_pct:+.0%}], "
                 f"HRV={hrv if hrv else '?'}/{baseline_hrv if baseline_hrv else '?'} [{hrv_pct:+.0%}])")
        return estimated

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

    # ── Override Detection & Learning ────────────────────────────────

    def _detect_override(self, zone, state):
        if state["last_setting_we_pushed"] is None:
            return None

        current_display = self._read_entity(ZONE_SETTING_ENTITY[zone])
        if current_display is None:
            return None

        current_raw = int(current_display) + SETTING_OFFSET
        expected_raw = state["last_setting_we_pushed"]

        if current_raw != expected_raw:
            self.log(f"[{zone}] MANUAL OVERRIDE: expected "
                     f"{expected_raw - SETTING_OFFSET:+d}, found "
                     f"{current_raw - SETTING_OFFSET:+d}")
            return {
                "expected_raw": expected_raw,
                "actual_raw": current_raw,
                "delta_raw": current_raw - expected_raw,
                "timestamp": datetime.now().isoformat(),
            }
        return None

    def _on_override_event(self, event_name, data, kwargs):
        zone = data.get("zone")
        if zone not in self.zone_state:
            return
        if self.zone_state[zone]["bedtime_ts"] is None:
            return
        self.log(f"[{zone}] Override event: {data}")

    def _learn_from_override(self, zone, state, stage, override):
        """Shift the target for the CURRENT sleep stage based on manual adjustment."""
        delta_raw = override["delta_raw"]

        # ~0.7°F body temp shift per setting unit
        body_temp_shift = delta_raw * 0.7 * OVERRIDE_LEARNING_RATE

        offsets = self.learned_offsets.setdefault(zone, {s: 0.0 for s in STAGE_TARGETS})
        old_offset = offsets.get(stage, 0.0)
        offsets[stage] = old_offset + body_temp_shift

        self.log(f"[{zone}] LEARNED: stage={stage} delta={delta_raw:+d} → "
                 f"offset {old_offset:+.1f}→{offsets[stage]:+.1f}°F")

        state["current_raw_setting"] = override["actual_raw"]
        state["last_setting_we_pushed"] = None

        bedtime = datetime.fromisoformat(state["bedtime_ts"])
        minutes_in = (datetime.now() - bedtime).total_seconds() / 60.0
        state["override_history"].append({
            "timestamp": override["timestamp"],
            "stage": stage,
            "delta_raw": delta_raw,
            "minutes_in": round(minutes_in),
        })

    # ── State Persistence ────────────────────────────────────────────

    def _save_state(self):
        data = {}
        for zone in self.zones:
            data[zone] = {
                "state": {k: v for k, v in self.zone_state[zone].items()},
                "learned_offsets": self.learned_offsets.get(zone, {}),
            }
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(data, indent=2, default=str))
        except OSError as e:
            self.log(f"Failed to save state: {e}", level="WARNING")

    def _load_state(self):
        if not STATE_FILE.exists():
            self.log("No saved state — starting fresh")
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            for zone, zdata in data.items():
                s = zdata.get("state", {})
                self.zone_state[zone] = {
                    "bedtime_ts": None,
                    "current_raw_setting": s.get("current_raw_setting", 3),
                    "last_setting_we_pushed": None,
                    "integral_error": 0.0,
                    "last_body_temp": None,
                    "last_stage": "unknown",
                    "stage_changed_at": None,
                    "override_history": [],
                    "hr_samples": [],
                    "hrv_samples": [],
                    "hr_baseline": None,
                    "hrv_baseline": None,
                }
                self.learned_offsets[zone] = zdata.get(
                    "learned_offsets", {s: 0.0 for s in STAGE_TARGETS})
            self.log(f"Loaded state from {STATE_FILE}")
        except (json.JSONDecodeError, KeyError) as e:
            self.log(f"Failed to load state: {e} — starting fresh", level="WARNING")
