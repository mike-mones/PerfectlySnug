"""
Simplified Sleep Temperature Controller — AppDaemon App
========================================================

Controls the PerfectlySnug topper setting (-10 to +10) based on:
  - Body sensor temperature (from the topper's built-in sensors)
  - Phase of night (bedtime → sleep → wake, from elapsed time)
  - Static user baseline curve (USER_BASELINE)

Does NOT use: Apple Watch sleep stages, PID control, ML classifiers,
transfer function learning, or multi-night trend adaptation.

Control loop (every 5 min):
  1. Detect if topper is running (occupancy)
  2. Determine phase from elapsed time
  3. Read body sensors
  4. If body temp > target: step 1 cooler. If below: step 1 warmer.
  5. Respect deadband, clamp to baseline ± MAX_OFFSET
  6. Detect manual overrides → disable controller for the night
  7. At wake: reset presets to baseline
"""

import json
from datetime import datetime
from pathlib import Path

import hassapi as hass

# ── Configuration ────────────────────────────────────────────────────────

LOOP_INTERVAL_SEC = 300     # 5 min control loop
MAX_STEP_PER_LOOP = 1       # ±1 per cycle
DEADBAND_F = 1.0            # Don't adjust if error < 1°F
OCCUPANCY_THRESHOLD_F = 78.0  # Body temp below this = nobody in bed
OCCUPANCY_HOLD_MINUTES = 20  # Hold setting after first detecting body

# User's preferred baseline settings (-10 to +10)
USER_BASELINE = {
    "bedtime": -8,      # Aggressive cooling at sleep onset
    "sleep":   -6,      # Moderate cooling during bulk of night
    "wake":    -5,      # Ease off cooling toward morning
}
# Max offset the controller can apply around baseline
MAX_OFFSET_FROM_BASELINE = 3

# Body temperature target (°F) — single value, no per-stage targets
BODY_TEMP_TARGET_F = 83.0

# Kill switch: rapid manual changes disable controller for the night
KILL_SWITCH_CHANGES = 3
KILL_SWITCH_WINDOW_SEC = 20

# Notification entity
NOTIFY_SERVICE = "notify/mobile_app_mike_mones_iphone_14"

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


# ── Main AppDaemon App ──────────────────────────────────────────────────

class SleepController(hass.Hass):

    def initialize(self):
        self.log("=" * 60)
        self.log("Sleep Controller v3 (simplified) initializing")

        self.zones = ["left"]
        self.zone_state = {}

        self._load_state()

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
        self.log(f"  Body temp target: {BODY_TEMP_TARGET_F}°F")
        self.log(f"  Max offset: ±{MAX_OFFSET_FROM_BASELINE}")
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
            "last_run_progress": 0,
            "last_restart_ts": None,
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

                # Auto-restart if topper schedule exhausted but body still in bed
                prev_progress = state.get("last_run_progress", 0)
                body_sensors = self._read_sensors(zone)
                body_in_bed = (body_sensors.get("body_avg") or 0) >= OCCUPANCY_THRESHOLD_F
                if prev_progress > 90 and body_in_bed:
                    self.log(f"[{zone}] Topper shut down (progress={prev_progress}) but body in bed. Auto-restarting!")
                    try:
                        self.call_service("switch/turn_on", entity_id=f"switch.smart_topper_{zone}_side_running")
                    except Exception as svc_err:
                        self.log(f"[{zone}] FAILED to auto-restart: {svc_err}", level="ERROR")
                    self._notify(f"SleepSync: Topper schedule exhausted ({duration:.1f}h). Auto-restarted.")
                    state["last_run_progress"] = 0
                    state["last_restart_ts"] = now.isoformat()
                    continue

                # Wait for restart to take effect
                last_restart = state.get("last_restart_ts")
                if last_restart and (now - datetime.fromisoformat(last_restart)).total_seconds() < 120:
                    self.log(f"[{zone}] Waiting for auto-restart to take effect.")
                    continue

                # ── End of night ──
                overrides = len(state["override_history"])
                temps = state["body_temp_history"]
                avg_t = (sum(temps) / len(temps)) if temps else 0
                self.log(
                    f"[{zone}] *** WAKE *** {duration:.1f}h | "
                    f"body avg {avg_t:.1f}°F | "
                    f"{overrides} overrides")

                # Reset presets to baseline for next night
                for phase, entity in ZONE_PRESETS[zone].items():
                    baseline_val = USER_BASELINE.get(phase, -6)
                    try:
                        self.call_service("number/set_value", entity_id=entity, value=baseline_val)
                    except Exception as exc:
                        self.log(f"[{zone}] Failed to reset {phase} to {baseline_val}: {exc}", level="ERROR")
                self.log(f"[{zone}] Presets reset to baseline: {USER_BASELINE}")

                state["bedtime_ts"] = None
                state["last_settings_pushed"] = {}
                state["override_history"] = []
                self._save_state()
                continue

            if not is_sleeping:
                continue

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
                # Accept the override, don't learn from it
                state["last_settings_pushed"][phase] = override["actual"]
                state["override_history"].append(override)
                self._save_state()
                continue

            # 4. Simple threshold control
            baseline = USER_BASELINE.get(phase, -6)
            error = body_avg - BODY_TEMP_TARGET_F

            # Read current setting
            preset_entity = ZONE_PRESETS[zone][phase]
            current_setting = self._read_entity(preset_entity)
            if current_setting is None:
                self.log(f"[{zone}] Can't read {phase} preset", level="WARNING")
                continue
            current_setting = int(current_setting)

            # Deadband: don't adjust if error is small
            if abs(error) < DEADBAND_F:
                elapsed = (now - datetime.fromisoformat(state["bedtime_ts"])).total_seconds() / 60
                self.log(
                    f"[{zone}] t+{elapsed:.0f}m {phase} | "
                    f"body={body_avg:.1f}°F target={BODY_TEMP_TARGET_F}°F "
                    f"err={error:+.1f}°F DEADBAND — no change | "
                    f"setting={current_setting:+d}")
                continue

            # Step toward correction
            if error > 0:
                # Too warm → cool more (decrease setting)
                new_setting = current_setting - 1
            else:
                # Too cool → warm up (increase setting)
                new_setting = current_setting + 1

            # Clamp to baseline ± MAX_OFFSET
            lower = baseline - MAX_OFFSET_FROM_BASELINE
            upper = baseline + MAX_OFFSET_FROM_BASELINE
            new_setting = max(lower, min(upper, new_setting))

            # Hard clamp: -10 to 0 (cooling only, never heating)
            new_setting = max(-10, min(0, new_setting))

            elapsed = (now - datetime.fromisoformat(state["bedtime_ts"])).total_seconds() / 60
            self.log(
                f"[{zone}] t+{elapsed:.0f}m {phase} | "
                f"body={body_avg:.1f}°F target={BODY_TEMP_TARGET_F}°F "
                f"err={error:+.1f}°F | "
                f"setting: {current_setting:+d}→{new_setting:+d}")

            # 5. Apply if changed — write to ALL presets
            if new_setting != current_setting:
                for p_name, p_entity in ZONE_PRESETS[zone].items():
                    try:
                        self.call_service("number/set_value", entity_id=p_entity, value=new_setting)
                        state["last_settings_pushed"][p_name] = new_setting
                    except Exception as svc_err:
                        self.log(f"[{zone}] FAILED to set {p_entity}: {svc_err}", level="ERROR")
                self.log(f"[{zone}] SET all presets = {new_setting:+d}")

        # Periodic save
        if not hasattr(self, "_loop_count"):
            self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 10 == 0:
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
        body_vals = [v for k in ("body_right", "body_center", "body_left")
                     if (v := sensors.get(k)) is not None]
        if body_vals:
            sensors["body_avg"] = sum(body_vals) / len(body_vals)
        return sensors

    def _is_sleeping(self, zone):
        progress = self._read_entity(ZONE_SENSORS[zone]["run_progress"])
        return progress is not None and progress > 0

    def _notify(self, message):
        try:
            self.call_service(NOTIFY_SERVICE, message=message, title="SleepSync")
        except (TypeError, AttributeError, ConnectionError) as e:
            self.log(f"Notify failed: {e}", level="WARNING")

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
