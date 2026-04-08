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

# Settings per cycle position (-10 to +10 scale, capped at 0)
# These are the BASELINE before room temp compensation
CYCLE_SETTINGS = {
    1: -8,   # First cycle: aggressive cooling for sleep onset
    2: -7,   # Second cycle: still cooling, deep sleep dominant
    3: -5,   # Third cycle: moderate, REM increasing
    4: -4,   # Fourth cycle: light cooling, REM dominant
    5: -3,   # Fifth cycle: approaching morning
    6: -2,   # Sixth cycle: near wake
}
CYCLE_DURATION_MIN = 90

# Room temperature compensation
AMBIENT_REFERENCE_F = 68.0          # Calibration point — 7-day overnight average is 68.3°F
AMBIENT_COMP_PER_F = 0.8            # Base: +0.8 setting per °F below reference
AMBIENT_COLD_THRESHOLD_F = 63.0     # Below this, extra compensation kicks in
AMBIENT_COLD_EXTRA_PER_F = 0.5      # Additional per °F below threshold
MAX_SETTING = 0                     # NEVER heat — user runs warm

# Override handling
OVERRIDE_FREEZE_MIN = 60            # Freeze controller for 1 hour after manual change
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


class SleepControllerV4(hass.Hass):

    def initialize(self):
        self.log("=" * 60)
        self.log("Sleep Controller v4 initializing")

        self._state = {
            "sleep_start": None,        # When sleep began (ISO string)
            "last_setting": None,       # Current L1 value
            "last_change_ts": None,     # When we last changed L1
            "override_floor": None,     # User's last manual value (floor)
            "override_freeze_until": None,
            "manual_mode": False,       # Kill switch triggered
            "recent_changes": [],       # Timestamps of recent manual changes
            "body_below_since": None,   # When body first dropped below BODY_EMPTY_THRESHOLD_F
        }
        self._load_state()
        self._pg_conn = None

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
        self.log(f"  Room temp: {E_ROOM_TEMP}")
        self.log(f"  Max setting: {MAX_SETTING}")
        self.log("=" * 60)

    # ── Main Control Loop ────────────────────────────────────────────

    def _control_loop(self, kwargs):
        now = datetime.now()

        # Not sleeping? Nothing to do.
        if not self._is_sleeping():
            return

        # Responsive cooling watchdog — ALWAYS runs, even during freeze.
        # This is safety-critical: firmware stability depends on it.
        rc_state = self.get_state(E_RESPONSIVE_COOLING)
        if rc_state == "off":
            self.log("WARNING: Responsive cooling was OFF — re-enabling", level="WARNING")
            self.call_service("switch/turn_on", entity_id=E_RESPONSIVE_COOLING)

        # Manual mode (kill switch) — hands off for the night
        if self._state["manual_mode"]:
            return

        # Override freeze — respect the user
        freeze_until = self._state.get("override_freeze_until")
        if freeze_until:
            freeze_dt = datetime.fromisoformat(freeze_until)
            if now < freeze_dt:
                remaining = (freeze_dt - now).total_seconds() / 60
                self.log(f"Override freeze — {remaining:.0f}m remaining")
                return
            else:
                self._state["override_freeze_until"] = None
                self.log("Override freeze expired")

        # Rate limit — don't change more than once per MIN_CHANGE_INTERVAL_SEC
        last_change = self._state.get("last_change_ts")
        if last_change:
            elapsed = (now - datetime.fromisoformat(last_change)).total_seconds()
            if elapsed < MIN_CHANGE_INTERVAL_SEC:
                return

        # Compute the right setting
        room_temp = self._read_float(E_ROOM_TEMP)
        sleep_stage = self._read_str(E_SLEEP_STAGE)
        body_center = self._read_float(E_BODY_CENTER)

        # Gap 1: Robust occupancy detection
        if not self._check_occupancy(body_center, now):
            return

        sleep_start = self._state.get("sleep_start")
        if not sleep_start:
            return

        elapsed_min = (now - datetime.fromisoformat(sleep_start)).total_seconds() / 60

        # Determine setting from sleep cycle position
        setting, room_temp_comp, data_source = self._compute_setting(elapsed_min, room_temp, sleep_stage)

        # Apply override floor — never go colder than user's last override
        floor = self._state.get("override_floor")
        if floor is not None and setting < floor:
            self.log(f"Override floor: computed {setting:+d} < floor {floor:+d}, using floor")
            setting = floor

        # Apply
        current = self._read_float(E_BEDTIME_TEMP)
        if current is not None and int(current) != setting:
            cycle_num = self._get_cycle_num(elapsed_min)
            self.log(
                f"Cycle {cycle_num}, "
                f"elapsed={elapsed_min:.0f}m, room={room_temp}°F, "
                f"stage={sleep_stage}, src={data_source}, "
                f"room_comp={room_temp_comp:+d}: "
                f"{int(current):+d} → {setting:+d}"
            )
            self._set_l1(setting)
            self._state["last_change_ts"] = now.isoformat()
            self._log_to_postgres(
                elapsed_min, room_temp, sleep_stage, body_center, setting,
                cycle_num=cycle_num, room_temp_comp=room_temp_comp,
                data_source=data_source, override_floor=floor,
            )
            self._save_state()

    def _compute_setting(self, elapsed_min, room_temp, sleep_stage):
        """Compute L1 setting from cycle position + room temp.

        Returns (setting, room_temp_comp, data_source) where:
          room_temp_comp: degrees of room temp compensation applied
          data_source: 'stage' or 'time_cycle'
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

        # Room temperature compensation — physics, always applied
        if room_temp is not None:
            delta = AMBIENT_REFERENCE_F - room_temp
            if delta > 0:
                room_temp_comp = delta * AMBIENT_COMP_PER_F
                if room_temp < AMBIENT_COLD_THRESHOLD_F:
                    room_temp_comp += (AMBIENT_COLD_THRESHOLD_F - room_temp) * AMBIENT_COLD_EXTRA_PER_F
                room_temp_comp = round(room_temp_comp)
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

    # ── Sleep Mode ───────────────────────────────────────────────────

    def _on_sleep_mode(self, entity, attribute, old, new, kwargs):
        if new == "on" and old != "on":
            self.log("Sleep mode ON — starting night")
            self._state["sleep_start"] = datetime.now().isoformat()
            self._state["manual_mode"] = False
            self._state["override_floor"] = None
            self._state["override_freeze_until"] = None
            self._state["recent_changes"] = []
            self._state["last_change_ts"] = None
            self._state["body_below_since"] = None
            # Gap 2: Compute cycle 1 with room temp compensation
            # If pre-cool had the topper at -10, the firmware's responsive cooling
            # will handle the transition smoothly — just set our target.
            room_temp = self._read_float(E_ROOM_TEMP)
            initial_setting, comp, _ = self._compute_setting(0, room_temp, None)
            self.log(f"  Initial L1={initial_setting:+d} (room={room_temp}°F, comp={comp:+d})")
            self._set_l1(initial_setting)
            self._state["last_setting"] = initial_setting
            self._save_state()

        elif new == "off" and old == "on":
            self.log("Sleep mode OFF — ending night")
            self._end_night()
            self._state["sleep_start"] = None
            self._state["manual_mode"] = False
            self._state["override_floor"] = None
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

        # Was this change made by US?
        expected = self._state.get("last_setting")
        if expected is not None and new_val == expected:
            return  # We made this change, ignore

        # This is a manual override
        now = datetime.now()
        self.log(f"MANUAL OVERRIDE: {old_val:+d} → {new_val:+d}")

        # Record for kill switch detection
        self._state["recent_changes"].append(now.timestamp())
        self._check_kill_switch()

        # Set freeze and floor
        self._state["override_freeze_until"] = (
            now + timedelta(minutes=OVERRIDE_FREEZE_MIN)
        ).isoformat()
        self._state["override_floor"] = new_val
        self._state["last_setting"] = new_val

        self._log_override(new_val)
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

    def _check_occupancy(self, body_center, now):
        """Return True if someone is in bed, False if empty.

        Logic: sleep_mode is ON (already checked by caller).
        - body_center > 74°F → occupied (handles the 70→80 ramp during getting in)
        - body_center < 72°F for 20+ min → empty (bathroom break / woke up)
        - body_center is None → assume occupied (sensor glitch, don't skip)
        """
        if body_center is None:
            return True

        if body_center >= BODY_OCCUPIED_THRESHOLD_F:
            self._state["body_below_since"] = None
            return True

        if body_center < BODY_EMPTY_THRESHOLD_F:
            below_since = self._state.get("body_below_since")
            if below_since is None:
                self._state["body_below_since"] = now.isoformat()
                return True  # Just dropped — give it time
            elapsed = (now - datetime.fromisoformat(below_since)).total_seconds() / 60
            if elapsed >= BODY_EMPTY_TIMEOUT_MIN:
                self.log(f"Bed appears empty — body={body_center:.1f}°F for {elapsed:.0f}m")
                return False
            return True  # Not long enough yet

        # Between EMPTY (72) and OCCUPIED (74) — hysteresis zone, assume occupied
        return True

    def _check_midnight_restart(self):
        """Gap 5: If AppDaemon restarts while sleeping, resume from correct position."""
        if not self._is_sleeping():
            return

        if self._state.get("sleep_start"):
            elapsed = (datetime.now() - datetime.fromisoformat(self._state["sleep_start"])).total_seconds() / 60
            self.log(f"MID-NIGHT RESTART: sleep_mode is ON, "
                     f"sleep_start from state file, elapsed={elapsed:.0f}m")
            return

        # No sleep_start in state — try to recover from HA entity last_changed
        try:
            last_changed = self.get_state(E_SLEEP_MODE, attribute="last_changed")
            if last_changed:
                self._state["sleep_start"] = last_changed
                elapsed = (datetime.now() - datetime.fromisoformat(last_changed)).total_seconds() / 60
                self.log(f"MID-NIGHT RESTART: Recovered sleep_start from "
                         f"last_changed={last_changed}, elapsed={elapsed:.0f}m")
                self._save_state()
            else:
                self.log("MID-NIGHT RESTART: sleep_mode ON but cannot determine start time",
                         level="WARNING")
        except Exception as e:
            self.log(f"MID-NIGHT RESTART: Failed to recover sleep_start: {e}",
                     level="WARNING")

    # ── Helpers ──────────────────────────────────────────────────────

    def _is_sleeping(self):
        state = self.get_state(E_SLEEP_MODE)
        return state == "on"

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
        """Log nightly summary to Postgres."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()

            sleep_start = self._state.get("sleep_start")
            if not sleep_start:
                return

            # Read Apple Health sleep totals
            deep = self._read_float("input_number.apple_health_sleep_deep_hrs")
            rem = self._read_float("input_number.apple_health_sleep_rem_hrs")
            core = self._read_float("input_number.apple_health_sleep_core_hrs")
            awake = self._read_float("input_number.apple_health_sleep_awake_hrs")

            cur.execute("""
                INSERT INTO nightly_summary
                (night_date, bedtime_ts, wake_ts, duration_hours,
                 bedtime_setting, override_count, manual_mode, controller_ver,
                 total_sleep_min, deep_sleep_min, rem_sleep_min, core_sleep_min, awake_min)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_date) DO UPDATE SET
                 wake_ts = EXCLUDED.wake_ts,
                 duration_hours = EXCLUDED.duration_hours,
                 bedtime_setting = EXCLUDED.bedtime_setting,
                 override_count = EXCLUDED.override_count,
                 manual_mode = EXCLUDED.manual_mode,
                 controller_ver = EXCLUDED.controller_ver,
                 total_sleep_min = EXCLUDED.total_sleep_min,
                 deep_sleep_min = EXCLUDED.deep_sleep_min,
                 rem_sleep_min = EXCLUDED.rem_sleep_min,
                 core_sleep_min = EXCLUDED.core_sleep_min,
                 awake_min = EXCLUDED.awake_min
            """, (
                datetime.fromisoformat(sleep_start).date(),
                sleep_start,
                datetime.now().isoformat(),
                (datetime.now() - datetime.fromisoformat(sleep_start)).total_seconds() / 3600,
                self._state.get("last_setting"),
                len(self._state.get("recent_changes", [])),
                self._state.get("manual_mode", False),
                "v4",
                ((deep or 0) + (rem or 0) + (core or 0)) * 60 if deep else None,
                (deep or 0) * 60 if deep else None,
                (rem or 0) * 60 if rem else None,
                (core or 0) * 60 if core else None,
                (awake or 0) * 60 if awake else None,
            ))
            conn.commit()
            self.log("Nightly summary saved to Postgres")
        except Exception as e:
            self.log(f"Failed to save nightly summary: {e}", level="WARNING")

    def _log_to_postgres(self, elapsed_min, room_temp, sleep_stage, body_center, setting,
                        cycle_num=None, room_temp_comp=0, data_source="unknown",
                        override_floor=None):
        """Log a controller reading to Postgres."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()

            body_left = self._read_float(E_BODY_LEFT)
            body_right = self._read_float(E_BODY_RIGHT)
            ambient = self._read_float("sensor.smart_topper_left_side_ambient_temperature")

            if cycle_num is None:
                cycle_num = self._get_cycle_num(elapsed_min)

            # Build notes with Gap 4 metadata
            notes = (
                f"cycle={cycle_num} src={data_source} "
                f"room_comp={room_temp_comp:+d}"
            )
            if override_floor is not None:
                notes += f" floor={override_floor:+d}"

            cur.execute("""
                INSERT INTO controller_readings
                (ts, zone, phase, elapsed_min, body_right_f, body_center_f,
                 body_left_f, body_avg_f, ambient_f, room_temp_f,
                 setting, effective, baseline, action, notes)
                VALUES (NOW(), 'left', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                sleep_stage or f"cycle_{cycle_num}",
                elapsed_min,
                body_right, body_center, body_left,
                body_center,
                ambient, room_temp,
                setting, setting,
                CYCLE_SETTINGS.get(cycle_num, -5),
                "set",
                notes,
            ))
            conn.commit()
        except Exception as e:
            self.log(f"Postgres log failed: {e}", level="WARNING")
            self._pg_conn = None

    def _log_override(self, value):
        """Log a manual override to Postgres."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()

            room_temp = self._read_float(E_ROOM_TEMP)
            body = self._read_float(E_BODY_CENTER)
            sleep_start = self._state.get("sleep_start")
            elapsed = 0
            if sleep_start:
                elapsed = (datetime.now() - datetime.fromisoformat(sleep_start)).total_seconds() / 60

            cur.execute("""
                INSERT INTO controller_readings
                (ts, zone, phase, elapsed_min, body_center_f, room_temp_f,
                 setting, effective, action, override_delta)
                VALUES (NOW(), 'left', 'override', %s, %s, %s, %s, %s, 'override', %s)
            """, (elapsed, body, room_temp, value, value, value))
            conn.commit()
        except Exception as e:
            self.log(f"Override log failed: {e}", level="WARNING")
            self._pg_conn = None

    def _get_pg(self):
        """Get or create Postgres connection."""
        if self._pg_conn is not None:
            try:
                self._pg_conn.cursor().execute("SELECT 1")
                return self._pg_conn
            except Exception:
                self._pg_conn = None
        try:
            import psycopg2
            self._pg_conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASS,
                connect_timeout=5,
            )
            self._ensure_schema(self._pg_conn)
            return self._pg_conn
        except Exception as e:
            self.log(f"Postgres connect failed: {e}", level="WARNING")
            return None

    def _ensure_schema(self, conn):
        """Add notes column to controller_readings if missing."""
        try:
            cur = conn.cursor()
            cur.execute("""
                ALTER TABLE controller_readings ADD COLUMN IF NOT EXISTS notes TEXT
            """)
            conn.commit()
        except Exception:
            conn.rollback()

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
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
