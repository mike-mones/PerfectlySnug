"""
Preference-Based Sleep Temperature Controller — AppDaemon App
==============================================================

Controls the PerfectlySnug topper setting (-10 to +10) based on:
  - Learned preference curve (baseline + multi-night override learning)
  - Ambient room temperature compensation
  - Body sensor extremes only (>85°F = too hot, used for safety cooling)
  - 4-phase night profile: bedtime → deep → REM → wake
  - Pre-wake thermal ramp for gentle morning transition

Body sensors at 80–85°F are AMBIGUOUS — they reflect body-mattress contact
temperature, not perceived comfort. The controller does NOT chase a body
temp target. Instead, it follows the user's learned preferences and only
intervenes when sensors show clear extremes.

Manual overrides are RESPECTED: after a user manually adjusts the setting,
the controller freezes for OVERRIDE_FREEZE_MIN minutes, then resumes from
the user's chosen value. Overrides also feed into multi-night learning.

Control loop (every 5 min):
  1. Detect if topper is running (occupancy)
  2. Determine phase from elapsed time (bedtime → deep → REM → wake)
  3. Compute effective setting: learned baseline + ambient compensation
  4. If body sensor > 85°F: step 1 cooler (safety)
  5. Detect manual overrides → freeze controller + learn for future nights
  6. Pre-wake ramp: gradually shift temperature 30 min before wake
  7. At wake: save override data, reset presets to learned baseline
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import hassapi as hass

# Add ml/ to path so we can import the learner
_project_root = Path(__file__).parent  # apps/ directory (works both locally and on HA Green)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from ml.learner import SleepLearner, NightRecord

# ── Configuration ────────────────────────────────────────────────────────

LOOP_INTERVAL_SEC = 300     # 5 min control loop
OCCUPANCY_THRESHOLD_F = 78.0  # Body temp below this = nobody in bed
OCCUPANCY_HOLD_MINUTES = 20  # Hold setting after first detecting body

# User's preferred baseline settings (-10 to +10)
USER_BASELINE = {
    "bedtime": -8,      # Aggressive cooling at sleep onset
    "deep":    -7,      # Cooler during first half of night (deep sleep)
    "rem":     -5,      # Warmer during second half (REM-dominant)
    "wake":    -4,      # Ease off cooling toward morning
}

# Phase timing (minutes from bedtime)
BEDTIME_DURATION_MIN = 60     # First hour = bedtime phase
DEEP_DURATION_MIN = 240       # Next 4 hours = deep sleep phase
# After deep, REM phase runs until pre-wake ramp begins
PRE_WAKE_RAMP_MIN = 30        # Start ramping 30 min before estimated wake

# Body sensor thresholds — only act on clear extremes
BODY_HOT_THRESHOLD_F = 85.0   # Above this = definitely too hot, cool down
BODY_COLD_THRESHOLD_F = 80.0  # Below this while occupied = definitely cold
# 80–85°F is the "ambiguous zone" — follow preferences, don't adjust

# Ambient temperature compensation
# The topper blows room-temp air to cool/heat. In a cold room, even mild
# cooling settings blast freezing air. Compensation must be aggressive enough
# to switch from cooling to heating when the room is very cold.
AMBIENT_REFERENCE_F = 70.0
AMBIENT_COMPENSATION_PER_F = 0.8          # Base rate (was 0.5 — too timid)
AMBIENT_COLD_THRESHOLD_F = 65.0           # Below this, ramp compensation harder
AMBIENT_COLD_EXTRA_PER_F = 0.5            # Extra compensation per °F below threshold
AMBIENT_FLOOR_TEMP_F = 60.0               # Below this, force minimum setting
AMBIENT_FLOOR_SETTING = 0                 # Never cool below neutral in a freezing room
MAX_AMBIENT_COMPENSATED_SETTING = 0       # Never heat — user runs warm. 0 = neutral ceiling.

# Kill switch: rapid manual changes disable controller for the night
KILL_SWITCH_CHANGES = 3
KILL_SWITCH_WINDOW_SEC = 300              # 5 min window (was 20s — too tight)

# Deadband: don't change the setting unless computed value differs by this much
SETTING_DEADBAND = 2
# Cooldown: minimum seconds between setting changes (topper thermal lag)
CHANGE_COOLDOWN_SEC = 900  # 15 minutes

# Override freeze: after a manual override, freeze controller for this long
OVERRIDE_FREEZE_MIN = 60                  # 1 hour (was 15 min — too short, controller fought user)

# Override debounce: wait this long after the last change before committing
# the override to history. Prevents intermediate values (e.g., scrolling
# through -10 → -8 → -5) from being recorded as separate overrides.
OVERRIDE_DEBOUNCE_SEC = 60

# Auto-restart debounce: wait this long after restart before allowing _end_night
AUTO_RESTART_DEBOUNCE_SEC = 300  # 5 minutes

# Multi-night learning: how many nights of override data to retain
LEARNING_HISTORY_NIGHTS = 7

# Room temperature sensor — configurable via apps.yaml
ROOM_TEMP_ENTITY_DEFAULT = "sensor.superior_6000s_temperature"

# Notification entity
NOTIFY_SERVICE = "notify/mobile_app_mike_mones_iphone_14"

# PostgreSQL logging (Pi at 192.168.0.75)
POSTGRES_HOST_DEFAULT = "192.168.0.75"
POSTGRES_PORT = 5432
POSTGRES_DB = "sleepdata"
POSTGRES_USER = "sleepsync"
POSTGRES_PASS = "sleepsync_local"

# ── Entity Mappings ──────────────────────────────────────────────────────

ZONE_SENSORS = {
    "left": {
        "body_right":   "sensor.smart_topper_left_side_body_sensor_right",
        "body_center":  "sensor.smart_topper_left_side_body_sensor_center",
        "body_left":    "sensor.smart_topper_left_side_body_sensor_left",
        "ambient":      "sensor.smart_topper_left_side_ambient_temperature",
        "run_progress":  "sensor.smart_topper_left_side_run_progress",
        "blower_pct":   "sensor.smart_topper_left_side_blower_output",
        "heater_head_pct": "sensor.smart_topper_left_side_heater_head_output",
        "heater_foot_pct": "sensor.smart_topper_left_side_heater_foot_output",
    },
    "right": {
        "body_right":   "sensor.smart_topper_right_side_body_sensor_right",
        "body_center":  "sensor.smart_topper_right_side_body_sensor_center",
        "body_left":    "sensor.smart_topper_right_side_body_sensor_left",
        "ambient":      "sensor.smart_topper_right_side_ambient_temperature",
        "run_progress":  "sensor.smart_topper_right_side_run_progress",
        "blower_pct":   "sensor.smart_topper_right_side_blower_output",
        "heater_head_pct": "sensor.smart_topper_right_side_heater_head_output",
        "heater_foot_pct": "sensor.smart_topper_right_side_heater_foot_output",
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

        # Configurable settings from apps.yaml
        self.room_temp_entity = self.args.get(
            "room_temp_entity", ROOM_TEMP_ENTITY_DEFAULT)
        self.pg_host = self.args.get("postgres_host", POSTGRES_HOST_DEFAULT)

        self.zones = ["left"]
        self.zone_state = {}
        self.learned = {}  # Multi-night learned adjustments (legacy, kept for compat)
        self._pg_conn = None  # Lazy PostgreSQL connection

        # ML learner — replaces simple EMA with multi-signal adaptive model
        self.learner = SleepLearner(STATE_DIR)
        self.learner.load()
        if not self.learner.models:
            # Bootstrap from existing override history
            count = self.learner.bootstrap_from_override_history()
            if count > 0:
                self.learner.save()
                self.log(f"ML learner bootstrapped from {count} override records")

        self._load_state()
        self._load_learned()

        for zone in self.zones:
            if zone not in self.zone_state:
                self.zone_state[zone] = self._fresh_zone_state()

        # Control loop
        self.run_every(self._control_loop, "now", LOOP_INTERVAL_SEC)

        # Listen for sleep rating notification responses
        self.listen_event(self._on_sleep_rating, "mobile_app_notification_action")

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
        self.log(f"  Room temp: {self.room_temp_entity}")
        self.log(f"  Postgres: {self.pg_host}:{POSTGRES_PORT}/{POSTGRES_DB}")
        self.log(f"  Learned adjustments: {self.learned}")
        for zone in self.zones:
            summary = self.learner.get_model_summary(zone)
            self.log(f"  ML learner [{zone}]: {summary.get('nights', 0)} nights, "
                     f"status={summary.get('status', 'no_data')}")
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
            "last_setting_change_ts": None,
            "hot_streak": 0,
            "bed_empty_since": None,
            "override_freeze_until": None,  # Freeze controller after manual override
            "pending_override": None,       # Debounce: {phase, value, first_seen, ...}
            "pending_override_ts": None,    # When the pending override was last updated
            "last_override_value": None,    # User's last manual setting value
            "override_floor": None,         # Don't go colder than this after override
        }

    # ── Phase Detection ──────────────────────────────────────────────

    def _get_active_phase(self, zone, state):
        """Determine phase from elapsed time: bedtime → deep → rem → wake.

        4-phase night profile based on typical sleep architecture:
          - bedtime: first BEDTIME_DURATION_MIN (cooling for sleep onset)
          - deep:    next DEEP_DURATION_MIN (cooler for deep/NREM-dominant first half)
          - rem:     remainder until pre-wake ramp (warmer for REM-dominant second half)
          - wake:    PRE_WAKE_RAMP_MIN before estimated end (gradual warm-up)
        """
        if state["bedtime_ts"] is None:
            return None

        bedtime = datetime.fromisoformat(state["bedtime_ts"])
        now = datetime.now()
        elapsed_min = (now - bedtime).total_seconds() / 60.0

        # Phase 1: Bedtime
        if elapsed_min < BEDTIME_DURATION_MIN:
            return "bedtime"

        # Check run_progress for wake phase detection
        progress = self._read_entity(ZONE_SENSORS[zone]["run_progress"])
        if progress is not None and progress > 0:
            total_min = elapsed_min / (progress / 100.0)
            remaining_min = total_min - elapsed_min
            # Phase 4: Pre-wake ramp
            if remaining_min <= PRE_WAKE_RAMP_MIN:
                return "wake"

        # Phase 2: Deep sleep (first ~4 hours after bedtime)
        if elapsed_min < (BEDTIME_DURATION_MIN + DEEP_DURATION_MIN):
            return "deep"

        # Phase 3: REM-dominant second half
        return "rem"

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
                state["last_setting_change_ts"] = None

                # Read current settings as starting point
                for phase, entity in ZONE_PRESETS[zone].items():
                    val = self._read_entity(entity)
                    if val is not None:
                        state["last_settings_pushed"][phase] = int(val)
                # Also populate deep/rem from the sleep preset so override
                # detection works immediately for all 4 controller phases
                sleep_val = state["last_settings_pushed"].get("sleep")
                if sleep_val is not None:
                    state["last_settings_pushed"].setdefault("deep", sleep_val)
                    state["last_settings_pushed"].setdefault("rem", sleep_val)

                self.log(f"[{zone}] *** BEDTIME *** presets: {state['last_settings_pushed']}")
                continue

            # ── Wake detection ──
            if not is_sleeping and state["bedtime_ts"] is not None:
                bedtime = datetime.fromisoformat(state["bedtime_ts"])
                duration = (now - bedtime).total_seconds() / 3600

                # Topper's built-in schedule shut off — auto-restart if body in bed
                body_sensors = self._read_sensors(zone)
                body_avg = body_sensors.get("body_avg")
                # Only consider body present if sensors returned valid data
                # AND readings are above threshold. None = sensor unavailable,
                # which should NOT be treated as "bed empty".
                body_in_bed = body_avg is not None and body_avg >= OCCUPANCY_THRESHOLD_F
                sensors_unavailable = body_avg is None
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

                # Wait for restart to take effect (extended debounce)
                last_restart = state.get("last_restart_ts")
                if last_restart and (now - datetime.fromisoformat(last_restart)).total_seconds() < AUTO_RESTART_DEBOUNCE_SEC:
                    self.log(f"[{zone}] Waiting for auto-restart to take effect.")
                    continue

                if sensors_unavailable:
                    # Sensors are unavailable — don't assume bed is empty
                    self.log(f"[{zone}] Sensors unavailable (topper off) — waiting for data", level="WARNING")
                    continue

                # Body not in bed and topper off — end session
                self._end_night(zone, state, now)
                continue

            if not is_sleeping:
                continue

            # ── Occupancy-based shutoff (topper still running but bed empty) ──
            body_sensors_check = self._read_sensors(zone)
            body_avg_check = body_sensors_check.get("body_avg")
            # Only start bed-empty timer when sensors explicitly read below
            # threshold. None (unavailable) should NOT trigger shutoff.
            if body_avg_check is not None:
                body_present = body_avg_check >= OCCUPANCY_THRESHOLD_F
            else:
                # Sensors unavailable — assume body is still present (safe default)
                body_present = True
                self.log(f"[{zone}] Sensors unavailable — assuming still in bed", level="WARNING")

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

            # ── Override freeze: respect manual adjustments ──
            freeze_until = state.get("override_freeze_until")
            if freeze_until is not None:
                freeze_dt = datetime.fromisoformat(freeze_until)
                if now < freeze_dt:
                    remaining = (freeze_dt - now).total_seconds() / 60
                    self.log(f"[{zone}] Override freeze — {remaining:.0f}m remaining")
                    continue
                else:
                    state["override_freeze_until"] = None
                    # Resume from user's chosen value, not controller's old target.
                    # The user overrode for a reason — respect it as a floor.
                    user_val = state.get("last_override_value")
                    if user_val is not None:
                        state["override_floor"] = user_val
                        self.log(
                            f"[{zone}] Override freeze expired — resuming with "
                            f"floor={user_val:+d} (user's last override)"
                        )
                    else:
                        self.log(f"[{zone}] Override freeze expired — resuming control")

            # ── Active sleep control ──

            # 1. Determine phase
            phase = self._get_active_phase(zone, state)
            if phase is None:
                continue

            # 2. Read body sensors
            sensors = self._read_sensors(zone)
            body_avg = sensors.get("body_avg")
            # Use Levoit room temp for ambient compensation — topper's
            # "ambient" sensor reads mattress surface heat, not room air
            ambient = self._read_room_temp()

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

            # 3. Check for manual override (with debounce)
            #    When the user adjusts temperature, they often scroll through
            #    several values before landing on their choice. We detect the
            #    change immediately (for freeze + kill switch) but delay
            #    committing to override_history until OVERRIDE_DEBOUNCE_SEC
            #    passes with no further changes — capturing only the final value.
            override = self._detect_override(zone, state, phase)
            if override:
                state["recent_setting_changes"].append(datetime.now().timestamp())
                if self._check_kill_switch(zone, state):
                    continue

                # Update tracking immediately (prevents controller from reverting)
                state["last_settings_pushed"][phase] = override["actual"]
                # Keep deep/rem in sync since they share L2
                if phase in ("deep", "rem"):
                    state["last_settings_pushed"]["deep"] = override["actual"]
                    state["last_settings_pushed"]["rem"] = override["actual"]

                # Flush any existing pending override before replacing
                old_pending = state.get("pending_override")
                if old_pending and old_pending.get("actual") != override["actual"]:
                    state["override_history"].append(old_pending)
                    self.log(f"[{zone}] Flushed prior pending override: {old_pending['phase']}={old_pending['actual']:+d}")

                # Start/restart the debounce timer
                state["pending_override"] = override
                state["pending_override_ts"] = now.isoformat()

                # Set freeze immediately (user intent is clear even if value isn't final)
                from datetime import timedelta as _td
                state["override_freeze_until"] = (
                    datetime.now() + _td(minutes=OVERRIDE_FREEZE_MIN)
                ).isoformat()
                state["last_override_value"] = override["actual"]
                self.log(
                    f"[{zone}] OVERRIDE detected: {phase} "
                    f"{override['expected']:+d} → {override['actual']:+d} "
                    f"— freezing {OVERRIDE_FREEZE_MIN}m, floor={override['actual']:+d}")
                self._save_state()
                continue

            # Check if a pending override has stabilized (debounce expired)
            pending = state.get("pending_override")
            pending_ts = state.get("pending_override_ts")
            if pending and pending_ts:
                age = (now - datetime.fromisoformat(pending_ts)).total_seconds()
                if age >= OVERRIDE_DEBOUNCE_SEC:
                    # Value hasn't changed for OVERRIDE_DEBOUNCE_SEC — commit it
                    state["override_history"].append(pending)
                    state["pending_override"] = None
                    state["pending_override_ts"] = None
                    elapsed = (now - datetime.fromisoformat(
                        state["bedtime_ts"])).total_seconds() / 60
                    self.log(
                        f"[{zone}] OVERRIDE committed: {pending['phase']} "
                        f"= {pending['actual']:+d} (settled after {age:.0f}s)")
                    self._log_to_postgres(
                        zone, pending["phase"], elapsed, sensors, ambient,
                        pending["actual"], pending["actual"],
                        USER_BASELINE.get(pending["phase"], -6),
                        self.learned.get(zone, {}).get(pending["phase"], 0),
                        "override", override_delta=pending["delta"])
                    self._save_state()

            # 4. Compute effective setting from ML learner + ambient
            #    The learner blends science baselines with learned preferences
            #    based on its confidence level. Low confidence → baselines.
            #    High confidence → personalized predictions.
            ml_recs = self.learner.get_recommendations(zone, room_temp_f=ambient)
            baseline = ml_recs.get(phase, USER_BASELINE.get(phase, -6))

            # Legacy EMA adjustment — applied on top of ML if still present
            # (will phase out as ML confidence grows)
            learned_adj = round(self.learned.get(zone, {}).get(phase, 0))
            # Scale legacy adjustment by inverse of ML confidence to avoid double-counting
            ml_confidence = 0.0
            if zone in self.learner.models and phase in self.learner.models[zone].phases:
                ml_confidence = self.learner.models[zone].phases[phase].confidence
            legacy_scale = max(0.0, 1.0 - ml_confidence)
            effective = baseline + round(learned_adj * legacy_scale)

            # Ambient compensation: colder room → warmer setting
            # ALWAYS applies regardless of ML confidence — the ML learns
            # preferences at ~70°F; room temp compensation is a separate
            # physical reality that the ML can't override. You can't cool
            # someone with 61°F air and expect the same result as 70°F air.
            if ambient is not None:
                ambient_delta = AMBIENT_REFERENCE_F - ambient
                if ambient_delta > 0:
                    # Base compensation
                    ambient_adj = ambient_delta * AMBIENT_COMPENSATION_PER_F
                    # Extra ramp for very cold rooms (<65°F)
                    if ambient < AMBIENT_COLD_THRESHOLD_F:
                        cold_extra = (AMBIENT_COLD_THRESHOLD_F - ambient) * AMBIENT_COLD_EXTRA_PER_F
                        ambient_adj += cold_extra
                    effective += round(ambient_adj)
                    self.log(
                        f"[{zone}] Ambient comp: room={ambient:.0f}°F "
                        f"adj=+{round(ambient_adj)} → effective={effective:+d}"
                    )

                # Hard floor: in a freezing room, never actively cool
                if ambient <= AMBIENT_FLOOR_TEMP_F:
                    effective = max(AMBIENT_FLOOR_SETTING, effective)
                    self.log(
                        f"[{zone}] Cold room floor: room={ambient:.0f}°F "
                        f"≤ {AMBIENT_FLOOR_TEMP_F}°F, clamped to >={AMBIENT_FLOOR_SETTING:+d}"
                    )

            # Never heat — user runs warm, heating always makes them too hot.
            # Ambient compensation can bring us to neutral (0) but never above.
            effective = min(MAX_AMBIENT_COMPENSATED_SETTING, effective)

            # Override floor: after a manual override, never go colder than
            # what the user chose. They overrode because they were uncomfortable.
            override_floor = state.get("override_floor")
            if override_floor is not None:
                if effective < override_floor:
                    self.log(
                        f"[{zone}] Override floor: computed {effective:+d} "
                        f"< user's {override_floor:+d}, using floor"
                    )
                    effective = override_floor

            # Clamp to topper range
            effective = max(-10, min(10, effective))

            # Read current setting
            preset_entity = self._get_preset_entity(zone, phase)
            current_setting = self._read_entity(preset_entity)
            if current_setting is None:
                self.log(f"[{zone}] Can't read {phase} preset", level="WARNING")
                continue
            current_setting = int(current_setting)

            # 5. Pre-wake thermal ramp: gradually transition toward wake temp
            new_setting = effective
            action = "preference"

            if phase == "wake":
                # Linearly interpolate from REM baseline toward wake baseline
                # over the PRE_WAKE_RAMP_MIN period for a gentle transition
                progress = self._read_entity(ZONE_SENSORS[zone]["run_progress"])
                if progress is not None and progress > 0:
                    elapsed_total = (now - datetime.fromisoformat(
                        state["bedtime_ts"])).total_seconds() / 60
                    total_min = elapsed_total / (progress / 100.0)
                    remaining = total_min - elapsed_total
                    ramp_progress = max(0, min(1, 1 - remaining / PRE_WAKE_RAMP_MIN))
                    rem_start = ml_recs.get("rem", USER_BASELINE.get("rem", -5))
                    wake_target = effective  # already computed for wake phase
                    ramp_setting = round(
                        rem_start * (1 - ramp_progress) +
                        wake_target * ramp_progress
                    )
                    new_setting = ramp_setting
                    action = f"prewake_ramp({ramp_progress:.0%})"

            # 6. Body sensor extreme check — only override preferences
            #    when sensor data is clearly actionable

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

            elapsed = (now - datetime.fromisoformat(state["bedtime_ts"])).total_seconds() / 60
            amb_s = f"{ambient:.0f}" if ambient is not None else "?"
            self.log(
                f"[{zone}] t+{elapsed:.0f}m {phase} | "
                f"body={body_avg:.1f}°F amb={amb_s}°F | "
                f"base={baseline:+d} learn={learned_adj:+d} "
                f"eff={effective:+d} | "
                f"setting: {current_setting:+d}→{new_setting:+d} "
                f"({action})")

            # 6. Apply if changed — with deadband and cooldown
            #    Don't change unless diff >= SETTING_DEADBAND (prevents thrashing)
            #    Don't change within CHANGE_COOLDOWN_SEC of last change (thermal lag)
            #    Hot safety bypasses both — it's a safety mechanism
            diff = abs(new_setting - current_setting)
            is_safety = (action == "hot_safety")
            last_change = state.get("last_setting_change_ts")
            cooldown_ok = (last_change is None or
                          (now - datetime.fromisoformat(last_change)
                           ).total_seconds() >= CHANGE_COOLDOWN_SEC)
            should_apply = (new_setting != current_setting and
                           (is_safety or (diff >= SETTING_DEADBAND and cooldown_ok)))
            if should_apply:
                try:
                    self.call_service("number/set_value", entity_id=preset_entity, value=new_setting)
                    state["last_settings_pushed"][phase] = new_setting
                    # L2 is shared by deep and rem — keep both in sync
                    # to prevent phantom override detection on phase transition
                    if phase in ("deep", "rem"):
                        state["last_settings_pushed"]["deep"] = new_setting
                        state["last_settings_pushed"]["rem"] = new_setting
                    state["last_setting_change_ts"] = now.isoformat()
                    self.log(f"[{zone}] SET {phase} = {new_setting:+d}")
                except Exception as svc_err:
                    self.log(f"[{zone}] FAILED to set {preset_entity}: {svc_err}", level="ERROR")
                self._log_to_postgres(
                    zone, phase, elapsed, sensors, ambient,
                    new_setting, effective, baseline, learned_adj, action)
            else:
                if new_setting != current_setting and not cooldown_ok:
                    action = "cooldown"
                elif new_setting != current_setting and diff < SETTING_DEADBAND:
                    action = "deadband"
                self._log_to_postgres(
                    zone, phase, elapsed, sensors, ambient,
                    current_setting, effective, baseline, learned_adj, action
                    if action in ("cooldown", "deadband") else "hold")

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

        # Flush any pending debounced override before end-of-night
        pending = state.get("pending_override")
        if pending:
            state["override_history"].append(pending)
            self.log(f"[{zone}] Flushed pending override at end of night: {pending['phase']}={pending['actual']:+d}")
            state["pending_override"] = None
            state["pending_override_ts"] = None

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
            # Persist override history to durable storage before clearing
            self._persist_override_history(zone, bedtime, overrides)

        # Feed the ML learner with tonight's data
        room_temp = self._read_room_temp()
        # Enrich overrides with room temp for the learner
        for ov in overrides:
            if "room_temp_f" not in ov:
                ov["room_temp_f"] = room_temp
        # Read health data from HA entities (populated by SleepSync)
        avg_hr = self._read_entity("input_number.apple_health_hr_avg")
        avg_hrv = self._read_entity("input_number.apple_health_hrv")
        # Read latest sleep rating if available
        user_rating = self._read_latest_sleep_rating()
        try:
            night_record = NightRecord(
                night_date=bedtime.date().isoformat(),
                zone=zone,
                duration_hours=duration,
                avg_body_f=avg_t,
                room_temp_f=room_temp,
                override_count=len(overrides),
                overrides=overrides,
                final_settings=dict(state.get("last_settings_pushed", {})),
                manual_mode=state.get("manual_mode", False),
                avg_hr=avg_hr,
                avg_hrv=avg_hrv,
                user_rating=user_rating,
            )
            self.learner.update_after_night(night_record)
            self.learner.save()
            summary = self.learner.get_model_summary(zone)
            self.log(
                f"[{zone}] ML learner updated: {summary.get('nights', 0)} nights, "
                f"phases: {list(summary.get('phases', {}).keys())}"
            )
        except Exception as ml_err:
            self.log(f"[{zone}] ML learner update failed (non-fatal): {ml_err}", level="ERROR")

        # Log nightly summary to PostgreSQL
        self._log_nightly_summary(zone, state, bedtime, now, duration,
                                  avg_t, overrides)

        # Reset presets to ML-recommended baseline for next night.
        # Map 4 phases back to 3 topper presets: bedtime→L1, deep/rem→L2, wake→L3
        ml_recs = self.learner.get_recommendations(zone, room_temp_f=room_temp)
        preset_phase_map = {
            "bedtime": "bedtime",
            "sleep": "deep",
            "wake": "wake",
        }
        for preset_name, entity in ZONE_PRESETS[zone].items():
            phase_key = preset_phase_map.get(preset_name, preset_name)
            reset_val = ml_recs.get(phase_key, USER_BASELINE.get(phase_key, -6))
            reset_val = max(-10, min(10, reset_val))
            try:
                self.call_service("number/set_value", entity_id=entity, value=reset_val)
            except Exception as exc:
                self.log(f"[{zone}] Failed to reset {preset_name} to {reset_val}: {exc}", level="ERROR")
        self.log(f"[{zone}] Presets reset to ML recommendations: {ml_recs}")

        # Send morning sleep rating notification
        self._send_sleep_rating_request(zone, duration, len(overrides), avg_t)

        state["bedtime_ts"] = None
        state["last_settings_pushed"] = {}
        state["override_history"] = []
        state["bed_empty_since"] = None
        state["last_setting_change_ts"] = None
        state["override_freeze_until"] = None
        self._save_state()

    def _persist_override_history(self, zone, bedtime, overrides):
        """Append override data to a durable file for future ML retraining.

        This prevents the nightly wipe from losing training signal.
        """
        history_file = STATE_DIR / "override_history.jsonl"
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(history_file, "a") as f:
                for ov in overrides:
                    record = {
                        "night_date": bedtime.date().isoformat(),
                        "zone": zone,
                        **ov,
                    }
                    f.write(json.dumps(record, default=str) + "\n")
            self.log(f"[{zone}] Persisted {len(overrides)} overrides to {history_file}")
        except OSError as e:
            self.log(f"[{zone}] Failed to persist overrides: {e}", level="WARNING")

    def _get_preset_entity(self, zone, phase):
        """Map the 4-phase system to the topper's 3 native presets.

        The topper has L1 (bedtime), L2 (sleep), L3 (wake).
        Our 4 phases map as: bedtime→L1, deep→L2, rem→L2, wake→L3.
        """
        if phase == "bedtime":
            return ZONE_PRESETS[zone]["bedtime"]
        elif phase in ("deep", "rem"):
            return ZONE_PRESETS[zone]["sleep"]
        elif phase == "wake":
            return ZONE_PRESETS[zone]["wake"]
        return ZONE_PRESETS[zone].get("sleep")

    # ── Override Detection ───────────────────────────────────────────

    def _detect_override(self, zone, state, current_phase):
        """Check if user manually changed the active preset."""
        last_pushed = state["last_settings_pushed"].get(current_phase)
        if last_pushed is None:
            return None

        preset_entity = self._get_preset_entity(zone, current_phase)
        actual = self._read_entity(preset_entity)
        if actual is None:
            return None
        actual = int(actual)

        if actual != last_pushed:
            sensors = self._read_sensors(zone)
            body = sensors.get("body_avg")
            room_temp = self._read_room_temp()
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
                "room_temp_f": room_temp,
                "blower_pct": sensors.get("blower_pct"),
                "heater_head_pct": sensors.get("heater_head_pct"),
                "heater_foot_pct": sensors.get("heater_foot_pct"),
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
        # For L2 ("sleep" preset), check against both deep and rem tracking
        # since the controller stores pushed values under those keys
        if phase == "sleep":
            pushed_deep = state["last_settings_pushed"].get("deep")
            pushed_rem = state["last_settings_pushed"].get("rem")
            pushed = pushed_deep  # Either works — they're kept in sync
        else:
            pushed = state["last_settings_pushed"].get(phase)
        try:
            new_val = int(float(new))
        except (ValueError, TypeError):
            return
        if pushed is not None and new_val == pushed:
            return  # Our own write

        # Map preset key to active controller phase for comparison.
        # Presets: bedtime→L1, sleep→L2, wake→L3
        # Controller phases: bedtime, deep, rem, wake
        # L2 ("sleep") is active during both deep and rem phases.
        active_phase = self._get_active_phase(zone, state)
        phase_matches = False
        if phase == "bedtime" and active_phase == "bedtime":
            phase_matches = True
        elif phase == "sleep" and active_phase in ("deep", "rem"):
            phase_matches = True
        elif phase == "wake" and active_phase == "wake":
            phase_matches = True
        if not phase_matches:
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

    # ── Morning Sleep Rating ────────────────────────────────────────

    def _send_sleep_rating_request(self, zone, duration_h, override_count, avg_body):
        """Send an actionable iOS notification asking for a sleep quality rating.

        The user taps one of three options. The response is stored in
        sleep_ratings.jsonl for the ML learner to use as a quality signal.
        """
        try:
            self.call_service(
                NOTIFY_SERVICE,
                title="Good morning! ☀️",
                message=f"How did you sleep? ({duration_h:.1f}h, {override_count} adjustments)",
                data={
                    "actions": [
                        {"action": "SLEEP_RATING_1", "title": "😫 Terrible"},
                        {"action": "SLEEP_RATING_3", "title": "😐 Okay"},
                        {"action": "SLEEP_RATING_5", "title": "😴 Great"},
                    ],
                    "push": {"interruption-level": "passive"},
                },
            )
            self.log(f"[{zone}] Sent sleep rating notification")
        except Exception as e:
            self.log(f"[{zone}] Rating notification failed: {e}", level="WARNING")

    def _on_sleep_rating(self, event_name, data, kwargs):
        """Handle sleep rating notification response."""
        action = data.get("action", "")
        if not action.startswith("SLEEP_RATING_"):
            return
        try:
            rating = int(action.split("_")[-1])
        except ValueError:
            return
        self.log(f"Sleep rating received: {rating}/5")
        rating_file = STATE_DIR / "sleep_ratings.jsonl"
        try:
            with open(rating_file, "a") as f:
                f.write(json.dumps({
                    "date": datetime.now().isoformat(),
                    "rating": rating,
                }) + "\n")
        except OSError:
            self.log("Failed to save sleep rating", level="WARNING")

    def _read_latest_sleep_rating(self):
        """Read the most recent sleep rating from sleep_ratings.jsonl.

        Returns the rating (1-5) if one was recorded today, else None.
        """
        rating_file = STATE_DIR / "sleep_ratings.jsonl"
        if not rating_file.exists():
            return None
        try:
            last_line = None
            with open(rating_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last_line = line
            if last_line:
                record = json.loads(last_line)
                # Only use rating if from today
                rating_date = record.get("date", "")[:10]
                today = datetime.now().strftime("%Y-%m-%d")
                if rating_date == today:
                    return record.get("rating")
        except (json.JSONDecodeError, OSError):
            pass
        return None

    # ── Sensor Reading ───────────────────────────────────────────────

    # Sanity bounds — values outside these ranges indicate sensor malfunction
    ROOM_TEMP_MIN_F = 40.0
    ROOM_TEMP_MAX_F = 110.0
    BODY_TEMP_MIN_F = 50.0
    BODY_TEMP_MAX_F = 120.0

    def _read_entity(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(state)
        except (ValueError, TypeError):
            self.log(f"Entity {entity_id} not numeric: {state!r}", level="WARNING")
            return None

    def _read_room_temp(self):
        """Read room temperature with sanity bounds."""
        val = self._read_entity(self.room_temp_entity)
        if val is not None and not (self.ROOM_TEMP_MIN_F <= val <= self.ROOM_TEMP_MAX_F):
            self.log(f"Room temp {val:.1f}F outside sane range [{self.ROOM_TEMP_MIN_F}-{self.ROOM_TEMP_MAX_F}] — ignoring",
                     level="WARNING")
            return None
        return val

    def terminate(self):
        """Called by AppDaemon on clean shutdown — save state."""
        self.log("Controller terminating — saving state")
        self._save_state()

    def _read_sensors(self, zone):
        sensors = {}
        for key, entity_id in ZONE_SENSORS[zone].items():
            val = self._read_entity(entity_id)
            # Sanity check body sensor readings
            if key.startswith("body_") and val is not None:
                if not (self.BODY_TEMP_MIN_F <= val <= self.BODY_TEMP_MAX_F):
                    self.log(f"[{zone}] {key}={val:.1f}F outside sane range — treating as unavailable",
                             level="WARNING")
                    val = None
            sensors[key] = val

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

    # ── State Persistence ────────────────────────────────────────────

    def _pg_connect(self):
        """Lazy PostgreSQL connection with auto-reconnect."""
        if self._pg_conn is not None:
            try:
                self._pg_conn.cursor().execute("SELECT 1")
                return self._pg_conn
            except Exception:
                try:
                    self._pg_conn.close()
                except Exception:
                    pass
                self._pg_conn = None

        try:
            import psycopg2
            self._pg_conn = psycopg2.connect(
                host=self.pg_host, port=POSTGRES_PORT,
                dbname=POSTGRES_DB, user=POSTGRES_USER,
                password=POSTGRES_PASS, connect_timeout=5)
            self._pg_conn.autocommit = True
            return self._pg_conn
        except Exception as e:
            self.log(f"PostgreSQL connect failed: {e}", level="WARNING")
            self._pg_conn = None
            return None

    def _log_to_postgres(self, zone, phase, elapsed, sensors,
                         room_temp, setting, effective, baseline,
                         learned_adj, action, override_delta=None):
        """Write a control-loop reading to PostgreSQL."""
        conn = self._pg_connect()
        if conn is None:
            return
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO controller_readings
                   (zone, phase, elapsed_min, body_right_f, body_center_f,
                    body_left_f, body_avg_f, ambient_f, room_temp_f,
                    setting, effective, baseline, learned_adj, action,
                    override_delta)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (zone, phase, elapsed,
                 sensors.get("body_right"), sensors.get("body_center"),
                 sensors.get("body_left"), sensors.get("body_avg"),
                 sensors.get("ambient"), room_temp,
                 setting, effective, baseline, learned_adj, action,
                 override_delta))
        except Exception as e:
            self.log(f"PostgreSQL reading write failed: {e}", level="WARNING")
            self._pg_conn = None

    def _log_nightly_summary(self, zone, state, bedtime, wake, duration,
                             avg_body, overrides):
        """Write an end-of-night summary row to PostgreSQL."""
        conn = self._pg_connect()
        if conn is None:
            return
        try:
            night_date = bedtime.date()
            pushed = state.get("last_settings_pushed", {})
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO nightly_summary
                   (night_date, bedtime_ts, wake_ts, duration_hours,
                    bedtime_setting, sleep_setting, wake_setting,
                    avg_body_f, override_count, manual_mode)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (night_date) DO UPDATE SET
                    wake_ts=EXCLUDED.wake_ts,
                    duration_hours=EXCLUDED.duration_hours,
                    bedtime_setting=EXCLUDED.bedtime_setting,
                    sleep_setting=EXCLUDED.sleep_setting,
                    wake_setting=EXCLUDED.wake_setting,
                    avg_body_f=EXCLUDED.avg_body_f,
                    override_count=EXCLUDED.override_count,
                    manual_mode=EXCLUDED.manual_mode""",
                (night_date, bedtime, wake, duration,
                 pushed.get("bedtime"), pushed.get("deep", pushed.get("sleep")),
                 pushed.get("wake"), avg_body,
                 len(overrides), state.get("manual_mode", False)))
            self.log(f"[{zone}] Nightly summary saved to PostgreSQL")
        except Exception as e:
            self.log(f"PostgreSQL nightly summary failed: {e}", level="WARNING")
            self._pg_conn = None

    # ── State Persistence (file-based) ───────────────────────────────

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
                fresh = self._fresh_zone_state()
                saved = zdata.get("state", {})
                # Merge saved values into fresh template (handles new fields)
                fresh.update({k: v for k, v in saved.items() if k in fresh})
                self.zone_state[zone] = fresh
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
            old = self.learned[zone].get(phase, 0.0)
            if not isinstance(old, float):
                old = float(old)
            new = old * (1 - alpha) + avg_delta * alpha
            # Store as float to avoid rounding trap; round only when applying
            self.learned[zone][phase] = round(new, 2)
            self.log(
                f"[{zone}] LEARNING: {phase} adj {old:+.1f} → {new:+.2f} "
                f"(applied as {round(new):+d}) "
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
            # Migrate old "sleep" key → "deep" (from 3-phase to 4-phase)
            migrated = False
            for zone in list(self.learned.keys()):
                if "sleep" in self.learned[zone] and "deep" not in self.learned[zone]:
                    old_val = self.learned[zone].pop("sleep")
                    self.learned[zone]["deep"] = old_val
                    self.learned[zone].setdefault("rem", old_val * 0.7)
                    migrated = True
                    self.log(f"[{zone}] Migrated learned 'sleep'→'deep' ({old_val:+.2f})")
            if migrated:
                self._save_learned()
            self.log(f"Loaded learned preferences: {self.learned}")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self.log(f"Learned prefs corrupted: {e}", level="ERROR")
            self.learned = {}
