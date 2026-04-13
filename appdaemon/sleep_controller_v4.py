"""
Sleep Controller v4 — Works WITH the firmware, not against it.
================================================================

Architecture:
  The topper's firmware has a PID controller that adjusts blower/heaters
  to reach a target surface temperature. That PID runs continuously with
  direct hardware access. WE DO NOT REPLACE IT.

  This controller's job is simpler: pick the RIGHT L1 setting based on:
    1. Sleep phase (cyclical ~90-min cycles, not linear)
    2. Room temperature (from dehumidifier sensor — real room, not topper's inflated ambient)
    3. User preferences (warm sleeper, never heat)

  We change L1 infrequently (when phase changes or room temp shifts) and
  let the firmware's PID handle the fine-grained control.

Key principles:
  - Firmware responsive cooling stays ON — it handles second-by-second PID
  - We set L1 = "what temperature experience does the user want right now?"
  - Sleep is CYCLICAL: ~90-min cycles of Light→Deep→REM, repeating 4-6x/night
  - Deep sleep dominates early cycles, REM dominates later ones
  - Room temp compensation is physics, always applied
  - User runs warm, NEVER set above 0 (neutral)
  - User overrides are SACRED — never fight them
  - Everything logged to Postgres (192.168.0.75)

Firmware behavior (experimentally verified):
  L1=-8 → targets ~69°F surface (max cooling)
  L1= 0 → targets ~91°F surface (neutral, topper essentially off)
  L1=+5 → targets ~96°F surface (heating)
  The topper blows room-temp air, so in a 60°F room, L1=-5 still chills you.

Sensor reliability (verified with empty bed, topper on/off):
  Body sensors (TSL/TSC/TSR): RELIABLE. Pad surface thermistors.
    Empty bed = room temp + 1-3°F. Occupied = 80-89°F. Always updating.
  Ambient (TA): UNRELIABLE. Inside the base unit, not exposed to room air.
    Reads high when blower off (electronics heat), varies with blower state.
    Use dehumidifier sensor (sensor.superior_6000s_temperature) for real room temp.
  Setpoint (TempSP): Firmware's PID target in absolute °F.
    Useful to see what firmware is trying to do. Tracks body sensor when off.
  Blower %: FREEZES at last value when topper turns off. Only valid when running.
  Heater raw (IHH/IHF): NOT temperatures. Unknown encoding. Ignore.
  PID (Ctrl Out/P/I): Firmware's PI controller. Positive = cooling. Slow updates (~30s).
"""

import json
import os
import tempfile
from datetime import datetime
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

# Settings per cycle position (-10 to +10 scale, capped at 0)
# These are the BASELINE before room temp compensation and body temp feedback.
# Flattened curve — previous settings warmed too aggressively (user woke warm).
CYCLE_SETTINGS = {
    1: -8,   # First cycle: aggressive cooling for sleep onset
    2: -7,   # Second cycle: still cooling, deep sleep dominant
    3: -6,   # Third cycle: moderate, REM increasing
    4: -5,   # Fourth cycle: still cooling, REM dominant
    5: -4,   # Fifth cycle: approaching morning
    6: -3,   # Sixth cycle: near wake
}
CYCLE_DURATION_MIN = 90

# ── Feature Flags ────────────────────────────────────────────────────
# New experimental features. Learning defaults OFF until proven.
ENABLE_BODY_FEEDBACK = True          # Body temp reactive cooling — ON (addresses waking warm)
ENABLE_LEARNING = False              # Adaptive learning from override history — OFF until stable

# Body temperature feedback — reactive cooling tuning (requires ENABLE_BODY_FEEDBACK)
BODY_COMFORT_TARGET_F = 82.0         # Ideal body sensor reading during sleep
BODY_TEMP_COOL_BIAS_PER_F = 0.5     # Push L1 -0.5 per °F above comfort target
BODY_TEMP_MAX_BIAS = -3              # Don't push more than 3 steps extra

# Room temperature compensation
AMBIENT_REFERENCE_F = 68.0          # Calibration point — 7-day overnight average is 68.3°F
AMBIENT_COMP_PER_F = 0.8            # Base: +0.8 setting per °F below reference
AMBIENT_COLD_THRESHOLD_F = 63.0     # Below this, extra compensation kicks in
AMBIENT_COLD_EXTRA_PER_F = 0.5      # Additional per °F below threshold
AMBIENT_HOT_COMP_PER_F = 0.4        # Hot rooms: -0.4 setting per °F above reference
MAX_SETTING = 0                     # NEVER heat — user runs warm

# Override handling
OVERRIDE_FREEZE_MIN = 60            # Freeze controller for 1 hour after manual change
OVERRIDE_FLOOR_DECAY_MIN = 120      # Floor decays after 2 hours — let controller adapt
MIN_CHANGE_INTERVAL_SEC = 1800      # Don't change L1 more than once per 30 min
KILL_SWITCH_CHANGES = 3             # 3 manual changes in this window...
KILL_SWITCH_WINDOW_SEC = 300        # ...within 5 min = manual mode for night

# Occupancy — body sensors read 67-70°F empty, 80-89°F occupied
BODY_OCCUPIED_THRESHOLD_F = 74.0    # Getting-in threshold (ramp from 70→80 takes a few min)
BODY_EMPTY_THRESHOLD_F = 72.0       # Below this, start the empty-bed timer
BODY_EMPTY_TIMEOUT_MIN = 20         # Must stay below empty threshold for this long

# Entities (left side only — right side uses stock firmware)
E_BEDTIME_TEMP = "number.smart_topper_left_side_bedtime_temperature"
E_RUNNING = "switch.smart_topper_left_side_running"
E_RESPONSIVE_COOLING = "switch.smart_topper_left_side_responsive_cooling"
E_BODY_CENTER = "sensor.smart_topper_left_side_body_sensor_center"
E_BODY_LEFT = "sensor.smart_topper_left_side_body_sensor_left"
E_BODY_RIGHT = "sensor.smart_topper_left_side_body_sensor_right"
E_ROOM_TEMP = "sensor.superior_6000s_temperature"
E_SLEEP_STAGE = "input_text.apple_health_sleep_stage"
E_SLEEP_MODE = "input_boolean.sleep_mode"

# Postgres
PG_HOST = "192.168.0.75"
PG_PORT = 5432
PG_DB = "sleepdata"
PG_USER = "sleepsync"
PG_PASS = "sleepsync_local"

# State file (backup — primary state is in-memory)
_container = Path("/config/apps")
_host = Path("/addon_configs/a0d7b954_appdaemon/apps")
STATE_DIR = _container if _container.exists() else _host
STATE_FILE = STATE_DIR / "controller_state_v4.json"
LEARNED_FILE = STATE_DIR / "learned_adjustments.json"

# Learning parameters
LEARNING_LOOKBACK_NIGHTS = 14       # Analyze this many recent nights
LEARNING_MAX_ADJ = 3                # Max learned adjustment per cycle (±3)
LEARNING_DECAY = 0.7                # Weight: recent overrides count more


class SleepControllerV4(hass.Hass):

    def initialize(self):
        self.log("=" * 60)
        self.log("Sleep Controller v4 initializing")

        self._state = {
            "sleep_start": None,        # When sleep began (ISO string)
            "last_setting": None,       # Current L1 value
            "last_change_ts": None,     # When we last changed L1
            "override_floor": None,     # User's last manual value (floor)
            "override_floor_ts": None,  # When the floor was set
            "manual_mode": False,       # Kill switch triggered
            "recent_changes": [],       # Timestamps of recent manual changes
            "override_count": 0,        # Total overrides this night
            "body_below_since": None,   # When body first dropped below BODY_EMPTY_THRESHOLD_F
        }
        self._load_state()
        self._pg_conn = None
        self._learned = self._load_learned() if ENABLE_LEARNING else {}

        # Ensure responsive cooling is ON — we work WITH the firmware
        self.call_service("switch/turn_on", entity_id=E_RESPONSIVE_COOLING)

        # Run control loop every 5 minutes
        self.run_every(self._control_loop, "now", 300)

        # Listen for sleep mode changes
        self.listen_state(self._on_sleep_mode, E_SLEEP_MODE)

        # Listen for manual setting changes (override detection)
        self.listen_state(self._on_setting_change, E_BEDTIME_TEMP)

        # Gap 5: If sleep_mode is already ON (mid-night restart), resume
        self._check_midnight_restart()

        self.log("Controller v4 ready — working WITH firmware PID")
        self.log(f"  Cycles: {CYCLE_SETTINGS}")
        self.log(f"  Learned: {self._learned}")
        self.log(f"  Room temp: {E_ROOM_TEMP}")
        self.log(f"  Max setting: {MAX_SETTING}")
        self.log("=" * 60)

    # ── Main Control Loop ────────────────────────────────────────────

    def _control_loop(self, kwargs):
        now = datetime.now()

        # Responsive cooling watchdog — ALWAYS runs
        rc_state = self.get_state(E_RESPONSIVE_COOLING)
        if rc_state == "off":
            self.log("WARNING: Responsive cooling was OFF — re-enabling", level="WARNING")
            self.call_service("switch/turn_on", entity_id=E_RESPONSIVE_COOLING)

        # Read body sensors (needed for both auto-start and control)
        body_center = self._read_float(E_BODY_CENTER)
        body_left = self._read_float(E_BODY_LEFT)
        body_right = self._read_float(E_BODY_RIGHT)
        body_vals = [v for v in (body_left, body_center, body_right) if v is not None]
        body_max = max(body_vals) if body_vals else None
        body_avg = sum(body_vals) / len(body_vals) if body_vals else None

        # Auto-start: topper is running + someone in bed + no active night
        if not self._state.get("sleep_start"):
            running = self.get_state(E_RUNNING)
            if running == "on" and body_max is not None and body_max >= BODY_OCCUPIED_THRESHOLD_F:
                self._start_night(now)
            else:
                return  # Not sleeping, nothing to do

        # Auto-end: bed has been empty long enough
        if not self._check_occupancy(body_max, now):
            elapsed_min = self._elapsed_min()
            if elapsed_min > 0:
                room_temp = self._read_float(E_ROOM_TEMP)
                sleep_stage = self._read_str(E_SLEEP_STAGE)
                self._log_to_postgres(
                    elapsed_min, room_temp, sleep_stage, body_center,
                    self._state.get("last_setting", -8),
                    body_avg=body_avg, body_left=body_left, body_right=body_right,
                    action="empty_bed",
                )
            return

        # Manual mode (kill switch) — hands off for the night
        if self._state["manual_mode"]:
            return

        # Override freeze — derived from override_floor_ts (first 60 min = freeze)
        floor_ts = self._state.get("override_floor_ts")
        if floor_ts:
            floor_age_min = (now - datetime.fromisoformat(floor_ts)).total_seconds() / 60
            if floor_age_min < OVERRIDE_FREEZE_MIN:
                remaining = OVERRIDE_FREEZE_MIN - floor_age_min
                self.log(f"Override freeze — {remaining:.0f}m remaining")
                return

        # Rate limit — don't change more than once per MIN_CHANGE_INTERVAL_SEC
        last_change = self._state.get("last_change_ts")
        if last_change:
            elapsed = (now - datetime.fromisoformat(last_change)).total_seconds()
            if elapsed < MIN_CHANGE_INTERVAL_SEC:
                return

        # Compute the right setting
        room_temp = self._read_float(E_ROOM_TEMP)
        # Plausibility guard — reject sensor glitches outside sane range
        if room_temp is not None and not (40.0 <= room_temp <= 100.0):
            self.log(f"Room temp {room_temp}°F out of range — ignoring", level="WARNING")
            room_temp = None
        sleep_stage = self._read_str(E_SLEEP_STAGE)

        # body_center, body_left, body_right, body_avg already computed at top of loop

        elapsed_min = self._elapsed_min()

        # Determine setting from sleep cycle position + body temp feedback
        setting, room_temp_comp, data_source = self._compute_setting(
            elapsed_min, room_temp, sleep_stage, body_avg=body_avg
        )

        # Apply override floor — decays after OVERRIDE_FLOOR_DECAY_MIN
        floor = self._state.get("override_floor")
        floor_ts = self._state.get("override_floor_ts")
        if floor is not None and floor_ts:
            floor_age_min = (now - datetime.fromisoformat(floor_ts)).total_seconds() / 60
            if floor_age_min > OVERRIDE_FLOOR_DECAY_MIN:
                self.log(f"Override floor {floor:+d} expired after {floor_age_min:.0f}m")
                self._state["override_floor"] = None
                self._state["override_floor_ts"] = None
                floor = None
        if floor is not None and setting < floor:
            self.log(f"Override floor: computed {setting:+d} < floor {floor:+d}, using floor")
            setting = floor

        cycle_num = self._get_cycle_num(elapsed_min)

        # Apply
        current = self._read_float(E_BEDTIME_TEMP)
        if current is None:
            self.log("Bedtime temp entity unavailable — skipping", level="WARNING")
            return
        changed = int(current) != setting
        if changed:
            self.log(
                f"Cycle {cycle_num}, "
                f"elapsed={elapsed_min:.0f}m, room={room_temp}°F, "
                f"stage={sleep_stage}, src={data_source}, "
                f"room_comp={room_temp_comp:+d}: "
                f"{int(current):+d} → {setting:+d}"
            )
            self._set_l1(setting)
            self._state["last_change_ts"] = now.isoformat()
            self._save_state()

        # Log every iteration to Postgres for telemetry (not just changes)
        action = "set" if changed else "hold"
        self._log_to_postgres(
            elapsed_min, room_temp, sleep_stage, body_center, setting,
            cycle_num=cycle_num, room_temp_comp=room_temp_comp,
            data_source=data_source, override_floor=floor,
            body_avg=body_avg, body_left=body_left, body_right=body_right,
            action=action,
        )

    def _compute_setting(self, elapsed_min, room_temp, sleep_stage, body_avg=None):
        """Compute L1 setting from cycle position + learned adj + body feedback + room comp.

        Returns (setting, room_temp_comp, data_source) where:
          room_temp_comp: degrees of room temp compensation applied
          data_source: 'stage', 'time_cycle', 'time_cycle+learned', etc.
        """
        room_temp_comp = 0

        # If we have real sleep stage data, use it directly
        if sleep_stage and sleep_stage not in ("unknown", ""):
            setting = self._setting_for_stage(sleep_stage)
            data_source = "stage"
        else:
            # Fall back to time-based cycle estimation
            cycle_num = self._get_cycle_num(elapsed_min)
            setting = CYCLE_SETTINGS.get(cycle_num, CYCLE_SETTINGS[max(CYCLE_SETTINGS.keys())])
            data_source = "time_cycle"

            # Apply learned per-cycle adjustment from override history
            learned_adj = self._learned.get(str(cycle_num), 0)
            learned_adj = max(-LEARNING_MAX_ADJ, min(LEARNING_MAX_ADJ, learned_adj))
            if learned_adj != 0:
                setting += learned_adj
                data_source += "+learned"

        # Body temperature feedback — if body is warmer than comfort target,
        # push L1 more negative. This makes the firmware PID work harder.
        body_bias = 0
        if ENABLE_BODY_FEEDBACK and body_avg is not None and body_avg > BODY_COMFORT_TARGET_F:
            body_bias = -round((body_avg - BODY_COMFORT_TARGET_F) * BODY_TEMP_COOL_BIAS_PER_F)
            body_bias = max(body_bias, BODY_TEMP_MAX_BIAS)  # cap at -3
            setting += body_bias
            if body_bias != 0:
                data_source += "+body"

        # Room temperature compensation — physics, always applied
        if room_temp is not None:
            delta = AMBIENT_REFERENCE_F - room_temp
            if delta > 0:
                # Cold room: raise setting (less cooling needed from topper)
                room_temp_comp = delta * AMBIENT_COMP_PER_F
                if room_temp < AMBIENT_COLD_THRESHOLD_F:
                    room_temp_comp += (AMBIENT_COLD_THRESHOLD_F - room_temp) * AMBIENT_COLD_EXTRA_PER_F
                room_temp_comp = round(room_temp_comp)
                setting += room_temp_comp
            elif delta < 0:
                # Hot room: lower setting (firmware PID needs help, push target colder)
                room_temp_comp = round(delta * AMBIENT_HOT_COMP_PER_F)  # negative
                setting += room_temp_comp

        # Hard cap — never heat
        setting = min(MAX_SETTING, setting)
        setting = max(-10, setting)

        return setting, room_temp_comp, data_source

    def _setting_for_stage(self, stage):
        """Map Apple Health sleep stage to ideal setting."""
        stage = stage.lower().strip()
        return {
            "deep":  -8,   # Deep sleep: thermoregulation active, cooling helps
            "core":  -6,   # Light/core sleep: moderate cooling
            "rem":   -4,   # REM: thermoregulation impaired, ease off cooling
            "awake": -3,   # Awake in bed: minimal cooling
            "inbed": -5,   # In bed but not asleep yet
        }.get(stage, -5)   # Default: moderate

    def _get_cycle_num(self, elapsed_min):
        """Get cycle number (1-based) from elapsed minutes."""
        return min(max(1, int(elapsed_min / CYCLE_DURATION_MIN) + 1), max(CYCLE_SETTINGS.keys()))

    # ── Night Start / Stop ──────────────────────────────────────────

    def _start_night(self, now):
        """Start a new sleep night. Called by auto-detection or sleep_mode toggle."""
        if self._state.get("sleep_start"):
            return  # Already in a night
        self.log("Night started — bed occupied, topper running")
        self._state["sleep_start"] = now.isoformat()
        self._state["sleep_start_epoch"] = now.timestamp()
        self._state["manual_mode"] = False
        self._state["override_floor"] = None
        self._state["override_floor_ts"] = None
        self._state["recent_changes"] = []
        self._state["override_count"] = 0
        self._state["last_change_ts"] = None
        self._state["body_below_since"] = None
        # Reset sleep stage to prevent stale prior-night data
        try:
            self.call_service(
                "input_text/set_value",
                entity_id=E_SLEEP_STAGE, value="unknown",
            )
        except Exception:
            pass
        # Refresh learned adjustments
        if ENABLE_LEARNING:
            self._learned = self._learn_from_history()
            self._save_learned()
        self.log(f"  Learned adjustments: {self._learned}")
        # Compute initial setting
        room_temp = self._read_float(E_ROOM_TEMP)
        initial_setting, comp, _ = self._compute_setting(0, room_temp, None)
        self.log(f"  Initial L1={initial_setting:+d} (room={room_temp}°F, comp={comp:+d})")
        self._set_l1(initial_setting)
        self._state["last_setting"] = initial_setting
        self._state["initial_setting"] = initial_setting
        self._save_state()

    def _end_night_and_reset(self):
        """End the current night, log summary, clear state."""
        self.log("Night ended — bed empty")
        self._end_night()
        self._state["sleep_start"] = None
        self._state["sleep_start_epoch"] = None
        self._state["manual_mode"] = False
        self._state["override_floor"] = None
        self._state["override_floor_ts"] = None
        self._save_state()

    def _on_sleep_mode(self, entity, attribute, old, new, kwargs):
        """Manual sleep_mode toggle — still works as override."""
        if new == "on" and old != "on":
            self._start_night(datetime.now())
        elif new == "off" and old == "on":
            if self._state.get("sleep_start"):
                self._end_night_and_reset()

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

        # Was this change made by US?
        expected = self._state.get("last_setting")
        if expected is not None and new_val == expected:
            return  # We made this change, ignore

        # This is a manual override
        now = datetime.now()
        self.log(f"MANUAL OVERRIDE: {old_val:+d} → {new_val:+d}")

        # Record for kill switch detection (cap list to prevent unbounded growth)
        cutoff = now.timestamp() - KILL_SWITCH_WINDOW_SEC
        self._state["recent_changes"] = [
            t for t in self._state["recent_changes"] if t > cutoff
        ]
        self._state["recent_changes"].append(now.timestamp())
        self._check_kill_switch()

        # Set floor (freeze is derived from floor_ts — first 60 min)
        # Clamp floor to MAX_SETTING — user runs warm, floor must never allow heating
        self._state["override_floor"] = min(new_val, MAX_SETTING)
        self._state["override_floor_ts"] = now.isoformat()
        self._state["last_setting"] = new_val
        self._state["override_count"] = self._state.get("override_count", 0) + 1

        self._log_override(new_val, delta=new_val - old_val)
        self._save_state()

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

        When empty timeout expires, automatically ends the night.
        - body_temp > 74°F → occupied
        - body_temp < 72°F for 20+ min → empty, ends night
        - body_temp is None → assume occupied (sensor glitch)
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
                self.log(f"Bed empty for {elapsed:.0f}m (body={body_temp:.1f}°F) — ending night")
                self._end_night_and_reset()
                return False
            return True  # Not long enough yet

        # Between EMPTY (72) and OCCUPIED (74) — hysteresis zone, assume occupied
        # Reset timer so hysteresis doesn't accumulate phantom empty-bed duration
        self._state["body_below_since"] = None
        return True

    def _check_midnight_restart(self):
        """If AppDaemon restarts during an active night, resume from saved state."""
        sleep_start = self._state.get("sleep_start")
        if not sleep_start:
            return  # No night was active

        # Backfill epoch if missing
        if not self._state.get("sleep_start_epoch"):
            try:
                self._state["sleep_start_epoch"] = datetime.fromisoformat(sleep_start).timestamp()
                self._save_state()
            except Exception:
                pass

        elapsed_h = (datetime.now() - datetime.fromisoformat(sleep_start)).total_seconds() / 3600
        if elapsed_h > 14:
            # Stale night from a long time ago — clean up
            self.log(f"RECOVERY: stale night ({elapsed_h:.0f}h old) — ending and clearing")
            self._end_night()
            self._state["sleep_start"] = None
            self._state["sleep_start_epoch"] = None
            self._save_state()
        else:
            elapsed = self._elapsed_min()
            self.log(f"MID-NIGHT RESTART: resuming active night, elapsed={elapsed:.0f}m")

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

    def _set_l1(self, value):
        value = max(-10, min(MAX_SETTING, int(value)))
        self.call_service("number/set_value", entity_id=E_BEDTIME_TEMP, value=value)
        self._state["last_setting"] = value

    def _read_float(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(state)
        except (ValueError, TypeError):
            return None

    def _read_str(self, entity_id):
        state = self.get_state(entity_id)
        if state in (None, "unavailable"):
            return None
        return state

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

            night_date = datetime.fromisoformat(sleep_start).date()

            # Compute body/ambient stats from this night's controller_readings
            cur.execute("""
                SELECT
                    round(avg(body_avg_f)::numeric, 1),
                    round(avg(ambient_f)::numeric, 1),
                    round(min(ambient_f)::numeric, 1),
                    round(max(ambient_f)::numeric, 1)
                FROM controller_readings
                WHERE zone = 'left'
                  AND ts >= %s::timestamptz
                  AND action IN ('set', 'hold')
                  AND body_avg_f > 74
            """, (sleep_start,))
            stats = cur.fetchone()
            avg_body = float(stats[0]) if stats and stats[0] else None
            avg_ambient = float(stats[1]) if stats and stats[1] else None
            min_ambient = float(stats[2]) if stats and stats[2] else None
            max_ambient = float(stats[3]) if stats and stats[3] else None
            cur.close()

            # Read current L2/L3 for sleep/wake settings
            sleep_setting = self._read_float("number.smart_topper_left_side_sleep_temperature")
            wake_setting = self._read_float("number.smart_topper_left_side_wake_temperature")

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
                 avg_ambient_f, min_ambient_f, max_ambient_f, avg_body_f,
                 override_count, manual_mode, controller_ver,
                 total_sleep_min, deep_sleep_min, rem_sleep_min, core_sleep_min, awake_min)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_date) DO UPDATE SET
                 wake_ts = EXCLUDED.wake_ts,
                 duration_hours = EXCLUDED.duration_hours,
                 bedtime_setting = EXCLUDED.bedtime_setting,
                 sleep_setting = EXCLUDED.sleep_setting,
                 wake_setting = EXCLUDED.wake_setting,
                 avg_ambient_f = EXCLUDED.avg_ambient_f,
                 min_ambient_f = EXCLUDED.min_ambient_f,
                 max_ambient_f = EXCLUDED.max_ambient_f,
                 avg_body_f = EXCLUDED.avg_body_f,
                 override_count = EXCLUDED.override_count,
                 manual_mode = EXCLUDED.manual_mode,
                 controller_ver = EXCLUDED.controller_ver,
                 total_sleep_min = EXCLUDED.total_sleep_min,
                 deep_sleep_min = EXCLUDED.deep_sleep_min,
                 rem_sleep_min = EXCLUDED.rem_sleep_min,
                 core_sleep_min = EXCLUDED.core_sleep_min,
                 awake_min = EXCLUDED.awake_min
            """, (
                night_date,
                sleep_start,
                datetime.now().isoformat(),
                (datetime.now() - datetime.fromisoformat(sleep_start)).total_seconds() / 3600,
                self._state.get("initial_setting", self._state.get("last_setting")),
                int(sleep_setting) if sleep_setting is not None else None,
                int(wake_setting) if wake_setting is not None else None,
                avg_ambient, min_ambient, max_ambient, avg_body,
                self._state.get("override_count", 0),
                self._state.get("manual_mode", False),
                "v4",
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
        """Compute per-cycle adjustments from recent override history.

        Logic: Each manual override tells us the user wanted a different setting.
        We look at which cycle the override happened in and compute the average
        delta (override_value - what_controller_would_have_set) per cycle.

        The adjustments are weighted by recency (LEARNING_DECAY) so recent
        overrides matter more than old ones.
        """
        try:
            conn = self._get_pg()
            if not conn:
                return self._load_learned()
            cur = conn.cursor()

            # Get overrides from last N nights with the effective setting
            # (what the controller actually set, including body/room adjustments)
            cur.execute("""
                SELECT
                    elapsed_min,
                    setting as override_setting,
                    effective as controller_setting,
                    ts::date as night
                FROM controller_readings
                WHERE zone = 'left'
                  AND action = 'override'
                  AND setting IS NOT NULL
                  AND ts > now() - %s * interval '1 day'
                ORDER BY ts
            """, (LEARNING_LOOKBACK_NIGHTS,))

            overrides = cur.fetchall()
            cur.close()
            if not overrides:
                self.log("  Learning: no overrides in history — using defaults")
                return {}

            # Take only the LAST override per cycle per night
            # (user may adjust multiple times — final value is what they wanted)
            last_per_cycle_night = {}  # {(night, cycle): (override_val, controller_val)}
            for elapsed_min, override_val, ctrl_val, night in overrides:
                if elapsed_min is None:
                    continue
                cycle = self._get_cycle_num(elapsed_min)
                baseline = CYCLE_SETTINGS.get(cycle, CYCLE_SETTINGS[max(CYCLE_SETTINGS.keys())])
                # Compare override to baseline (not controller's adjusted value)
                # because learned adj feeds INTO the baseline, avoiding feedback loops
                last_per_cycle_night[(night, cycle)] = override_val - baseline

            if not last_per_cycle_night:
                return {}

            # Group by cycle
            cycle_deltas = {}
            for (night, cycle), delta in sorted(last_per_cycle_night.items()):
                cycle_deltas.setdefault(cycle, []).append(delta)

            # Compute weighted average delta per cycle
            adjustments = {}
            nights = set(n for n, _ in last_per_cycle_night.keys())
            for cycle, deltas in cycle_deltas.items():
                if not deltas:
                    continue
                # Weight recent overrides more (last = weight 1.0, older = decayed)
                weighted_sum = 0
                weight_total = 0
                for i, d in enumerate(reversed(deltas)):
                    w = LEARNING_DECAY ** i
                    weighted_sum += d * w
                    weight_total += w
                avg_delta = weighted_sum / weight_total if weight_total else 0
                adj = max(-LEARNING_MAX_ADJ, min(LEARNING_MAX_ADJ, round(avg_delta)))
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
                    k: max(-LEARNING_MAX_ADJ, min(LEARNING_MAX_ADJ, int(v)))
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
                        cycle_num=None, room_temp_comp=0, data_source="unknown",
                        override_floor=None, body_avg=None, body_left=None,
                        body_right=None, action="set"):
        """Log a controller reading to Postgres."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()

            ambient = self._read_float("sensor.smart_topper_left_side_ambient_temperature")

            # Compute real body average if not provided
            if body_avg is None:
                body_vals = [v for v in (body_left, body_center, body_right) if v is not None]
                body_avg = sum(body_vals) / len(body_vals) if body_vals else body_center

            if cycle_num is None:
                cycle_num = self._get_cycle_num(elapsed_min)

            # Read actual HA entity value as 'effective' (what's really set on device)
            effective_raw = self._read_float(E_BEDTIME_TEMP)
            effective = int(effective_raw) if effective_raw is not None else setting

            # Current learned adjustment for this cycle
            learned_adj = self._learned.get(str(cycle_num), 0)

            # Build notes
            notes = (
                f"cycle={cycle_num} src={data_source} "
                f"room_comp={room_temp_comp:+d}"
            )
            if override_floor is not None:
                notes += f" floor={override_floor:+d}"
            if sleep_stage:
                notes += f" stage={sleep_stage}"

            cur.execute("""
                INSERT INTO controller_readings
                (ts, zone, phase, elapsed_min, body_right_f, body_center_f,
                 body_left_f, body_avg_f, ambient_f, room_temp_f,
                 setting, effective, baseline, learned_adj, action, notes, controller_version)
                VALUES (NOW(), 'left', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'v4')
            """, (
                sleep_stage if sleep_stage and sleep_stage not in ("unknown", "") else f"cycle_{cycle_num}",
                elapsed_min,
                body_right, body_center, body_left,
                body_avg,
                ambient, room_temp,
                setting, effective,
                CYCLE_SETTINGS.get(cycle_num, -5),
                learned_adj,
                action,
                notes,
            ))
            conn.commit()
        except Exception as e:
            self.log(f"Postgres log failed: {e}", level="WARNING")
            try:
                self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None

    def _log_override(self, value, delta=None):
        """Log a manual override to Postgres."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()

            room_temp = self._read_float(E_ROOM_TEMP)
            body = self._read_float(E_BODY_CENTER)
            elapsed = self._elapsed_min()

            cur.execute("""
                INSERT INTO controller_readings
                (ts, zone, phase, elapsed_min, body_center_f, room_temp_f,
                 setting, effective, action, override_delta)
                VALUES (NOW(), 'left', 'override', %s, %s, %s, %s, %s, 'override', %s)
            """, (elapsed, body, room_temp, value, value, delta))
            conn.commit()
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
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
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
        self.log("Controller v4 shutting down — saving state")
        self._save_state()
        if self._pg_conn:
            try:
                self._pg_conn.close()
            except Exception:
                pass
