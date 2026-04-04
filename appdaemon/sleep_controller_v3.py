"""
Preference-Based Sleep Temperature Controller — AppDaemon App
==============================================================

Controls the PerfectlySnug topper setting (-10 to +10) based on:
  - Learned preference curve (baseline + multi-night override learning)
  - Ambient room temperature compensation
  - Body sensor extremes only (>85°F = too hot, used for safety cooling)

Body sensors at 80–85°F are AMBIGUOUS — they reflect body-mattress contact
temperature, not perceived comfort. The controller does NOT chase a body
temp target. Instead, it follows the user's learned preferences and only
intervenes when sensors show clear extremes.

Control loop (every 5 min):
  1. Detect if topper is running (occupancy)
  2. Determine phase from elapsed time
  3. Compute effective setting: learned baseline + ambient compensation
  4. If body sensor > 85°F: step 1 cooler (safety)
  5. Detect manual overrides → learn for future nights + set tonight's floor
  6. At wake: save override data, reset presets to learned baseline
"""

import json
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import hassapi as hass

# ── Configuration ────────────────────────────────────────────────────────

LOOP_INTERVAL_SEC = 300     # 5 min control loop
OCCUPANCY_THRESHOLD_F = 78.0  # Body temp below this = nobody in bed
OCCUPANCY_HOLD_MINUTES = 20  # Hold setting after first detecting body

# User's preferred baseline settings (-10 to +10)
USER_BASELINE = {
    "bedtime": -8,      # Aggressive cooling at sleep onset
    "sleep":   -6,      # Moderate cooling during bulk of night
    "wake":    -5,      # Ease off cooling toward morning
}

# Body sensor thresholds — only act on clear extremes
BODY_HOT_THRESHOLD_F = 85.0   # Above this = definitely too hot, cool down
BODY_COLD_THRESHOLD_F = 80.0  # Below this while occupied = definitely cold
# 80–85°F is the "ambiguous zone" — follow preferences, don't adjust

# Ambient temperature compensation
# Reference ambient: typical room temp where baselines feel right
AMBIENT_REFERENCE_F = 70.0
# Per-degree offset: colder room → warmer setting (+0.5 per degree below ref)
AMBIENT_COMPENSATION_PER_F = 0.5

# Kill switch: rapid manual changes disable controller for the night
KILL_SWITCH_CHANGES = 3
KILL_SWITCH_WINDOW_SEC = 20

# Multi-night learning: how many nights of override data to retain
LEARNING_HISTORY_NIGHTS = 7

# Notification entity
NOTIFY_SERVICE = "notify/mobile_app_mike_mones_iphone_14"

# InfluxDB logging
INFLUXDB_URL = "http://a0d7b954-influxdb:8086"
INFLUXDB_DB = "perfectly_snug"

# ── Entity Mappings ──────────────────────────────────────────────────────

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

# State persistence
_container = Path("/config/apps")
_host = Path("/addon_configs/a0d7b954_appdaemon/apps")
STATE_DIR = _container if _container.exists() else _host
STATE_FILE = STATE_DIR / "controller_state_v3.json"
LEARNING_FILE = STATE_DIR / "learned_preferences.json"


# ── Main AppDaemon App ──────────────────────────────────────────────────

class SleepController(hass.Hass):

    def initialize(self):
        self.log("=" * 60)
        self.log("Sleep Controller v3 (preference-based) initializing")

        self.zones = ["left"]
        self.zone_state = {}
        self.learned = {}  # Multi-night learned adjustments

        self._load_state()
        self._load_learned()

        for zone in self.zones:
            if zone not in self.zone_state:
                self.zone_state[zone] = self._fresh_zone_state()

        # Control loop
        self.run_every(self._control_loop, "now", LOOP_INTERVAL_SEC)

        # Listen for manual setting changes (kill switch)
        for zone in self.zones:
            for phase, entity in ZONE_PRESETS[zone].items():
                self.listen_state(
                    self._on_setting_changed,
                    entity,
                    zone=zone, phase=phase)

        self.log(f"  Zones: {', '.join(self.zones)}")
        self.log(f"  Baselines: {USER_BASELINE}")
        self.log(f"  Hot threshold: {BODY_HOT_THRESHOLD_F}°F")
        self.log(f"  Ambient ref: {AMBIENT_REFERENCE_F}°F")
        self.log(f"  Learned adjustments: {self.learned}")
        self.log("Controller ready")
        self.log("=" * 60)

    def _fresh_zone_state(self):
        return {
            "bedtime_ts": None,
            "last_settings_pushed": {},
            "manual_mode": False,
            "recent_setting_changes": [],
            "body_temp_history": [],
            "occupancy_detected_ts": None,
            "occupancy_hold_done": False,
            "override_history": [],
            "override_floor": {},
            "last_run_progress": 0,
            "last_restart_ts": None,
            "hot_streak": 0,  # Consecutive readings above BODY_HOT_THRESHOLD
            "bed_empty_since": None,
        }

    # ── Phase Detection ──────────────────────────────────────────────

    def _get_active_phase(self, zone, state):
        """Determine phase from elapsed time: bedtime → sleep → wake."""
        if state["bedtime_ts"] is None:
            return None

        bedtime = datetime.fromisoformat(state["bedtime_ts"])
        now = datetime.now()
        elapsed_min = (now - bedtime).total_seconds() / 60.0

        start_len = self._read_entity(ZONE_SCHEDULE[zone]["start_length"]) or 60
        wake_len = self._read_entity(ZONE_SCHEDULE[zone]["wake_length"]) or 30

        if elapsed_min < start_len:
            return "bedtime"

        # Use run_progress to estimate wake phase
        progress = self._read_entity(ZONE_SENSORS[zone]["run_progress"])
        if progress is not None and progress > 0:
            total_min = elapsed_min / (progress / 100.0)
            remaining_min = total_min - elapsed_min
            if remaining_min <= wake_len:
                return "wake"

        return "sleep"

    # ── Control Loop ─────────────────────────────────────────────────

    def _control_loop(self, kwargs):
        try:
            self._control_loop_inner(kwargs)
        except Exception as exc:
            self.log(f"CONTROL LOOP CRASH: {exc}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
            self._save_state()

    def _control_loop_inner(self, kwargs):
        now = datetime.now()

        for zone in self.zones:
            state = self.zone_state[zone]
            is_sleeping = self._is_sleeping(zone)
            if is_sleeping:
                state["last_run_progress"] = self._read_entity(ZONE_SENSORS[zone]["run_progress"]) or 0

            # ── Bedtime detection ──
            if is_sleeping and state["bedtime_ts"] is None:
                state["bedtime_ts"] = now.isoformat()
                state["last_settings_pushed"] = {}
                state["override_history"] = []
                state["override_floor"] = {}
                state["manual_mode"] = False
                state["recent_setting_changes"] = []
                state["body_temp_history"] = []
                state["occupancy_detected_ts"] = None
                state["occupancy_hold_done"] = False

                # Read current settings as starting point
                for phase, entity in ZONE_PRESETS[zone].items():
                    val = self._read_entity(entity)
                    if val is not None:
                        state["last_settings_pushed"][phase] = int(val)

                self.log(f"[{zone}] *** BEDTIME *** presets: {state['last_settings_pushed']}")
                continue

            # ── Wake detection ──
            if not is_sleeping and state["bedtime_ts"] is not None:
                bedtime = datetime.fromisoformat(state["bedtime_ts"])
                duration = (now - bedtime).total_seconds() / 3600

                # Topper's built-in schedule shut off — auto-restart if body in bed
                body_sensors = self._read_sensors(zone)
                body_in_bed = (body_sensors.get("body_avg") or 0) >= OCCUPANCY_THRESHOLD_F
                if body_in_bed:
                    self.log(f"[{zone}] Topper off but body in bed — auto-restarting! ({duration:.1f}h in)")
                    try:
                        self.call_service("switch/turn_on", entity_id=f"switch.smart_topper_{zone}_side_running")
                    except Exception as svc_err:
                        self.log(f"[{zone}] FAILED to auto-restart: {svc_err}", level="ERROR")
                    state["last_run_progress"] = 0
                    state["last_restart_ts"] = now.isoformat()
                    state["bed_empty_since"] = None
                    continue

                # Wait for restart to take effect
                last_restart = state.get("last_restart_ts")
                if last_restart and (now - datetime.fromisoformat(last_restart)).total_seconds() < 120:
                    self.log(f"[{zone}] Waiting for auto-restart to take effect.")
                    continue

                # Body not in bed and topper off — end session
                self._end_night(zone, state, now)
                continue

            if not is_sleeping:
                continue

            # ── Occupancy-based shutoff (topper still running but bed empty) ──
            body_sensors_check = self._read_sensors(zone)
            body_present = (body_sensors_check.get("body_avg") or 0) >= OCCUPANCY_THRESHOLD_F
            if not body_present and state.get("occupancy_hold_done"):
                # Body was previously detected, now gone
                if state.get("bed_empty_since") is None:
                    state["bed_empty_since"] = now.isoformat()
                    self.log(f"[{zone}] Bed empty — starting 20-min shutoff timer")
                else:
                    empty_min = (now - datetime.fromisoformat(state["bed_empty_since"])).total_seconds() / 60
                    if empty_min >= 20:
                        self.log(f"[{zone}] Bed empty for {empty_min:.0f}m — turning off topper")
                        try:
                            self.call_service("switch/turn_off", entity_id=f"switch.smart_topper_{zone}_side_running")
                        except Exception as svc_err:
                            self.log(f"[{zone}] FAILED to turn off: {svc_err}", level="ERROR")
                        self._end_night(zone, state, now)
                        continue
                    else:
                        self.log(f"[{zone}] Bed empty {empty_min:.0f}/20m — waiting")
                        continue
            elif body_present and state.get("bed_empty_since") is not None:
                # Body returned before 20-min timer expired
                self.log(f"[{zone}] Body returned — cancelling shutoff timer")
                state["bed_empty_since"] = None

            # ── Manual mode: stop adjusting for the night ──
            if state["manual_mode"]:
                continue

            # ── Active sleep control ──

            # 1. Determine phase
            phase = self._get_active_phase(zone, state)
            if phase is None:
                continue

            # 2. Read body sensors
            sensors = self._read_sensors(zone)
            body_avg = sensors.get("body_avg")
            ambient = sensors.get("ambient")

            # Occupancy check
            if body_avg is not None and body_avg < OCCUPANCY_THRESHOLD_F:
                self.log(f"[{zone}] Empty bed ({body_avg:.1f}°F < {OCCUPANCY_THRESHOLD_F}°F) — skipping")
                state["occupancy_detected_ts"] = None
                continue

            # Occupancy hold: hold setting for N minutes after first detecting body
            if (body_avg is not None
                    and body_avg >= OCCUPANCY_THRESHOLD_F
                    and not state.get("occupancy_hold_done", False)):
                if state.get("occupancy_detected_ts") is None:
                    state["occupancy_detected_ts"] = now.isoformat()
                    self.log(f"[{zone}] *** OCCUPANCY *** body={body_avg:.1f}°F — holding for {OCCUPANCY_HOLD_MINUTES}m")
                occ_ts = datetime.fromisoformat(state["occupancy_detected_ts"])
                hold_elapsed = (now - occ_ts).total_seconds() / 60
                if hold_elapsed < OCCUPANCY_HOLD_MINUTES:
                    self.log(f"[{zone}] Hold: {hold_elapsed:.0f}/{OCCUPANCY_HOLD_MINUTES}m")
                    continue
                else:
                    state["occupancy_hold_done"] = True
                    self.log(f"[{zone}] Hold complete — control active")

            if body_avg is None:
                self.log(f"[{zone}] No body temp — skipping", level="WARNING")
                continue

            # Track body temp history
            state["body_temp_history"].append(body_avg)
            if len(state["body_temp_history"]) > 24:  # ~2 hours at 5-min intervals
                state["body_temp_history"] = state["body_temp_history"][-24:]

            # 3. Check for manual override
            override = self._detect_override(zone, state, phase)
            if override:
                state["recent_setting_changes"].append(datetime.now().timestamp())
                if self._check_kill_switch(zone, state):
                    continue
                # Accept the override
                state["last_settings_pushed"][phase] = override["actual"]
                state["override_history"].append(override)
                # Set a floor — controller must not go below this tonight
                if override["delta"] > 0:
                    state["override_floor"][phase] = override["actual"]
                    self.log(
                        f"[{zone}] LEARNED: {phase} floor raised to "
                        f"{override['actual']:+d} (user went warmer)")
                    self._notify(
                        f"SleepSync: Learned — {phase} won't go below "
                        f"{override['actual']:+d} tonight")
                self._log_to_influx(zone, phase, sensors,
                                    override["actual"], "override")
                self._save_state()
                continue

            # 4. Compute effective setting from preferences + ambient
            baseline = USER_BASELINE.get(phase, -6)

            # Apply multi-night learned adjustment for this phase
            learned_adj = self.learned.get(zone, {}).get(phase, 0)
            effective = baseline + learned_adj

            # Ambient compensation: colder room → warmer setting
            if ambient is not None:
                ambient_delta = AMBIENT_REFERENCE_F - ambient
                if ambient_delta > 0:
                    # Room is colder than reference — warm up
                    ambient_adj = round(ambient_delta * AMBIENT_COMPENSATION_PER_F)
                    effective += ambient_adj

            # Clamp to topper range
            effective = max(-10, min(10, effective))

            # Respect manual override floor
            floor = state.get("override_floor", {}).get(phase, -10)
            effective = max(effective, floor)

            # Read current setting
            preset_entity = ZONE_PRESETS[zone][phase]
            current_setting = self._read_entity(preset_entity)
            if current_setting is None:
                self.log(f"[{zone}] Can't read {phase} preset", level="WARNING")
                continue
            current_setting = int(current_setting)

            # 5. Body sensor extreme check — only override preferences
            #    when sensor data is clearly actionable
            new_setting = effective
            action = "preference"

            if body_avg is not None and body_avg > BODY_HOT_THRESHOLD_F:
                # Clearly too hot — step cooler from current, don't jump
                state["hot_streak"] = state.get("hot_streak", 0) + 1
                if state["hot_streak"] >= 2:
                    # 2+ consecutive hot readings — cool by 1
                    new_setting = min(effective, current_setting - 1)
                    action = "hot_safety"
                    self.log(
                        f"[{zone}] HOT SAFETY: body={body_avg:.1f}°F > "
                        f"{BODY_HOT_THRESHOLD_F}°F — cooling")
            else:
                state["hot_streak"] = 0

            # Clamp final result
            new_setting = max(-10, min(10, new_setting))
            new_setting = max(new_setting, floor)

            elapsed = (now - datetime.fromisoformat(state["bedtime_ts"])).total_seconds() / 60
            amb_s = f"{ambient:.0f}" if ambient is not None else "?"
            self.log(
                f"[{zone}] t+{elapsed:.0f}m {phase} | "
                f"body={body_avg:.1f}°F amb={amb_s}°F | "
                f"base={baseline:+d} learn={learned_adj:+d} "
                f"eff={effective:+d} | "
                f"setting: {current_setting:+d}→{new_setting:+d} "
                f"({action})")

            # 6. Apply if changed — write to active phase only
            if new_setting != current_setting:
                try:
                    self.call_service("number/set_value", entity_id=preset_entity, value=new_setting)
                    state["last_settings_pushed"][phase] = new_setting
                except Exception as svc_err:
                    self.log(f"[{zone}] FAILED to set {preset_entity}: {svc_err}", level="ERROR")
                self.log(f"[{zone}] SET {phase} = {new_setting:+d}")
                self._log_to_influx(zone, phase, sensors, new_setting, action)
            else:
                self._log_to_influx(zone, phase, sensors, current_setting, "hold")

        # Periodic save
        if not hasattr(self, "_loop_count"):
            self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 10 == 0:
            self._save_state()

    # ── End of Night ───────────────────────────────────────────────

    def _end_night(self, zone, state, now):
        """Clean up after a sleep session and update multi-night learning."""
        bedtime = datetime.fromisoformat(state["bedtime_ts"])
        duration = (now - bedtime).total_seconds() / 3600
        overrides = state["override_history"]
        temps = state["body_temp_history"]
        avg_t = (sum(temps) / len(temps)) if temps else 0
        self.log(
            f"[{zone}] *** WAKE *** {duration:.1f}h | "
            f"body avg {avg_t:.1f}°F | "
            f"{len(overrides)} overrides")

        # Multi-night learning: if user made overrides, adjust baselines
        if overrides:
            self._update_learned(zone, overrides)

        # Reset presets to learned baseline for next night
        for phase, entity in ZONE_PRESETS[zone].items():
            baseline_val = USER_BASELINE.get(phase, -6)
            learned_adj = self.learned.get(zone, {}).get(phase, 0)
            reset_val = max(-10, min(10, baseline_val + learned_adj))
            try:
                self.call_service("number/set_value", entity_id=entity, value=reset_val)
            except Exception as exc:
                self.log(f"[{zone}] Failed to reset {phase} to {reset_val}: {exc}", level="ERROR")
        learned_baselines = {
            p: USER_BASELINE[p] + self.learned.get(zone, {}).get(p, 0)
            for p in USER_BASELINE
        }
        self.log(f"[{zone}] Presets reset to learned baseline: {learned_baselines}")

        state["bedtime_ts"] = None
        state["last_settings_pushed"] = {}
        state["override_history"] = []
        state["override_floor"] = {}
        state["bed_empty_since"] = None
        self._save_state()

    # ── Override Detection ───────────────────────────────────────────

    def _detect_override(self, zone, state, current_phase):
        """Check if user manually changed the active preset."""
        last_pushed = state["last_settings_pushed"].get(current_phase)
        if last_pushed is None:
            return None

        preset_entity = ZONE_PRESETS[zone][current_phase]
        actual = self._read_entity(preset_entity)
        if actual is None:
            return None
        actual = int(actual)

        if actual != last_pushed:
            sensors = self._read_sensors(zone)
            body = sensors.get("body_avg")
            bedtime_dt = datetime.fromisoformat(state["bedtime_ts"])
            mins = (datetime.now() - bedtime_dt).total_seconds() / 60
            self.log(
                f"[{zone}] MANUAL OVERRIDE t+{mins:.0f}m | "
                f"{current_phase}: {last_pushed:+d}->{actual:+d} "
                f"(delta={actual - last_pushed:+d}) | "
                f"body={f'{body:.1f}' if body else '?'}°F")
            return {
                "phase": current_phase,
                "expected": last_pushed,
                "actual": actual,
                "delta": actual - last_pushed,
                "timestamp": datetime.now().isoformat(),
                "body_temp": body,
            }
        return None

    def _on_setting_changed(self, entity, attribute, old, new, kwargs):
        """Real-time listener for preset changes (kill switch detection)."""
        zone = kwargs.get("zone")
        if zone not in self.zone_state:
            return
        state = self.zone_state[zone]
        if state["bedtime_ts"] is None:
            return
        if state["manual_mode"]:
            return

        phase = kwargs.get("phase")
        pushed = state["last_settings_pushed"].get(phase)
        try:
            new_val = int(float(new))
        except (ValueError, TypeError):
            return
        if pushed is not None and new_val == pushed:
            return  # Our own write

        # Only count changes on the active phase
        active_phase = self._get_active_phase(zone, state)
        if phase != active_phase:
            return

        state["recent_setting_changes"].append(datetime.now().timestamp())
        self._check_kill_switch(zone, state)

    def _check_kill_switch(self, zone, state):
        """3+ setting changes within 20 seconds = manual mode for the night."""
        now_ts = datetime.now().timestamp()
        cutoff = now_ts - KILL_SWITCH_WINDOW_SEC
        recent = [t for t in state["recent_setting_changes"] if t > cutoff]
        state["recent_setting_changes"] = recent

        if len(recent) >= KILL_SWITCH_CHANGES:
            state["manual_mode"] = True
            state["recent_setting_changes"] = []
            self.log(f"[{zone}] *** KILL SWITCH *** {len(recent)} changes in {KILL_SWITCH_WINDOW_SEC}s — manual mode")
            self._notify("SleepSync: Kill switch activated — manual mode for tonight")
            return True
        return False

    # ── Sensor Reading ───────────────────────────────────────────────

    def _read_entity(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(state)
        except (ValueError, TypeError):
            self.log(f"Entity {entity_id} not numeric: {state!r}", level="WARNING")
            return None

    def _read_sensors(self, zone):
        sensors = {}
        for key, entity_id in ZONE_SENSORS[zone].items():
            sensors[key] = self._read_entity(entity_id)

        # Determine which sensor is the "inner" one (closest to partner).
        # For the left zone, body_right is inner; for right zone, body_left.
        if zone == "left":
            inner_key, outer_key = "body_right", "body_left"
        else:
            inner_key, outer_key = "body_left", "body_right"

        # Check if the adjacent zone is significantly hotter — if so, the
        # inner sensor is reading partner heat bleed, not our body temp.
        adj_zone = "right" if zone == "left" else "left"
        adj_sensors = ZONE_SENSORS.get(adj_zone, {})
        adj_body_keys = ["body_right", "body_center", "body_left"]
        adj_vals = []
        for k in adj_body_keys:
            eid = adj_sensors.get(k)
            if eid:
                v = self._read_entity(eid)
                if v is not None:
                    adj_vals.append(v)
        adj_avg = (sum(adj_vals) / len(adj_vals)) if adj_vals else None

        inner_val = sensors.get(inner_key)
        outer_val = sensors.get(outer_key)
        exclude_inner = False
        if (inner_val is not None and outer_val is not None
                and adj_avg is not None and adj_avg > inner_val
                and inner_val - outer_val > 5.0):
            # Inner sensor is >5°F hotter than outer, and adjacent zone is
            # even hotter → inner sensor is reading partner bleed-through
            exclude_inner = True
            self.log(
                f"[{zone}] Excluding {inner_key} ({inner_val:.1f}°F) — "
                f"adj zone avg {adj_avg:.1f}°F, outer {outer_val:.1f}°F "
                f"(bleed-through)")

        use_keys = ["body_center"]
        if not exclude_inner:
            use_keys.append(inner_key)
        use_keys.append(outer_key)

        body_vals = [sensors[k] for k in use_keys if sensors.get(k) is not None]
        if body_vals:
            # Use median to ignore outlier sensors — one edge may read room
            # temp while another retains residual heat; median tracks the
            # sensor most representative of actual body temperature.
            body_vals_sorted = sorted(body_vals)
            n = len(body_vals_sorted)
            sensors["body_avg"] = body_vals_sorted[n // 2] if n % 2 else (
                body_vals_sorted[n // 2 - 1] + body_vals_sorted[n // 2]) / 2
        return sensors

    def _is_sleeping(self, zone):
        progress = self._read_entity(ZONE_SENSORS[zone]["run_progress"])
        return progress is not None and progress > 0

    def _notify(self, message):
        try:
            self.call_service(NOTIFY_SERVICE, message=message, title="SleepSync")
        except (TypeError, AttributeError, ConnectionError) as e:
            self.log(f"Notify failed: {e}", level="WARNING")

    # ── InfluxDB Logging ─────────────────────────────────────────────

    def _log_to_influx(self, zone, phase, sensors, setting, action="hold"):
        """Write a control-loop data point to InfluxDB."""
        body_r = sensors.get("body_right")
        body_c = sensors.get("body_center")
        body_l = sensors.get("body_left")
        body_avg = sensors.get("body_avg")
        ambient = sensors.get("ambient")

        fields = []
        if body_r is not None:
            fields.append(f"body_right={body_r}")
        if body_c is not None:
            fields.append(f"body_center={body_c}")
        if body_l is not None:
            fields.append(f"body_left={body_l}")
        if body_avg is not None:
            fields.append(f"body_median={body_avg}")
        if ambient is not None:
            fields.append(f"ambient={ambient}")
        if setting is not None:
            fields.append(f"setting={setting}i")
        fields.append(f"hot_threshold={BODY_HOT_THRESHOLD_F}")

        if not fields:
            return

        tags = f"zone={zone},phase={phase or 'unknown'},action={action}"
        line = f"sleep_controller,{tags} {','.join(fields)}"

        try:
            req = Request(
                f"{INFLUXDB_URL}/write?db={INFLUXDB_DB}&precision=s",
                data=line.encode(),
                method="POST",
            )
            urlopen(req, timeout=5)
        except Exception as e:
            self.log(f"InfluxDB write failed: {e}", level="WARNING")

    # ── State Persistence ────────────────────────────────────────────

    def _save_state(self):
        data = {}
        for zone in self.zones:
            data[zone] = {
                "state": dict(self.zone_state[zone]),
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
                self.zone_state[zone] = self._fresh_zone_state()
            self.log(f"Loaded state from {STATE_FILE}")
        except (json.JSONDecodeError, KeyError) as e:
            self.log(f"State file corrupted: {e} — starting fresh", level="ERROR")

    # ── Multi-Night Learning ─────────────────────────────────────────

    def _update_learned(self, zone, overrides):
        """Update learned adjustments from tonight's overrides.

        Strategy: for each phase that was overridden, compute the average
        delta from baseline. Blend with existing learned value using
        exponential moving average (alpha=0.3 = 30% new, 70% old).
        """
        if zone not in self.learned:
            self.learned[zone] = {}

        # Group overrides by phase
        phase_deltas = {}
        for ov in overrides:
            p = ov.get("phase")
            baseline = USER_BASELINE.get(p, -6)
            # How far the user moved from the baseline
            delta_from_baseline = ov["actual"] - baseline
            phase_deltas.setdefault(p, []).append(delta_from_baseline)

        alpha = 0.3  # Learning rate
        for phase, deltas in phase_deltas.items():
            avg_delta = sum(deltas) / len(deltas)
            old = self.learned[zone].get(phase, 0)
            new = old * (1 - alpha) + avg_delta * alpha
            # Round to nearest int (topper only accepts integers)
            self.learned[zone][phase] = round(new)
            self.log(
                f"[{zone}] LEARNING: {phase} adj {old:+d} → {round(new):+d} "
                f"(tonight avg delta: {avg_delta:+.1f})")

        self._save_learned()

    def _save_learned(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LEARNING_FILE.write_text(json.dumps(self.learned, indent=2))
        except OSError as e:
            self.log(f"Failed to save learned prefs: {e}", level="WARNING")

    def _load_learned(self):
        if not LEARNING_FILE.exists():
            self.log("No learned preferences — using raw baselines")
            self.learned = {}
            return
        try:
            self.learned = json.loads(LEARNING_FILE.read_text())
            self.log(f"Loaded learned preferences: {self.learned}")
        except (json.JSONDecodeError, KeyError) as e:
            self.log(f"Learned prefs corrupted: {e}", level="ERROR")
            self.learned = {}
