"""
Sleep Controller v5 — Left side RC-off blower-proxy control.
============================================================

Architecture:
  This controller disables firmware Responsive Cooling on the LEFT side and
  uses the empirically measured RC-off L1 ladder as a coarse blower-proxy
  model. We still only write the L1 entity, but we reason in blower-proxy %
  space and map back onto the closest allowed L1 step.

  Important nuance: RC-off does NOT guarantee that the firmware will hold a
  literal fixed blower output for a given L1 forever. The topper may still
  modulate its real blower internally, so logged proxy values are guidance,
  not a promised hardware command.

Measured RC-off ladder:
  -10→100, -9→87, -8→75, -7→65, -6→50,
   -5→41,  -4→33, -3→26, -2→20, -1→10, 0→0

Key principles:
  - Left side Responsive Cooling stays OFF
  - Right side remains passive-only telemetry
  - Use the bedroom Aqara thermometer by default for room temp
  - Preserve aggressive cooling for a hot sleeper
  - Manual overrides are sacred: freeze for 1 hour, then keep an all-night floor
  - Learn from override residuals in blower space, not firmware setpoint space
"""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import hassapi as hass

# ── Sleep Phase Science ──────────────────────────────────────────────────
#
# Sleep cycles in ~90-minute waves:
#   Light (N1/N2) → Deep (N3) → REM → Light → Deep → REM → ...
#
# Thermoregulation by phase:
#   Deep sleep: body temp at nadir, thermoregulation ACTIVE → cooling helps
#   REM sleep: thermoregulation IMPAIRED → overcooling causes waking
#   Light/transitions: normal regulation → follow preferences
#
# Ideal cooling trajectory:
#   Cycle 1 (0-90 min): aggressive cool → body needs help dropping core temp
#   Cycle 2 (90-180 min): moderate cool → deep sleep dominant, body is cold
#   Cycle 3-4 (180-360 min): light cool → REM increases, overcooling wakes
#   Cycle 5+ (360+ min): minimal cool → approaching wake, body warming up

# Settings per cycle position (-10 to +10 scale, capped at 0).
# Under RC-off these act as fixed blower-proxy baselines, so stay colder than v4.
CYCLE_SETTINGS = {
    1: -10,  # Start cold and only ease off gradually
    2: -9,
    3: -8,
    4: -7,
    5: -6,
    6: -5,
}
CYCLE_DURATION_MIN = 90

# ── Control Model ────────────────────────────────────────────────────
CONTROLLER_VERSION = "v5_rc_off"
ENABLE_LEARNING = True
MAX_SETTING = 0

L1_TO_BLOWER_PCT = {
    -10: 100,
    -9: 87,
    -8: 75,
    -7: 65,
    -6: 50,
    -5: 41,
    -4: 33,
    -3: 26,
    -2: 20,
    -1: 10,
    0: 0,
}

# Room compensation happens in blower space now.
ROOM_BLOWER_REFERENCE_F = 68.0
ROOM_BLOWER_COLD_COMP_PER_F = 4.0
ROOM_BLOWER_COLD_THRESHOLD_F = 63.0
ROOM_BLOWER_COLD_EXTRA_PER_F = 3.0
ROOM_BLOWER_HOT_COMP_PER_F = 4.0

# Body handling stays conservative: only act on clear hot extremes.
BODY_HOT_THRESHOLD_F = 85.0
BODY_HOT_STREAK_COUNT = 2
BODY_TEMP_MIN_F = 55.0
BODY_TEMP_MAX_F = 110.0

# Override handling
OVERRIDE_FREEZE_MIN = 60            # Freeze controller for 1 hour after manual change
MIN_CHANGE_INTERVAL_SEC = 1800      # Don't change L1 more than once per 30 min
KILL_SWITCH_CHANGES = 3             # 3 manual changes in this window...
KILL_SWITCH_WINDOW_SEC = 300        # ...within 5 min = manual mode for night

# Occupancy — body sensors read 67-70°F empty, 80-89°F occupied
BODY_OCCUPIED_THRESHOLD_F = 74.0    # Getting-in threshold (ramp from 70→80 takes a few min)
BODY_EMPTY_THRESHOLD_F = 72.0       # Below this, start the empty-bed timer
BODY_EMPTY_TIMEOUT_MIN = 20         # Must stay below empty threshold for this long
AUTO_RESTART_DEBOUNCE_SEC = 300     # Wait 5 min before retrying a restart

# Entities
E_BEDTIME_TEMP = "number.smart_topper_left_side_bedtime_temperature"
E_RUNNING = "switch.smart_topper_left_side_running"
E_RESPONSIVE_COOLING = "switch.smart_topper_left_side_responsive_cooling"
E_PROFILE_3LEVEL = "switch.smart_topper_left_side_3_level_mode"
E_BODY_CENTER = "sensor.smart_topper_left_side_body_sensor_center"
E_BODY_LEFT = "sensor.smart_topper_left_side_body_sensor_left"
E_BODY_RIGHT = "sensor.smart_topper_left_side_body_sensor_right"
E_SLEEP_STAGE = "input_text.apple_health_sleep_stage"
E_SLEEP_MODE = "input_boolean.sleep_mode"
DEFAULT_ROOM_TEMP_ENTITY = "sensor.bedroom_temperature_sensor_temperature"

ZONE_ENTITY_IDS = {
    "left": {
        "bedtime": E_BEDTIME_TEMP,
        "body_center": E_BODY_CENTER,
        "body_left": E_BODY_LEFT,
        "body_right": E_BODY_RIGHT,
        "ambient": "sensor.smart_topper_left_side_ambient_temperature",
        "setpoint": "sensor.smart_topper_left_side_temperature_setpoint",
        "blower_pct": "sensor.smart_topper_left_side_blower_output",
    },
    "right": {
        "bedtime": "number.smart_topper_right_side_bedtime_temperature",
        "body_center": "sensor.smart_topper_right_side_body_sensor_center",
        "body_left": "sensor.smart_topper_right_side_body_sensor_left",
        "body_right": "sensor.smart_topper_right_side_body_sensor_right",
        "ambient": "sensor.smart_topper_right_side_ambient_temperature",
        "setpoint": "sensor.smart_topper_right_side_temperature_setpoint",
        "blower_pct": "sensor.smart_topper_right_side_blower_output",
    },
}

# Postgres — host is configurable via apps.yaml "postgres_host" arg
PG_HOST_DEFAULT = "192.168.0.3"
PG_PORT = 5432
PG_DB = "sleepdata"
PG_USER = "sleepsync"
PG_PASS = "sleepsync_local"

BED_PRESENCE_ENTITIES = {
    "left_pressure": "sensor.bed_presence_2bcab8_left_pressure",
    "right_pressure": "sensor.bed_presence_2bcab8_right_pressure",
    "left_unoccupied_pressure": "number.bed_presence_2bcab8_left_unoccupied_pressure",
    "right_unoccupied_pressure": "number.bed_presence_2bcab8_right_unoccupied_pressure",
    "left_occupied_pressure": "number.bed_presence_2bcab8_left_occupied_pressure",
    "right_occupied_pressure": "number.bed_presence_2bcab8_right_occupied_pressure",
    "left_trigger_pressure": "number.bed_presence_2bcab8_left_trigger_pressure",
    "right_trigger_pressure": "number.bed_presence_2bcab8_right_trigger_pressure",
    "occupied_left": "binary_sensor.bed_presence_2bcab8_bed_occupied_left",
    "occupied_right": "binary_sensor.bed_presence_2bcab8_bed_occupied_right",
    "occupied_either": "binary_sensor.bed_presence_2bcab8_bed_occupied_either",
    "occupied_both": "binary_sensor.bed_presence_2bcab8_bed_occupied_both",
}

# State file (backup — primary state is in-memory)
_container = Path("/config/apps")
_host = Path("/addon_configs/a0d7b954_appdaemon/apps")
STATE_DIR = _container if _container.exists() else _host
STATE_FILE = STATE_DIR / "controller_state_v5_rc_off.json"
LEARNED_FILE = STATE_DIR / "learned_blower_adjustments_v5.json"

# Learning parameters
LEARNING_LOOKBACK_NIGHTS = 14       # Analyze this many recent nights
LEARNING_MAX_BLOWER_ADJ = 30        # Max learned blower adjustment per cycle (±30%)
LEARNING_DECAY = 0.7                # Weight: recent overrides count more


class SleepControllerV5(hass.Hass):

    def initialize(self):
        self.log("=" * 60)
        self.log("Sleep Controller v5 initializing")
        self._room_temp_entity = getattr(self, "args", {}).get(
            "room_temp_entity",
            DEFAULT_ROOM_TEMP_ENTITY,
        )
        self._pg_host = getattr(self, "args", {}).get(
            "postgres_host", PG_HOST_DEFAULT,
        )

        self._state = {
            "sleep_start": None,        # When sleep began (ISO string)
            "sleep_start_epoch": None,  # Epoch form for DST-safe elapsed tracking
            "last_setting": None,       # Current L1 value
            "last_change_ts": None,     # When we last changed L1
            "last_restart_ts": None,    # Last time we auto-restarted the topper
            "last_target_blower_pct": None,
            "override_freeze_until": None,
            "override_floor": None,     # User's last manual value (floor)
            "override_floor_blower_pct": None,
            "manual_mode": False,       # Kill switch triggered
            "recent_changes": [],       # Timestamps of recent manual changes
            "override_count": 0,        # Total overrides this night
            "body_below_since": None,   # When body first dropped below BODY_EMPTY_THRESHOLD_F
            "hot_streak": 0,
            "current_cycle_num": None,
        }
        self._load_state()
        self._pg_conn = None
        self._learned = self._load_learned()

        # Force left-side responsive cooling OFF — v5 owns the outer proxy model.
        self.call_service("switch/turn_off", entity_id=E_RESPONSIVE_COOLING)

        # Run control loop every 5 minutes
        self.run_every(self._control_loop, "now", 300)

        # Listen for sleep mode changes
        self.listen_state(self._on_sleep_mode, E_SLEEP_MODE)

        # Listen for manual setting changes (override detection)
        self.listen_state(self._on_setting_change, E_BEDTIME_TEMP)
        self.listen_state(
            self._on_right_setting_change,
            ZONE_ENTITY_IDS["right"]["bedtime"],
        )

        # Gap 5: If sleep_mode is already ON (mid-night restart), resume
        self._check_midnight_restart()

        self.log("Controller v5 ready — left side in RC-off blower-proxy mode")
        self.log(f"  Cycles: {CYCLE_SETTINGS}")
        self.log(f"  Learned: {self._learned}")
        self.log(f"  Room temp: {self._get_room_temp_entity()}")
        self.log(f"  Max setting: {MAX_SETTING}")
        self.log("=" * 60)

    # ── Main Control Loop ────────────────────────────────────────────

    def _control_loop(self, kwargs):
        now = datetime.now()

        # Not sleeping? Nothing to do.
        if not self._is_sleeping():
            return

        # Responsive cooling watchdog — keep the left side in RC-off mode.
        self._ensure_responsive_cooling_off()

        # ── Always read sensors (telemetry must never have gaps) ──────
        room_temp = self._read_temperature(self._get_room_temp_entity())
        if room_temp is not None and not (40.0 <= room_temp <= 100.0):
            self.log(f"Room temp {room_temp}°F out of range — ignoring", level="WARNING")
            room_temp = None
        sleep_stage = self._read_str(E_SLEEP_STAGE)
        left_snapshot = self._read_zone_snapshot("left")
        bed_presence = self._read_bed_presence_snapshot()
        body_center = left_snapshot["body_center"]
        body_left = left_snapshot["body_left"]
        body_right = left_snapshot["body_right"]
        current = left_snapshot["setting"]
        actual_blower_pct = left_snapshot["blower_pct"]
        setpoint = left_snapshot["setpoint"]
        ambient = left_snapshot["ambient"]

        body_vals = [v for v in (body_left, body_center, body_right) if v is not None]
        body_max = max(body_vals) if body_vals else body_center
        body_avg = left_snapshot["body_avg"]

        # Occupancy detection
        if not self._check_occupancy(body_max, now):
            elapsed_min = self._elapsed_min()
            if elapsed_min > 0:
                self._log_to_postgres(
                    elapsed_min, room_temp, sleep_stage, body_center,
                    self._state.get("last_setting", -10),
                    body_avg=body_avg, body_left=body_left, body_right=body_right,
                    action="empty_bed", ambient=ambient, setpoint=setpoint,
                    blower_pct=actual_blower_pct, responsive_cooling_on=False,
                    bed_presence=bed_presence,
                )
                self._log_passive_zone_snapshot(
                    "right", elapsed_min, room_temp, sleep_stage, bed_presence=bed_presence
                )
            return

        # Built-in schedules can shut the topper off mid-night while sleep_mode
        # is still ON. If body sensors still show occupied, force it back on.
        self._ensure_topper_running(now)

        sleep_start = self._state.get("sleep_start")
        if not sleep_start:
            return

        elapsed_min = self._elapsed_min()
        if current is None:
            self.log("Bedtime temp entity unavailable — skipping", level="WARNING")
            return

        # ── Always compute the ideal setting (for logging even if blocked) ──
        plan = self._compute_setting(
            elapsed_min,
            room_temp,
            sleep_stage,
            body_avg=body_avg,
            current_setting=current,
        )
        setting = plan["setting"]
        target_blower_pct = plan["target_blower_pct"]
        base_setting = plan["base_setting"]
        base_blower_pct = plan["base_blower_pct"]
        cycle_num = plan["cycle_num"]
        room_temp_comp = plan["room_temp_comp"]
        learned_adj_pct = plan["learned_adj_pct"]
        data_source = plan["data_source"]

        # Apply override floor for the rest of the night.
        floor = self._state.get("override_floor")
        if floor is not None and setting < floor:
            self.log(f"Override floor: computed {setting:+d} < floor {floor:+d}, using floor")
            setting = floor
            target_blower_pct = self._l1_to_blower_pct(setting)

        # ── Determine if we can change the setting ────────────────────
        changed = int(current) != setting
        blocked = False
        action = "hold"

        if self._state["manual_mode"]:
            blocked = True
            action = "manual_hold"
        else:
            freeze_until = self._state.get("override_freeze_until")
            if freeze_until:
                freeze_dt = datetime.fromisoformat(freeze_until)
                if now < freeze_dt:
                    blocked = True
                    action = "freeze_hold"
                else:
                    self._state["override_freeze_until"] = None
                    self._save_state()
        if not blocked:
            last_change = self._state.get("last_change_ts")
            if last_change:
                since_change = (now - datetime.fromisoformat(last_change)).total_seconds()
                if since_change < MIN_CHANGE_INTERVAL_SEC:
                    blocked = True
                    action = "rate_hold"

        logged_blower_pct = actual_blower_pct

        # Apply setting change if allowed and needed
        if changed and not blocked:
            self.log(
                f"Cycle {cycle_num}, "
                f"elapsed={elapsed_min:.0f}m, room={room_temp}°F, "
                f"stage={sleep_stage}, src={data_source}, "
                f"proxy={int(current):+d}/{self._l1_to_blower_pct(int(current))}% → "
                f"{setting:+d}/{target_blower_pct}%"
            )
            self._set_l1(setting)
            # Prefer a fresh HA read on set rows; the pre-change sample can be stale.
            logged_blower_pct = None
            self._state["last_change_ts"] = now.isoformat()
            self._state["last_target_blower_pct"] = target_blower_pct
            self._save_state()
            action = "hot_safety" if plan["hot_safety"] else "set"

        # ── Always log to Postgres — no telemetry gaps ────────────────
        self._log_to_postgres(
            elapsed_min, room_temp, sleep_stage, body_center, setting,
            cycle_num=cycle_num, room_temp_comp=room_temp_comp,
            data_source=data_source, override_floor=floor,
            body_avg=body_avg, body_left=body_left, body_right=body_right,
            action=action, ambient=ambient, setpoint=setpoint,
            effective=setting if changed and not blocked else int(current),
            baseline=base_setting, learned_adj=learned_adj_pct,
            blower_pct=logged_blower_pct, target_blower_pct=target_blower_pct,
            base_blower_pct=base_blower_pct, responsive_cooling_on=False,
            bed_presence=bed_presence,
        )
        self._log_passive_zone_snapshot(
            "right", elapsed_min, room_temp, sleep_stage, bed_presence=bed_presence
        )

    def _compute_setting(self, elapsed_min, room_temp, sleep_stage, body_avg=None, current_setting=None):
        """Compute target L1 from cycle/stage, room compensation, and learned blower residuals."""
        cycle_num = self._get_cycle_num(elapsed_min)
        self._state["current_cycle_num"] = cycle_num

        data_source = "time_cycle"
        base_setting = CYCLE_SETTINGS.get(cycle_num, CYCLE_SETTINGS[max(CYCLE_SETTINGS.keys())])
        if sleep_stage and sleep_stage not in ("unknown", ""):
            staged_setting = self._setting_for_stage(sleep_stage)
            if staged_setting is not None:
                base_setting = staged_setting
                data_source = "stage"

        base_blower_pct = self._l1_to_blower_pct(base_setting)
        target_blower_pct = base_blower_pct

        learned_adj_pct = 0
        if ENABLE_LEARNING:
            learned_adj_pct = int(self._learned.get(str(cycle_num), 0))
            learned_adj_pct = max(
                -LEARNING_MAX_BLOWER_ADJ,
                min(LEARNING_MAX_BLOWER_ADJ, learned_adj_pct),
            )
            if learned_adj_pct:
                target_blower_pct += learned_adj_pct
                data_source += "+learned"

        room_temp_comp = self._room_temp_to_blower_comp(room_temp)
        if room_temp_comp:
            target_blower_pct += room_temp_comp
            data_source += "+room"

        hot_safety = False
        if body_avg is not None and body_avg > BODY_HOT_THRESHOLD_F:
            self._state["hot_streak"] = self._state.get("hot_streak", 0) + 1
            if self._state["hot_streak"] >= BODY_HOT_STREAK_COUNT:
                safety_from = current_setting if current_setting is not None else base_setting
                safety_blower_pct = self._l1_to_blower_pct(self._next_colder_setting(safety_from))
                if safety_blower_pct > target_blower_pct:
                    target_blower_pct = safety_blower_pct
                    hot_safety = True
                    data_source += "+hot"
        else:
            self._state["hot_streak"] = 0

        target_blower_pct = max(0, min(100, round(target_blower_pct)))
        setting = self._blower_pct_to_l1(target_blower_pct)
        snapped_blower_pct = self._l1_to_blower_pct(setting)

        return {
            "setting": setting,
            "target_blower_pct": snapped_blower_pct,
            "base_setting": base_setting,
            "base_blower_pct": base_blower_pct,
            "cycle_num": cycle_num,
            "room_temp_comp": room_temp_comp,
            "learned_adj_pct": learned_adj_pct,
            "data_source": data_source,
            "hot_safety": hot_safety,
        }

    def _setting_for_stage(self, stage):
        """Map Apple Health sleep stage to an RC-off baseline setting."""
        stage = stage.lower().strip()
        return {
            "deep": -10,
            "core": -8,
            "rem": -6,
            "awake": -5,
            "inbed": -9,
        }.get(stage)

    def _get_cycle_num(self, elapsed_min):
        """Get cycle number (1-based) from elapsed minutes."""
        return min(max(1, int(elapsed_min / CYCLE_DURATION_MIN) + 1), max(CYCLE_SETTINGS.keys()))

    # ── Sleep Mode ───────────────────────────────────────────────────

    def _on_sleep_mode(self, entity, attribute, old, new, kwargs):
        if new == "on" and old != "on":
            self.log("Sleep mode ON — starting night")
            now = datetime.now()
            self._state["sleep_start"] = now.isoformat()
            self._state["sleep_start_epoch"] = now.timestamp()
            self._state["manual_mode"] = False
            self._state["override_freeze_until"] = None
            self._state["override_floor"] = None
            self._state["override_floor_blower_pct"] = None
            self._state["recent_changes"] = []
            self._state["override_count"] = 0
            self._state["last_change_ts"] = None
            self._state["last_restart_ts"] = None
            self._state["body_below_since"] = None
            self._state["hot_streak"] = 0
            self._ensure_responsive_cooling_off()
            self.call_service(
                "input_text/set_value",
                entity_id=E_SLEEP_STAGE, value="unknown",
            )
            self._learned = self._learn_from_history()
            self._save_learned()
            self.log(f"  Learned adjustments: {self._learned}")

            room_temp = self._read_temperature(self._get_room_temp_entity())
            initial_snapshot = self._read_zone_snapshot("left")
            plan = self._compute_setting(
                0,
                room_temp,
                None,
                body_avg=initial_snapshot["body_avg"],
                current_setting=initial_snapshot["setting"],
            )
            initial_setting = plan["setting"]
            current_setting = initial_snapshot["setting"]
            if current_setting is not None and current_setting < initial_setting:
                initial_setting = int(current_setting)
                plan["target_blower_pct"] = self._l1_to_blower_pct(initial_setting)
            self.log(
                f"  Initial L1={initial_setting:+d} "
                f"(room={room_temp}°F, proxy={plan['target_blower_pct']}%)"
            )
            self._set_l1(initial_setting)
            self._state["last_setting"] = initial_setting
            self._state["last_target_blower_pct"] = plan["target_blower_pct"]
            self._state["initial_setting"] = initial_setting
            self._save_state()

        elif new == "off" and old == "on":
            self.log("Sleep mode OFF — ending night")
            self._end_night()
            self._state["sleep_start"] = None
            self._state["sleep_start_epoch"] = None
            self._state["manual_mode"] = False
            self._state["override_freeze_until"] = None
            self._state["override_floor"] = None
            self._state["override_floor_blower_pct"] = None
            self._state["last_restart_ts"] = None
            self._state["hot_streak"] = 0
            self._save_state()

    # ── Override Detection ───────────────────────────────────────────

    def _on_setting_change(self, entity, attribute, old, new, kwargs):
        """Detect manual setting changes and respect them."""
        if not self._is_sleeping():
            return

        try:
            old_val = int(float(old))
            new_val = int(float(new))
        except (ValueError, TypeError):
            return

        expected = self._state.get("last_setting")
        if expected is not None and new_val == expected:
            return

        now = datetime.now()
        self.log(f"MANUAL OVERRIDE: {old_val:+d} → {new_val:+d}")
        controller_value = expected if expected is not None else old_val
        snapshot = self._read_zone_snapshot("left")
        sleep_stage = self._read_str(E_SLEEP_STAGE)
        room_temp = self._read_temperature(self._get_room_temp_entity())

        cutoff = now.timestamp() - KILL_SWITCH_WINDOW_SEC
        self._state["recent_changes"] = [
            t for t in self._state["recent_changes"] if t > cutoff
        ]
        self._state["recent_changes"].append(now.timestamp())
        self._check_kill_switch()

        floor = min(new_val, MAX_SETTING)
        self._state["override_freeze_until"] = (
            now + timedelta(minutes=OVERRIDE_FREEZE_MIN)
        ).isoformat()
        self._state["override_floor"] = floor
        self._state["override_floor_blower_pct"] = self._l1_to_blower_pct(floor)
        self._state["last_setting"] = new_val
        self._state["last_target_blower_pct"] = self._l1_to_blower_pct(new_val)
        self._state["override_count"] = self._state.get("override_count", 0) + 1

        self._log_override(
            "left",
            new_val,
            controller_value=controller_value,
            delta=new_val - controller_value,
            room_temp=room_temp,
            sleep_stage=sleep_stage,
            snapshot=snapshot,
        )
        self._save_state()

    def _on_right_setting_change(self, entity, attribute, old, new, kwargs):
        """Persist passive right-side manual changes for future training data."""
        if not self._is_sleeping():
            return

        try:
            old_val = int(float(old))
            new_val = int(float(new))
        except (ValueError, TypeError):
            return

        if new_val == old_val:
            return

        self.log(f"RIGHT SIDE CHANGE: {old_val:+d} → {new_val:+d}")
        self._log_override(
            "right",
            new_val,
            controller_value=old_val,
            delta=new_val - old_val,
            room_temp=self._read_temperature(self._get_room_temp_entity()),
            sleep_stage=self._read_str(E_SLEEP_STAGE),
            snapshot=self._read_zone_snapshot("right"),
        )

    def _check_kill_switch(self):
        """If user makes KILL_SWITCH_CHANGES changes in KILL_SWITCH_WINDOW_SEC, go manual."""
        now_ts = datetime.now().timestamp()
        cutoff = now_ts - KILL_SWITCH_WINDOW_SEC
        recent = [t for t in self._state["recent_changes"] if t > cutoff]
        self._state["recent_changes"] = recent

        if len(recent) >= KILL_SWITCH_CHANGES:
            self.log(f"KILL SWITCH — {len(recent)} changes in {KILL_SWITCH_WINDOW_SEC}s. Manual mode for the night.")
            self._state["manual_mode"] = True

    # ── Occupancy & Restart ─────────────────────────────────────────

    def _check_occupancy(self, body_temp, now):
        """Return True if someone is in bed, False if empty.

        Logic: sleep_mode is ON (already checked by caller).
        - body_temp > 74°F → occupied (handles the 70→80 ramp during getting in)
        - body_temp < 72°F for 20+ min → empty (bathroom break / woke up)
        - body_temp is None → assume occupied (sensor glitch, don't skip)
        """
        if body_temp is None:
            return True

        if body_temp >= BODY_OCCUPIED_THRESHOLD_F:
            self._state["body_below_since"] = None
            return True

        if body_temp < BODY_EMPTY_THRESHOLD_F:
            below_since = self._state.get("body_below_since")
            if below_since is None:
                self._state["body_below_since"] = now.isoformat()
                return True  # Just dropped — give it time
            elapsed = (now - datetime.fromisoformat(below_since)).total_seconds() / 60
            if elapsed >= BODY_EMPTY_TIMEOUT_MIN:
                self.log(f"Bed appears empty — body={body_temp:.1f}°F for {elapsed:.0f}m")
                return False
            return True  # Not long enough yet

        # Between EMPTY (72) and OCCUPIED (74) — hysteresis zone, assume occupied
        # Reset timer so hysteresis doesn't accumulate phantom empty-bed duration
        self._state["body_below_since"] = None
        return True

    def _check_midnight_restart(self):
        """Gap 5: If AppDaemon restarts while sleeping, resume from correct position."""
        if self._is_sleeping():
            if self._state.get("sleep_start"):
                # Backfill epoch if missing (pre-epoch state files)
                if not self._state.get("sleep_start_epoch"):
                    self._state["sleep_start_epoch"] = datetime.fromisoformat(
                        self._state["sleep_start"]
                    ).timestamp()
                    self._save_state()
                elapsed = self._elapsed_min()
                self.log(f"MID-NIGHT RESTART: sleep_mode is ON, "
                         f"sleep_start from state file, elapsed={elapsed:.0f}m")
                return

            # No sleep_start in state — try to recover from HA entity last_changed
            try:
                last_changed = self.get_state(E_SLEEP_MODE, attribute="last_changed")
                if last_changed:
                    # HA returns UTC-aware ISO; convert to naive local for consistency
                    dt = datetime.fromisoformat(last_changed)
                    if dt.tzinfo is not None:
                        dt = dt.astimezone().replace(tzinfo=None)
                    self._state["sleep_start"] = dt.isoformat()
                    self._state["sleep_start_epoch"] = dt.timestamp()
                    elapsed = (datetime.now() - dt).total_seconds() / 60
                    self.log(f"MID-NIGHT RESTART: Recovered sleep_start from "
                             f"last_changed={last_changed}, elapsed={elapsed:.0f}m")
                    self._save_state()
                else:
                    self.log("MID-NIGHT RESTART: sleep_mode ON but cannot determine start time",
                             level="WARNING")
            except Exception as e:
                self.log(f"MID-NIGHT RESTART: Failed to recover sleep_start: {e}",
                         level="WARNING")
        else:
            # Sleep mode is OFF — check if we missed an end-of-night while restarting
            sleep_start = self._state.get("sleep_start")
            if sleep_start:
                elapsed_h = (datetime.now() - datetime.fromisoformat(sleep_start)).total_seconds() / 3600
                if elapsed_h < 14:  # Plausible we missed the off transition
                    self.log("RECOVERY: sleep_start set but sleep_mode OFF — running missed _end_night()")
                    self._end_night()
                self._state["sleep_start"] = None
                self._save_state()

    # ── Helpers ──────────────────────────────────────────────────────

    def _is_sleeping(self):
        state = self.get_state(E_SLEEP_MODE)
        return state == "on"

    def _elapsed_min(self):
        """Get minutes since sleep started, using epoch seconds (DST-safe)."""
        epoch = self._state.get("sleep_start_epoch")
        if epoch:
            return (datetime.now().timestamp() - epoch) / 60
        # Fallback: parse ISO string (may drift ±60 min on DST change)
        sleep_start = self._state.get("sleep_start")
        if sleep_start:
            return (datetime.now() - datetime.fromisoformat(sleep_start)).total_seconds() / 60
        return 0

    def _night_date_for(self, dt):
        """Assign a timestamp to its sleep night (before 6 PM counts as prior night)."""
        return dt.date() if dt.hour >= 18 else (dt - timedelta(days=1)).date()

    def _get_room_temp_entity(self):
        return getattr(self, "_room_temp_entity", DEFAULT_ROOM_TEMP_ENTITY)

    def _set_l1(self, value):
        value = max(-10, min(MAX_SETTING, int(value)))
        self.call_service("number/set_value", entity_id=E_BEDTIME_TEMP, value=value)
        self._state["last_setting"] = value

    def _l1_to_blower_pct(self, value):
        value = max(-10, min(MAX_SETTING, int(value)))
        return L1_TO_BLOWER_PCT[value]

    def _blower_pct_to_l1(self, blower_pct):
        blower_pct = max(0, min(100, int(round(blower_pct))))
        return min(
            L1_TO_BLOWER_PCT,
            key=lambda l1: (abs(L1_TO_BLOWER_PCT[l1] - blower_pct), l1),
        )

    def _next_colder_setting(self, value):
        return max(-10, min(MAX_SETTING, int(value) - 1))

    def _room_temp_to_blower_comp(self, room_temp):
        if room_temp is None:
            return 0
        if room_temp > ROOM_BLOWER_REFERENCE_F:
            return round((room_temp - ROOM_BLOWER_REFERENCE_F) * ROOM_BLOWER_HOT_COMP_PER_F)
        if room_temp < ROOM_BLOWER_REFERENCE_F:
            comp = (ROOM_BLOWER_REFERENCE_F - room_temp) * ROOM_BLOWER_COLD_COMP_PER_F
            if room_temp < ROOM_BLOWER_COLD_THRESHOLD_F:
                comp += (
                    ROOM_BLOWER_COLD_THRESHOLD_F - room_temp
                ) * ROOM_BLOWER_COLD_EXTRA_PER_F
            return -round(comp)
        return 0

    def _ensure_responsive_cooling_off(self):
        rc_state = self.get_state(E_RESPONSIVE_COOLING)
        if rc_state == "on":
            self.log("WARNING: Responsive cooling was ON — turning it back OFF", level="WARNING")
            self.call_service("switch/turn_off", entity_id=E_RESPONSIVE_COOLING)
            return True
        return False

    def _ensure_topper_running(self, now):
        """Re-enable the topper if sleep_mode is on and firmware turned it off."""
        running_state = self.get_state(E_RUNNING)
        if running_state == "on":
            self._state["last_restart_ts"] = None
            return False
        if running_state != "off":
            return False

        last_restart = self._state.get("last_restart_ts")
        if last_restart:
            since_restart = (now - datetime.fromisoformat(last_restart)).total_seconds()
            if since_restart < AUTO_RESTART_DEBOUNCE_SEC:
                self.log(
                    "Topper still OFF during sleep — waiting for restart debounce",
                    level="WARNING",
                )
                return False

        self.log("WARNING: Topper was OFF during sleep — auto-restarting", level="WARNING")
        try:
            self.call_service("switch/turn_on", entity_id=E_RUNNING)
        except Exception as err:
            self.log(f"FAILED to auto-restart topper: {err}", level="ERROR")
            return False

        self._state["last_restart_ts"] = now.isoformat()
        self._save_state()
        return True

    def _read_float(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(state)
        except (ValueError, TypeError):
            return None

    def _read_temperature(self, entity_id):
        value = self._read_float(entity_id)
        if value is None:
            return None
        unit = self.get_state(entity_id, attribute="unit_of_measurement")
        if isinstance(unit, str) and unit.strip().lower() in {"°c", "c", "celsius"}:
            return round((value * 9 / 5) + 32, 2)
        return value

    def _read_str(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unavailable"):
            return None
        return state

    def _read_bool(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unknown", "unavailable", ""):
            return None
        if state == "on":
            return True
        if state == "off":
            return False
        return None

    def _read_body_temp(self, entity_id):
        value = self._read_temperature(entity_id)
        if value is None:
            return None
        if not (BODY_TEMP_MIN_F <= value <= BODY_TEMP_MAX_F):
            self.log(f"{entity_id}={value:.1f}°F outside sane range — ignoring", level="WARNING")
            return None
        return value

    def _fused_body_avg(self, zone, snapshot):
        if zone == "left":
            inner_key, outer_key, adj_zone = "body_right", "body_left", "right"
        else:
            inner_key, outer_key, adj_zone = "body_left", "body_right", "left"

        inner_val = snapshot.get(inner_key)
        outer_val = snapshot.get(outer_key)
        adj_vals = [
            self._read_body_temp(ZONE_ENTITY_IDS[adj_zone][key])
            for key in ("body_left", "body_center", "body_right")
        ]
        adj_vals = [v for v in adj_vals if v is not None]
        adj_avg = sum(adj_vals) / len(adj_vals) if adj_vals else None

        use_keys = ["body_center", outer_key]
        if not (
            inner_val is not None
            and outer_val is not None
            and adj_avg is not None
            and adj_avg > inner_val
            and inner_val - outer_val > 5.0
        ):
            use_keys.append(inner_key)

        body_vals = [snapshot[key] for key in use_keys if snapshot.get(key) is not None]
        if not body_vals:
            return snapshot.get("body_center")
        body_vals = sorted(body_vals)
        mid = len(body_vals) // 2
        if len(body_vals) % 2:
            return body_vals[mid]
        return (body_vals[mid - 1] + body_vals[mid]) / 2

    def _derive_calibrated_pressure(self, raw, unoccupied, occupied):
        if raw is None or unoccupied is None or occupied is None:
            return None
        if occupied <= unoccupied:
            return None
        value = ((raw - unoccupied) * 100.0) / (occupied - unoccupied)
        return round(max(0.0, min(100.0, value)), 2)

    def _read_bed_presence_snapshot(self):
        left_pressure = self._read_float(BED_PRESENCE_ENTITIES["left_pressure"])
        right_pressure = self._read_float(BED_PRESENCE_ENTITIES["right_pressure"])
        left_unoccupied = self._read_float(BED_PRESENCE_ENTITIES["left_unoccupied_pressure"])
        right_unoccupied = self._read_float(BED_PRESENCE_ENTITIES["right_unoccupied_pressure"])
        left_occupied = self._read_float(BED_PRESENCE_ENTITIES["left_occupied_pressure"])
        right_occupied = self._read_float(BED_PRESENCE_ENTITIES["right_occupied_pressure"])
        return {
            "left_pressure": left_pressure,
            "right_pressure": right_pressure,
            "left_calibrated_pressure": self._derive_calibrated_pressure(
                left_pressure, left_unoccupied, left_occupied
            ),
            "right_calibrated_pressure": self._derive_calibrated_pressure(
                right_pressure, right_unoccupied, right_occupied
            ),
            "left_unoccupied_pressure": left_unoccupied,
            "right_unoccupied_pressure": right_unoccupied,
            "left_occupied_pressure": left_occupied,
            "right_occupied_pressure": right_occupied,
            "left_trigger_pressure": self._read_float(BED_PRESENCE_ENTITIES["left_trigger_pressure"]),
            "right_trigger_pressure": self._read_float(BED_PRESENCE_ENTITIES["right_trigger_pressure"]),
            "occupied_left": self._read_bool(BED_PRESENCE_ENTITIES["occupied_left"]),
            "occupied_right": self._read_bool(BED_PRESENCE_ENTITIES["occupied_right"]),
            "occupied_either": self._read_bool(BED_PRESENCE_ENTITIES["occupied_either"]),
            "occupied_both": self._read_bool(BED_PRESENCE_ENTITIES["occupied_both"]),
        }

    def _read_zone_snapshot(self, zone):
        """Read the current HA sensor snapshot for a topper zone."""
        entities = ZONE_ENTITY_IDS[zone]
        body_center = self._read_body_temp(entities["body_center"])
        body_left = self._read_body_temp(entities["body_left"])
        body_right = self._read_body_temp(entities["body_right"])
        setting = self._read_float(entities["bedtime"])
        snapshot = {
            "body_center": body_center,
            "body_left": body_left,
            "body_right": body_right,
            "ambient": self._read_temperature(entities["ambient"]),
            "setpoint": self._read_temperature(entities["setpoint"]),
            "blower_pct": self._read_float(entities["blower_pct"]),
            "setting": int(setting) if setting is not None else None,
        }
        snapshot["body_avg"] = self._fused_body_avg(zone, snapshot)
        return snapshot

    def _log_passive_zone_snapshot(self, zone, elapsed_min, room_temp, sleep_stage, bed_presence=None):
        """Persist a passive 5-minute telemetry snapshot for a non-controlled zone."""
        snapshot = self._read_zone_snapshot(zone)
        has_signal = any(
            snapshot[key] is not None
            for key in ("setting", "body_center", "body_left", "body_right", "ambient", "setpoint", "blower_pct")
        )
        if not has_signal:
            return

        self._log_to_postgres(
            elapsed_min,
            room_temp,
            sleep_stage,
            snapshot["body_center"],
            snapshot["setting"],
            zone=zone,
            cycle_num=self._get_cycle_num(elapsed_min),
            room_temp_comp=0,
            data_source=f"passive_{zone}",
            body_avg=snapshot["body_avg"],
            body_left=snapshot["body_left"],
            body_right=snapshot["body_right"],
            action="passive",
            ambient=snapshot["ambient"],
            setpoint=snapshot["setpoint"],
            effective=snapshot["setting"],
            baseline=None,
            learned_adj=None,
            blower_pct=snapshot["blower_pct"],
            bed_presence=bed_presence,
        )

    def _end_night(self):
        """Log nightly summary to Postgres, computing stats from controller_readings."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()

            sleep_start = self._state.get("sleep_start")
            if not sleep_start:
                return

            night_date = self._night_date_for(datetime.fromisoformat(sleep_start))

            # Compute body/ambient/room stats from this night's controller_readings
            cur.execute("""
                SELECT
                    round(avg(body_avg_f)::numeric, 1),
                    round(avg(ambient_f)::numeric, 1),
                    round(min(ambient_f)::numeric, 1),
                    round(max(ambient_f)::numeric, 1),
                    round(avg(room_temp_f)::numeric, 1),
                    round(min(room_temp_f)::numeric, 1),
                    round(max(room_temp_f)::numeric, 1)
                FROM controller_readings
                WHERE zone = 'left'
                  AND ts >= %s::timestamptz
                  AND action <> 'empty_bed'
                  AND body_avg_f > 74
            """, (sleep_start,))
            stats = cur.fetchone()
            avg_body = float(stats[0]) if stats and stats[0] else None
            avg_ambient = float(stats[1]) if stats and stats[1] else None
            min_ambient = float(stats[2]) if stats and stats[2] else None
            max_ambient = float(stats[3]) if stats and stats[3] else None
            avg_room = float(stats[4]) if stats and stats[4] else None
            min_room = float(stats[5]) if stats and stats[5] else None
            max_room = float(stats[6]) if stats and stats[6] else None
            cur.close()

            # Read L2/L3 only when 3-level mode is actually enabled.
            if self.get_state(E_PROFILE_3LEVEL) == "on":
                sleep_setting = self._read_float("number.smart_topper_left_side_sleep_temperature")
                wake_setting = self._read_float("number.smart_topper_left_side_wake_temperature")
            else:
                sleep_setting = None
                wake_setting = None

            # Read Apple Health sleep totals
            deep = self._read_float("input_number.apple_health_sleep_deep_hrs")
            rem = self._read_float("input_number.apple_health_sleep_rem_hrs")
            core = self._read_float("input_number.apple_health_sleep_core_hrs")
            awake = self._read_float("input_number.apple_health_sleep_awake_hrs")

            cur = conn.cursor()
            cur.execute("""
                INSERT INTO nightly_summary
                (night_date, bedtime_ts, wake_ts, duration_hours,
                 bedtime_setting, sleep_setting, wake_setting,
                 avg_ambient_f, min_ambient_f, max_ambient_f,
                 avg_room_f, min_room_f, max_room_f, avg_body_f,
                 override_count, manual_mode, controller_ver,
                 total_sleep_min, deep_sleep_min, rem_sleep_min, core_sleep_min, awake_min)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_date) DO UPDATE SET
                 wake_ts = EXCLUDED.wake_ts,
                 duration_hours = EXCLUDED.duration_hours,
                 bedtime_setting = EXCLUDED.bedtime_setting,
                 sleep_setting = EXCLUDED.sleep_setting,
                 wake_setting = EXCLUDED.wake_setting,
                 avg_ambient_f = EXCLUDED.avg_ambient_f,
                 min_ambient_f = EXCLUDED.min_ambient_f,
                 max_ambient_f = EXCLUDED.max_ambient_f,
                 avg_room_f = EXCLUDED.avg_room_f,
                 min_room_f = EXCLUDED.min_room_f,
                 max_room_f = EXCLUDED.max_room_f,
                 avg_body_f = EXCLUDED.avg_body_f,
                 override_count = EXCLUDED.override_count,
                 manual_mode = EXCLUDED.manual_mode,
                 controller_ver = EXCLUDED.controller_ver,
                 total_sleep_min = COALESCE(EXCLUDED.total_sleep_min, nightly_summary.total_sleep_min),
                 deep_sleep_min = COALESCE(EXCLUDED.deep_sleep_min, nightly_summary.deep_sleep_min),
                 rem_sleep_min = COALESCE(EXCLUDED.rem_sleep_min, nightly_summary.rem_sleep_min),
                 core_sleep_min = COALESCE(EXCLUDED.core_sleep_min, nightly_summary.core_sleep_min),
                 awake_min = COALESCE(EXCLUDED.awake_min, nightly_summary.awake_min)
            """, (
                night_date,
                sleep_start,
                datetime.now().isoformat(),
                (datetime.now() - datetime.fromisoformat(sleep_start)).total_seconds() / 3600,
                self._state.get("initial_setting", self._state.get("last_setting")),
                int(sleep_setting) if sleep_setting is not None else None,
                int(wake_setting) if wake_setting is not None else None,
                avg_ambient, min_ambient, max_ambient,
                avg_room, min_room, max_room, avg_body,
                self._state.get("override_count", 0),
                self._state.get("manual_mode", False),
                CONTROLLER_VERSION,
                ((deep or 0) + (rem or 0) + (core or 0)) * 60 if deep is not None else None,
                (deep or 0) * 60 if deep is not None else None,
                (rem or 0) * 60 if rem is not None else None,
                (core or 0) * 60 if core is not None else None,
                (awake or 0) * 60 if awake is not None else None,
            ))
            conn.commit()
            cur.close()
            self.log("Nightly summary saved to Postgres")
        except Exception as e:
            self.log(f"Failed to save nightly summary: {e}", level="WARNING")

    # ── Learning System ─────────────────────────────────────────────

    def _learn_from_history(self):
        """Compute per-cycle blower residuals from recent override history."""
        try:
            conn = self._get_pg()
            if not conn:
                return self._load_learned()
            cur = conn.cursor()

            cur.execute("""
                SELECT
                    elapsed_min,
                    setting as override_setting,
                    effective as controller_setting,
                    CASE
                        WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
                        THEN (ts AT TIME ZONE 'America/New_York')::date
                        ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
                    END AS night
                FROM controller_readings
                WHERE zone = 'left'
                  AND action = 'override'
                  AND setting IS NOT NULL
                  AND effective IS NOT NULL
                  AND setting IS DISTINCT FROM effective
                  AND controller_version = %s
                  AND ts > now() - %s * interval '1 day'
                ORDER BY ts
            """, (CONTROLLER_VERSION, LEARNING_LOOKBACK_NIGHTS))

            overrides = cur.fetchall()
            cur.close()
            if not overrides:
                self.log("  Learning: no overrides in history — using defaults")
                return {}

            last_per_cycle_night = {}
            for elapsed_min, override_val, ctrl_val, night in overrides:
                if elapsed_min is None or ctrl_val is None:
                    continue
                cycle = self._get_cycle_num(elapsed_min)
                delta = self._l1_to_blower_pct(override_val) - self._l1_to_blower_pct(ctrl_val)
                last_per_cycle_night[(night, cycle)] = delta

            if not last_per_cycle_night:
                return {}

            cycle_deltas = {}
            for (night, cycle), delta in sorted(last_per_cycle_night.items()):
                cycle_deltas.setdefault(cycle, []).append(delta)

            adjustments = {}
            nights = set(n for n, _ in last_per_cycle_night.keys())
            for cycle, deltas in cycle_deltas.items():
                if not deltas:
                    continue
                weighted_sum = 0
                weight_total = 0
                for i, d in enumerate(reversed(deltas)):
                    w = LEARNING_DECAY ** i
                    weighted_sum += d * w
                    weight_total += w
                avg_delta = weighted_sum / weight_total if weight_total else 0
                adj = max(-LEARNING_MAX_BLOWER_ADJ, min(LEARNING_MAX_BLOWER_ADJ, round(avg_delta)))
                if adj != 0:
                    adjustments[str(cycle)] = adj

            self.log(f"  Learning: {len(last_per_cycle_night)} override events across {len(nights)} nights → {adjustments}")
            return adjustments

        except Exception as e:
            self.log(f"Learning failed: {e}", level="WARNING")
            return self._load_learned()

    def _load_learned(self):
        """Load learned adjustments from file."""
        try:
            if LEARNED_FILE.exists():
                with open(LEARNED_FILE) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return {}
                # Clamp values on load in case file was manually edited
                return {
                    k: max(-LEARNING_MAX_BLOWER_ADJ, min(LEARNING_MAX_BLOWER_ADJ, int(v)))
                    for k, v in data.items()
                    if isinstance(v, (int, float))
                }
        except Exception:
            pass
        return {}

    def _save_learned(self):
        """Save learned adjustments to file."""
        try:
            fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._learned, f, indent=2)
                os.replace(tmp, str(LEARNED_FILE))
            except Exception:
                os.unlink(tmp)
                raise
        except Exception as e:
            self.log(f"Learned save failed: {e}", level="WARNING")

    def _log_to_postgres(self, elapsed_min, room_temp, sleep_stage, body_center, setting,
                        zone="left", cycle_num=None, room_temp_comp=0, data_source="unknown",
                        override_floor=None, body_avg=None, body_left=None,
                        body_right=None, action="set", ambient=None,
                        setpoint=None, effective=None, baseline=None,
                        learned_adj=None, blower_pct=None,
                        target_blower_pct=None, base_blower_pct=None,
                        responsive_cooling_on=None, bed_presence=None):
        """Log a controller reading to Postgres."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()
            try:
                if ambient is None:
                    ambient = self._read_temperature(ZONE_ENTITY_IDS[zone]["ambient"])
                if setpoint is None:
                    setpoint = self._read_temperature(ZONE_ENTITY_IDS[zone]["setpoint"])

                # Compute real body average if not provided
                if body_avg is None:
                    body_vals = [v for v in (body_left, body_center, body_right) if v is not None]
                    body_avg = sum(body_vals) / len(body_vals) if body_vals else body_center

                if cycle_num is None:
                    cycle_num = self._get_cycle_num(elapsed_min)

                # Read actual HA entity value as 'effective' (what's really set on device)
                if effective is None:
                    effective_raw = self._read_float(ZONE_ENTITY_IDS[zone]["bedtime"])
                    effective = int(effective_raw) if effective_raw is not None else setting

                if learned_adj is None:
                    learned_adj = self._learned.get(str(cycle_num), 0) if zone == "left" else None
                if baseline is None and zone == "left":
                    baseline = CYCLE_SETTINGS.get(cycle_num, -5)
                if blower_pct is None:
                    blower_pct = self._read_float(ZONE_ENTITY_IDS[zone]["blower_pct"])

                notes = (
                    f"cycle={cycle_num} src={data_source} "
                    f"room_comp={room_temp_comp:+d}"
                )
                if override_floor is not None:
                    notes += f" floor={override_floor:+d}"
                if sleep_stage:
                    notes += f" stage={sleep_stage}"
                if base_blower_pct is not None:
                    notes += f" base_proxy_blower={base_blower_pct}"
                if target_blower_pct is not None:
                    notes += f" proxy_blower={target_blower_pct}"
                if blower_pct is not None:
                    notes += f" actual_blower={round(blower_pct)}"
                if responsive_cooling_on is not None:
                    notes += f" rc={'on' if responsive_cooling_on else 'off'}"
                if bed_presence is None:
                    bed_presence = self._read_bed_presence_snapshot()

                cur.execute("""
                    INSERT INTO controller_readings
                    (ts, zone, phase, elapsed_min, body_right_f, body_center_f,
                     body_left_f, body_avg_f, ambient_f, room_temp_f, setpoint_f,
                     setting, effective, baseline, learned_adj, action, notes, controller_version,
                     bed_left_pressure_pct, bed_right_pressure_pct,
                     bed_left_calibrated_pressure_pct, bed_right_calibrated_pressure_pct,
                     bed_left_unoccupied_pressure_pct, bed_right_unoccupied_pressure_pct,
                     bed_left_occupied_pressure_pct, bed_right_occupied_pressure_pct,
                     bed_left_trigger_pressure_pct, bed_right_trigger_pressure_pct,
                     bed_occupied_left, bed_occupied_right, bed_occupied_either, bed_occupied_both)
                    VALUES (
                        NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                """, (
                    zone,
                    sleep_stage if sleep_stage and sleep_stage not in ("unknown", "") else f"cycle_{cycle_num}",
                    elapsed_min,
                    body_right, body_center, body_left,
                    body_avg,
                    ambient, room_temp, setpoint,
                    setting, effective,
                    baseline,
                    learned_adj,
                    action,
                    notes,
                    CONTROLLER_VERSION,
                    bed_presence.get("left_pressure"),
                    bed_presence.get("right_pressure"),
                    bed_presence.get("left_calibrated_pressure"),
                    bed_presence.get("right_calibrated_pressure"),
                    bed_presence.get("left_unoccupied_pressure"),
                    bed_presence.get("right_unoccupied_pressure"),
                    bed_presence.get("left_occupied_pressure"),
                    bed_presence.get("right_occupied_pressure"),
                    bed_presence.get("left_trigger_pressure"),
                    bed_presence.get("right_trigger_pressure"),
                    bed_presence.get("occupied_left"),
                    bed_presence.get("occupied_right"),
                    bed_presence.get("occupied_either"),
                    bed_presence.get("occupied_both"),
                ))
                conn.commit()
            finally:
                cur.close()
        except Exception as e:
            self.log(f"Postgres log failed: {e}", level="WARNING")
            try:
                self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None

    def _log_override(self, zone, value, controller_value=None, delta=None,
                      room_temp=None, sleep_stage=None, snapshot=None):
        """Log a manual override to Postgres."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()
            try:
                if room_temp is None:
                    room_temp = self._read_temperature(self._get_room_temp_entity())
                if snapshot is None:
                    snapshot = self._read_zone_snapshot(zone)
                bed_presence = self._read_bed_presence_snapshot()
                elapsed = self._elapsed_min()
                cycle_num = self._get_cycle_num(elapsed)
                phase = sleep_stage if sleep_stage and sleep_stage not in ("unknown", "") else f"cycle_{cycle_num}"
                baseline = CYCLE_SETTINGS.get(cycle_num, -5) if zone == "left" else None
                learned_adj = self._learned.get(str(cycle_num), 0) if zone == "left" else None
                notes = f"cycle={cycle_num} src=manual zone={zone}"
                if sleep_stage:
                    notes += f" stage={sleep_stage}"
                if controller_value is not None:
                    notes += f" controller={controller_value:+d}"
                    notes += (
                        f" controller_proxy_blower={self._l1_to_blower_pct(controller_value)}"
                    )
                notes += f" override_proxy_blower={self._l1_to_blower_pct(value)}"
                if snapshot.get("blower_pct") is not None:
                    notes += f" actual_blower={round(snapshot['blower_pct'])}"
                if zone == "left":
                    notes += " rc=off"

                cur.execute("""
                    INSERT INTO controller_readings
                    (ts, zone, phase, elapsed_min, body_right_f, body_center_f,
                     body_left_f, body_avg_f, ambient_f, room_temp_f, setpoint_f,
                     setting, effective, baseline, learned_adj, action,
                     override_delta, notes, controller_version,
                     bed_left_pressure_pct, bed_right_pressure_pct,
                     bed_left_calibrated_pressure_pct, bed_right_calibrated_pressure_pct,
                     bed_left_unoccupied_pressure_pct, bed_right_unoccupied_pressure_pct,
                     bed_left_occupied_pressure_pct, bed_right_occupied_pressure_pct,
                     bed_left_trigger_pressure_pct, bed_right_trigger_pressure_pct,
                     bed_occupied_left, bed_occupied_right, bed_occupied_either, bed_occupied_both)
                    VALUES (
                        NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'override', %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                """, (
                    zone,
                    phase,
                    elapsed,
                    snapshot["body_right"],
                    snapshot["body_center"],
                    snapshot["body_left"],
                    snapshot["body_avg"],
                    snapshot["ambient"],
                    room_temp,
                    snapshot["setpoint"],
                    value,
                    controller_value if controller_value is not None else snapshot["setting"],
                    baseline,
                    learned_adj,
                    delta,
                    notes,
                    CONTROLLER_VERSION,
                    bed_presence.get("left_pressure"),
                    bed_presence.get("right_pressure"),
                    bed_presence.get("left_calibrated_pressure"),
                    bed_presence.get("right_calibrated_pressure"),
                    bed_presence.get("left_unoccupied_pressure"),
                    bed_presence.get("right_unoccupied_pressure"),
                    bed_presence.get("left_occupied_pressure"),
                    bed_presence.get("right_occupied_pressure"),
                    bed_presence.get("left_trigger_pressure"),
                    bed_presence.get("right_trigger_pressure"),
                    bed_presence.get("occupied_left"),
                    bed_presence.get("occupied_right"),
                    bed_presence.get("occupied_either"),
                    bed_presence.get("occupied_both"),
                ))
                conn.commit()
            finally:
                cur.close()
        except Exception as e:
            self.log(f"Override log failed: {e}", level="WARNING")
            try:
                self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None

    def _get_pg(self):
        """Get or create Postgres connection."""
        if self._pg_conn is not None:
            try:
                cur = self._pg_conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
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
                host=self._pg_host, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASS,
                connect_timeout=3,
                options="-c statement_timeout=3000",  # 3s max per query
            )
            return self._pg_conn
        except Exception as e:
            self.log(f"Postgres connect failed: {e}", level="WARNING")
            return None

    def _save_state(self):
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(STATE_DIR), suffix=".tmp", prefix="ctrl_state_"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._state, f, indent=2, default=str)
                os.replace(tmp_path, str(STATE_FILE))
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            self.log(f"State save failed: {e}", level="WARNING")

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    saved = json.load(f)
                self._state.update(saved)
                self.log(f"Loaded state from {STATE_FILE}")
        except Exception as e:
            self.log(f"State load failed: {e}", level="WARNING")

    def terminate(self):
        self.log("Controller v5 shutting down — saving state")
        self._save_state()
        if self._pg_conn:
            try:
                self._pg_conn.close()
            except Exception:
                pass
