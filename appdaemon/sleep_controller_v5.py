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
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import hassapi as hass

# Add the apps/ directory to sys.path so ml.* (policy, features) can be
# imported by the shadow-mode logger. This mirrors v3/v4's pattern.
_project_root = Path(__file__).parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

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
#   Pre-sleep/in-bed: intentional pre-cooling is allowed before sleep onset
#   Cycle 1 (0-90 min): cool-biased baseline → body feedback may ease off
#   Cycle 2 (90-180 min): cool-biased baseline → body feedback may ease off
#   Cycle 3-4 (180-360 min): light cool → REM increases, overcooling wakes
#   Cycle 5+ (360+ min): minimal cool → approaching wake, body warming up

# Settings per cycle position (-10 to +10 scale, capped at 0).
# Under RC-off these act as fixed blower-proxy baselines, so stay colder than v4.
CYCLE_SETTINGS = {
    # v5.1 baselines — refit from 49 left-zone overrides across 30 nights, motivated
    # by 2026-04-30 user report ("cold mid-night, slightly warm in the morning").
    # c1, c3-c5 from posterior-mean shrinkage (prior_n=5); c2 reverted to v5's
    # value after counterfactual replay (tools/replay_v51_vs_v5.py) showed the
    # shrunk c2=-8 was the WORST integer choice for hit-rate (0/7), because the
    # c2 user_pref distribution is bimodal: [-10,-10,-10,-10,-6,-6,-4]. The
    # mean (-8) falls in a no-man's-land between the cooling cluster and the
    # mild cluster. c2=-10 matches the dominant cluster (4/7) and recovers
    # hit-rate. c6 manually dipped one step cooler than v5 to address late-cycle
    # overheat reports that don't trigger overrides (mild pre-wake heat).
    # Override corpus per-cycle means (n, mean):
    # c1=(11,-9.27) c2=(7,-8.0) c3=(10,-6.0) c4=(5,-3.0) c5=(7,-2.86) c6=(9,-3.11).
    # See _archive/v5_1_baseline_fit_2026-04-30.md.
    1: -10,  # Cool-biased early-sleep prior. Not forced: body feedback is active
             # from cycle 1 and can warm this immediately when body_left is cool.
    2: -10,  # was -9 (v5), then -8 (initial v5.1); reverted to -10 after hit-rate
             # replay showed bimodal preference dominated by [-10,-10,-10,-10] cluster.
             # Body feedback is active here too, so this is no longer forced cooling.
    3:  -7,  # Was -8; shrunken posterior over 10 overrides (mean -6.0).
    4:  -5,  # Was -7; shrunken posterior over 5 overrides (mean -3.0). Largest single
             # change; addresses 2026-04-30 "cold in the middle of the night" report.
    5:  -5,  # Was -6; capped at -5 (prior_n=5 says -4) to avoid easing too far before
             # the c6 cooldown; n=7 in this cycle, LOOCV unstable.
    6:  -6,  # Was -5; intentional non-monotonic dip — pre-wake active cooldown to
             # address 2026-04-30 "slightly warm later in the morning" report.
}
CYCLE_DURATION_MIN = 90

# ── Body-temperature feedback (v5.2) ─────────────────────────────────
# After the cycle baseline is set, apply a closed-loop correction based on
# the actual body sensor reading. Cycle baselines are open-loop (time only);
# adding body feedback closes the loop on the controlled variable that
# matters (skin temp). Fit by counterfactual replay over 41 left-zone
# overrides; held-out LOOCV MAE drops from 3.116 (v5.1) to 1.633 (v5.2).
#
# Mechanics: when body_avg_f is BELOW BODY_FB_TARGET, shift the cycle
# baseline by Kp*(target-body) settings warmer (less cooling). No correction
# applied above target — the safety rails (`hot_safety`, `overheat_hard`,
# right-zone rail) handle hot-side. Applies from cycle 1 once sleep is active;
# only explicit pre-sleep/in-bed stage labels preserve intentional pre-cooling.
#
# Sweep evidence (n=41 overrides, 30 nights):
#   v5  : MAE=3.024 hit=26.8% bias=-2.00 (open-loop, monotonic baselines)
#   v5.1: MAE=2.683 hit=31.7% bias=-1.71 (refit baselines)
#   v5.2: MAE=1.732 hit=61.0% bias=+0.32 (refit + body feedback)
#   LOOCV: v5.2=1.633 vs v5.1=3.116 (-48% on held-out)
BODY_FB_ENABLED = True
BODY_FB_INPUT = "body_left"   # which sensor feeds the closed loop; body_left
                              # is the skin-contact channel and matches the
                              # right-zone controller's input. Was body_avg
                              # which mixed in warm-sheet body_center.
BODY_FB_TARGET_F = 80.0       # below this body_left_f, ease off cooling.
                              # Matches right-zone target. Both sleepers'
                              # body_left p50 ≈ 79.5°F (user 79.3, wife 79.7),
                              # so 80°F is the natural cross-zone reference.
BODY_FB_KP_COLD = 1.25        # settings warmer per °F below target. Larger
                              # than the body_avg-fit value (0.55) because
                              # body_left runs ~3°F cooler than body_avg, so
                              # the deltas are smaller — Kp scales accordingly.
                              # Refit MAE 1.81, LOOCV 1.51 (was v5.2 body_avg
                              # MAE 1.73, LOOCV 1.63).
BODY_FB_MAX_DELTA = 5         # cap upward correction (warmer-shift) from baseline
BODY_FB_MIN_CYCLE = 1         # apply from cycle 1 once sleep is active; explicit
                              # inbed/awake pre-sleep stages preserve pre-cooling

# ── Right-zone v5.2 shadow controller (her side, log-only) ───────────
# The wife's right zone runs in firmware Responsive Cooling mode (RC modulates
# blower toward a setpoint determined by `setting`). Her n=6 overrides skew
# COOLER-please (4 of 6) at body 76-86°F, and the rich notes column shows
# firmware blower=0% in 3 of 4 cooler-please events: RC was static while she
# was warm. That's the gap a body-feedback controller could close.
#
# This block computes what a right-zone v5.2 WOULD set at each tick and writes
# it to /config/snug_right_v52_shadow.jsonl alongside the firmware actual.
# NO HA service calls, NO actuation. Pure observability — once 1-2 nights of
# shadow data show the decisions are sensible, we flip a flag to enable real
# control.
#
# Parameters fit by sweep on her 6 overrides (best of {target, Kp_hot, Kp_cold,
# baselines, cap}; n=6 is too thin for trustworthy fitting — these are
# educated starting points, not validated).
RIGHT_SHADOW_ENABLED = True   # flip false to silence shadow logger entirely
RIGHT_CYCLE_SETTINGS = {
    1: -8,   # gentler than user's -10; her firmware default is -5 to -6
    2: -7,
    3: -6,
    4: -5,
    5: -5,
    6: -5,
}
RIGHT_BODY_FB_TARGET_F = 80.0
RIGHT_BODY_FB_KP_HOT  = 0.5   # body warm → cooler (her dominant complaint)
RIGHT_BODY_FB_KP_COLD = 0.3   # body cool → slightly warmer
RIGHT_BODY_FB_MAX_DELTA = 4   # tighter cap than left (less data)
RIGHT_BODY_FB_SKIP_CYCLES = ()
RIGHT_BEDJET_WINDOW_MIN = 30.0  # match right_overheat_safety.BEDJET_SUPPRESS_MIN
                                 # — body sensors inflate during the wife's
                                 # warm-blanket pre-warm; suppress corrections
                                 # during this window.
RIGHT_SHADOW_LOG_PATH = "/config/snug_right_v52_shadow.jsonl"

# Apple Health can report pre-sleep bed occupancy before actual sleep onset.
# Preserve intentional pre-cooling during those explicit not-yet-asleep states;
# once the stage is asleep/deep/core/rem (or unknown elapsed-sleep control), the
# early-cycle body feedback is allowed to ease off cooling immediately.
PRE_SLEEP_STAGE_VALUES = ("inbed", "awake")

# User-stated preference (2026-05-01): intentionally pre-cool before getting
# into bed, then keep maximum/aggressive cooling for the first ~30 minutes after
# bed-occupancy onset. This explicit occupancy-based gate overrides body
# feedback and room compensation; after it expires, body feedback may apply from
# cycle 1.
INITIAL_BED_COOLING_MIN = 30.0
INITIAL_BED_LEFT_SETTING = -10
INITIAL_BED_RIGHT_SETTING = -10
BED_ONSET_CLEAR_DEBOUNCE_MIN = 10.0

# Right-zone live actuation. TWO-KEY ARMING:
#   1. RIGHT_LIVE_ENABLED Python constant (this file) — armed by code
#   2. input_boolean.snug_right_controller_enabled — armed by HA UI (default off)
# BOTH must be true for the controller to write to her bedtime entity. Either
# one false → shadow only, no actuation. The HA helper is the operational
# kill switch (instant toggle from UI, no redeploy). Code default: armed
# in code so the user can enable/disable from HA UI without a redeploy. The
# HA helper defaults OFF, so the system is safe by default — flip the UI
# helper to enable live control when ready.
RIGHT_LIVE_ENABLED = True
E_RIGHT_CONTROLLER_FLAG = "input_boolean.snug_right_controller_enabled"
E_BEDTIME_TEMP_RIGHT = "number.smart_topper_right_side_bedtime_temperature"
RIGHT_MIN_CHANGE_INTERVAL_SEC = 1800   # 30 min between live writes
RIGHT_OVERRIDE_FREEZE_MIN = 60          # 1 hour freeze after manual change

# ── Control Model ────────────────────────────────────────────────────
CONTROLLER_VERSION = "v5_2_rc_off"
# CONTROLLER_PATCH_LEVEL is a finer-grained marker for in-place patches that
# must not reset the cross-night learner (which filters PG history on
# CONTROLLER_VERSION). Tonight's 2026-05-01 patch set:
#   - override floor removed (override is a learning event, not a night-long floor)
#   - 3-level mode forced OFF as a watchdog (init + every control loop tick)
#   - right-zone proactive-cool: -1 step when body_left ≥ 86°F sustained ≥10 min,
#     gated off during BedJet/initial-bed/unoccupied windows
#   - bed-onset event listener schedules an immediate control tick
#   - body feedback is gated off when bed presence says the bed is unoccupied
#   - right-zone hot-rail source is included in passive PG snapshot notes
# See PROGRESS_REPORT.md and docs/2026-05-01_v52_patches.md for detail.
CONTROLLER_PATCH_LEVEL = "v5_2_rc_off+noFloor+3levelWatchdog+rightHotRail86+bedOnsetEvent+bodyFbOccGate+hotRailNotes+overheatBypass+rcBothZones+railNotOverride+railRestoreGuard+leftSelfWrite+bodyFbFailClosed+learnerInitialBedExclude"
ENABLE_LEARNING = True
MAX_SETTING = 0

L1_TO_BLOWER_PCT = {
    # IMPORTANT (2026-05-01 finding): this is the RC-OFF blower percentage for
    # each user setting, validated by the empty-bed step-response test where
    # blower output exactly equaled this table for the active setting (RC=on
    # but no body present, so RC had nothing to modulate against).
    #
    # When RC is on AND the bed is occupied, the firmware modulates the
    # blower BELOW these values. CORRECTED (2026-05-01 data audit, n=43,827
    # occupied RC-on rows): mean delta = -21 pts, median = -18, std = 22.
    # The earlier "-45" figure was inflated by including running=off rows
    # (integration-injected zeros) and unoccupied rows. So this table is
    # the *RC-off baseline*, not what the firmware actually outputs in
    # normal operation. Any code that needs "expected blower under RC"
    # should NOT use this table directly.
    #
    # ALSO: the lookup must be against L_active (the user dial that is
    # actually live given run_progress / 3-level mode), not L1
    # (bedtime_temperature). When 3_level_mode=on the firmware advances
    # L1 → L2 → L3 by run_progress; using L1 alone misattributes most of
    # the night to the wrong dial. See
    # docs/findings/2026-05-01_data_audit_labels.md (§5) and
    # PerfectlySnug/tools/lib_active_setting.py for the L_active helper.
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

# Room compensation happens in blower space now. Anchor at a neutral/warm room
# (72°F); below that, progressively reduce cooling, above it add cooling.
ROOM_BLOWER_REFERENCE_F = 72.0
ROOM_BLOWER_COLD_COMP_PER_F = 4.0
ROOM_BLOWER_COLD_THRESHOLD_F = 63.0
ROOM_BLOWER_COLD_EXTRA_PER_F = 3.0
ROOM_BLOWER_HOT_COMP_PER_F = 4.0

# Right-zone room compensation uses the same physical room reference as the
# left zone (72°F), but a wife-specific multiplier/policy.  Start conservative:
# only add cooling when the room is warmer than the shared reference.  Do NOT
# apply cold-room warming yet; last night's only right-side override was colder
# (-4→-5) at ~68°F, so warming below 72°F would contradict fresh evidence.
RIGHT_ROOM_BLOWER_REFERENCE_F = ROOM_BLOWER_REFERENCE_F
RIGHT_ROOM_BLOWER_HOT_COMP_PER_F = 4.0
RIGHT_ROOM_BLOWER_COLD_COMP_PER_F = 0.0

# Body handling stays conservative: only act on clear hot extremes.
BODY_HOT_THRESHOLD_F = 85.0
BODY_HOT_STREAK_COUNT = 2
BODY_TEMP_MIN_F = 55.0
BODY_TEMP_MAX_F = 110.0

# Hard-overheat rail (separate from BODY_HOT_THRESHOLD step-colder logic).
# At sustained body ≥90°F we jump straight to max cooling instead of climbing
# one step at a time. Historical analysis (14 nights) shows body_left_f
# never reached 90°F (max 88.6°F), so this rail is a future-only safety net
# for the left zone — it is gated behind input_boolean.snug_overheat_rail_enabled
# so it can be disabled instantly if it ever misbehaves.
OVERHEAT_HARD_F = 90.0
OVERHEAT_HARD_STREAK = 2            # consecutive readings before firing
OVERHEAT_HARD_RELEASE_F = 86.0      # hysteresis: stay engaged until we drop below
E_OVERHEAT_RAIL_FLAG = "input_boolean.snug_overheat_rail_enabled"

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
E_RESPONSIVE_COOLING_RIGHT = "switch.smart_topper_right_side_responsive_cooling"
E_PROFILE_3LEVEL = "switch.smart_topper_left_side_3_level_mode"
E_PROFILE_3LEVEL_RIGHT = "switch.smart_topper_right_side_3_level_mode"

# Right-zone proactive-cool rail (override-absence trap mitigation, 2026-05-01).
# When body_left on the right zone stays ≥ this threshold for ≥ this many ticks
# AND we're not in BedJet/initial-bed/unoccupied/sensor-missing windows, bias
# the proposed right-side setting one step cooler. Threshold 86°F (not 84°F)
# because the existing right body feedback (Kp_hot=0.5 × delta from 80°F target)
# already corrects -2 at body=84°F; this rail is the next escalation.
RIGHT_HOT_RAIL_F = 86.0
RIGHT_HOT_RAIL_STREAK = 2          # 2 consecutive ticks (≈10 min @ 5 min cycles)
RIGHT_HOT_RAIL_BIAS = -1           # one step cooler when streak triggers
RIGHT_RAIL_RELEASE_F = 82.0         # standalone rail releases below this body_left°F
RIGHT_RAIL_RELEASE_TOLERANCE_F = 2.0
RIGHT_RAIL_MAX_ENGAGE_SEC = 6 * 60 * 60
E_RIGHT_OVERHEAT_RAIL_FLAG = "input_boolean.snug_right_overheat_rail_enabled"
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
            # NOTE 2026-05-01: override_floor removed. Manual overrides are now
            # treated as learning data points (cross-night via _learn_from_history)
            # rather than night-long floors. The 60-min freeze still prevents
            # immediate re-fighting of a fresh user input.
            "manual_mode": False,       # Kill switch triggered
            "recent_changes": [],       # Timestamps of recent manual changes
            "override_count": 0,        # Total overrides this night
            "body_below_since": None,   # When body first dropped below BODY_EMPTY_THRESHOLD_F
            "hot_streak": 0,
            "right_hot_streak": 0,      # 2026-05-01: right-zone 86°F sustained counter
            "right_rail_force_seen": False,
            "right_rail_force_seen_at": None,
            "current_cycle_num": None,
            "left_bed_onset_ts": None,
            "right_bed_onset_ts": None,
            "left_bed_vacated_since": None,
            "right_bed_vacated_since": None,
        }
        self._load_state()
        self._pg_conn = None
        self._learned = self._load_learned()

        # Force responsive cooling OFF on BOTH sides — v5 owns the outer proxy model.
        self._ensure_responsive_cooling_off(force=True)
        # Force 3-level mode OFF on BOTH sides at boot. User runs single-stage L1.
        self._ensure_3_level_off()

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

        # Bed-presence onset listeners: schedule a near-immediate algorithm tick
        # on off/unavailable→on transitions without firing at AppDaemon startup.
        for zone in ("left", "right"):
            self.listen_state(
                self._on_bed_onset,
                BED_PRESENCE_ENTITIES[f"occupied_{zone}"],
                new="on",
                zone=zone,
            )
            self.listen_state(
                self._on_bed_vacated,
                BED_PRESENCE_ENTITIES[f"occupied_{zone}"],
                new="off",
                zone=zone,
            )

        # Gap 5: If sleep_mode is already ON (mid-night restart), resume
        self._check_midnight_restart()

        self.log(f"Controller {CONTROLLER_PATCH_LEVEL} ready — left side in RC-off blower-proxy mode")
        self.log(f"  Body feedback: enabled={BODY_FB_ENABLED}, "
                 f"target={BODY_FB_TARGET_F}°F, Kp_cold={BODY_FB_KP_COLD}, "
                 f"max_delta={BODY_FB_MAX_DELTA}, min_cycle={BODY_FB_MIN_CYCLE}")
        self.log(f"  Right-zone shadow: enabled={RIGHT_SHADOW_ENABLED}, "
                 f"live={RIGHT_LIVE_ENABLED}, "
                 f"baselines={list(RIGHT_CYCLE_SETTINGS.values())}, "
                 f"target={RIGHT_BODY_FB_TARGET_F}°F, "
                 f"Kp_hot={RIGHT_BODY_FB_KP_HOT}, Kp_cold={RIGHT_BODY_FB_KP_COLD}, "
                 f"room_ref={RIGHT_ROOM_BLOWER_REFERENCE_F}°F, "
                 f"room_hot={RIGHT_ROOM_BLOWER_HOT_COMP_PER_F}/°F, "
                 f"room_cold={RIGHT_ROOM_BLOWER_COLD_COMP_PER_F}/°F")
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

        # Responsive cooling watchdog — keep both sides in RC-off mode.
        self._ensure_responsive_cooling_off()
        # 3-level mode watchdog — keep BOTH sides in single-stage L1 mode
        # so L_active == L1 and the controller's writes apply to the live dial.
        self._ensure_3_level_off()

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

        # Occupancy detection. Bed-presence, when available, is the source for
        # the initial-bed cooling window; body sensors remain the fallback for
        # empty-bed detection and older data.
        body_occupied = self._check_occupancy(body_max, now)
        left_mins_since_onset = self._update_zone_occupancy_onset(
            "left",
            self._zone_occupied_from_bed_presence(
                bed_presence, "left", fallback=body_occupied
            ),
            now,
        )
        if not body_occupied:
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
            body_left=body_left,
            current_setting=current,
            mins_since_occupied=left_mins_since_onset,
            bed_occupied=bed_presence.get("occupied_left") if isinstance(bed_presence, dict) else None,
        )
        setting = plan["setting"]
        target_blower_pct = plan["target_blower_pct"]
        base_setting = plan["base_setting"]
        base_blower_pct = plan["base_blower_pct"]
        cycle_num = plan["cycle_num"]
        room_temp_comp = plan["room_temp_comp"]
        learned_adj_pct = plan["learned_adj_pct"]
        data_source = plan["data_source"]

        # NOTE 2026-05-01: override floor removed. We no longer clamp `setting`
        # to a night-long floor based on the user's last manual change; manual
        # changes are learning events (consumed cross-night via
        # _learn_from_history) and the 60-min freeze below still prevents
        # immediate re-fighting of a fresh user input.

        # ── Determine if we can change the setting ────────────────────
        changed = int(current) != setting
        blocked = False
        action = "hold"
        write_applied = False
        overheat_hard_plan = bool(plan.get("overheat_hard"))
        hot_safety_plan = bool(plan.get("hot_safety"))
        manual_mode_active = bool(self._state["manual_mode"])

        if manual_mode_active:
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
        safety_bypass = changed and (
            overheat_hard_plan or (hot_safety_plan and not manual_mode_active)
        )

        # Apply setting change if allowed and needed. Hard-overheat is a safety
        # rail and must bypass freeze/manual/rate-limit; hot_safety bypasses
        # freeze/rate-limit but deliberately respects the manual-mode kill switch.
        if safety_bypass:
            if overheat_hard_plan:
                self.log(
                    f"OVERHEAT_HARD bypass: forcing L1={setting:+d} "
                    "despite freeze/manual/rate-limit",
                    level="WARNING",
                )
                action = "overheat_hard"
            else:
                self.log(
                    f"HOT_SAFETY bypass: forcing L1={setting:+d} "
                    "despite freeze/rate-limit",
                    level="WARNING",
                )
                action = "hot_safety"
            self._set_l1(setting)
            logged_blower_pct = None
            self._state["last_change_ts"] = now.isoformat()
            self._state["last_target_blower_pct"] = target_blower_pct
            self._save_state()
            write_applied = True
        elif changed and not blocked:
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
            write_applied = True
            action = "set"
        elif not changed and overheat_hard_plan:
            action = "overheat_hard"
        elif not changed and hot_safety_plan and not manual_mode_active:
            action = "hot_safety"

        # ── Always log to Postgres — no telemetry gaps ────────────────
        self._log_to_postgres(
            elapsed_min, room_temp, sleep_stage, body_center, setting,
            cycle_num=cycle_num, room_temp_comp=room_temp_comp,
            data_source=data_source, override_floor=None,
            body_avg=body_avg, body_left=body_left, body_right=body_right,
            action=action, ambient=ambient, setpoint=setpoint,
            effective=setting if write_applied else int(current),
            baseline=base_setting, learned_adj=learned_adj_pct,
            blower_pct=logged_blower_pct, target_blower_pct=target_blower_pct,
            base_blower_pct=base_blower_pct, responsive_cooling_on=False,
            bed_presence=bed_presence,
        )
        right_snap = self._read_zone_snapshot("right")
        right_v52_entry = None
        # ── Right-zone v5.2 shadow: log decision alongside firmware actual ──
        # Run before the passive PG row so rail/source annotations can flow into
        # controller_readings.notes for right-zone monitoring queries.
        if RIGHT_SHADOW_ENABLED:
            right_v52_entry = self._right_v52_shadow_tick(
                elapsed_min=elapsed_min, room_temp=room_temp,
                sleep_stage=sleep_stage, right_snap=right_snap,
            )
        right_source = None
        if right_v52_entry and right_v52_entry.get("hot_rail_fired"):
            right_source = right_v52_entry.get("right_v52_source")
        self._log_passive_zone_snapshot(
            "right", elapsed_min, room_temp, sleep_stage, bed_presence=bed_presence,
            snapshot=right_snap, data_source_suffix=right_source,
        )

        # ── Shadow-mode: log what ml.policy.controller_decision would set ──
        # No hardware effect; pure observability for both zones. We use this to
        # compare the proposed new policy against v5 over real nights before
        # deciding to wire it up live.
        self._shadow_log_decision(
            zone="left", elapsed_min=elapsed_min, room_temp_f=room_temp,
            body_f=body_left, v5_setting=setting,
        )
        if right_snap.get("body_left") is not None:
            self._shadow_log_decision(
                zone="right", elapsed_min=elapsed_min, room_temp_f=room_temp,
                body_f=right_snap.get("body_left"),
                v5_setting=right_snap.get("setting"),
            )

    def _right_v52_shadow_tick(self, *, elapsed_min, room_temp, sleep_stage, right_snap):
        """Compute and log the right-zone v5.2 decision; do not act.

        Sensor input: body_left_f (skin-contact for the right zone — same as
        right_overheat_safety uses). body_avg includes warm-sheet body_center
        and would push the shadow controller toward false-cool corrections.

        BedJet gate: during the first BEDJET_SUPPRESS_MIN minutes after
        right-bed onset, BedJet airflow inflates body sensors. Force the
        correction to 0 in that window so the shadow log isn't full of
        garbage cooling proposals during legitimate warm-blanket use.
        """
        try:
            cycle_num = self._get_cycle_num(elapsed_min)
            base = RIGHT_CYCLE_SETTINGS.get(
                cycle_num,
                RIGHT_CYCLE_SETTINGS[max(RIGHT_CYCLE_SETTINGS.keys())],
            )
            # Skin-contact channel, NOT body_avg (which mixes warm-sheet center).
            body_skin = right_snap.get("body_left")
            body_avg = right_snap.get("body_avg")  # logged for diagnostics
            firmware_setting = right_snap.get("setting")
            firmware_blower = right_snap.get("blower_pct")
            occupied_state = self._read_bool(BED_PRESENCE_ENTITIES["occupied_right"])
            occupied = occupied_state is True

            # Track right-bed onset for BedJet/initial-bed gates. The event
            # listener writes right_bed_onset_ts; this helper preserves a
            # snapshot/HA-last_changed fallback if AppDaemon restarted mid-night.
            now = datetime.now()
            mins_since_onset = (
                self._update_zone_occupancy_onset("right", occupied, now)
                if occupied_state is not None else self._minutes_since_onset("right", now)
            )
            in_bedjet_window = (
                mins_since_onset is not None
                and 0 <= mins_since_onset <= RIGHT_BEDJET_WINDOW_MIN
            )
            in_initial_bed_cooling = (
                occupied
                and mins_since_onset is not None
                and 0 <= mins_since_onset <= INITIAL_BED_COOLING_MIN
            )
            pre_sleep_stage = (
                sleep_stage is not None
                and str(sleep_stage).lower().strip() in PRE_SLEEP_STAGE_VALUES
            )

            correction = 0
            corr_reason = "none"
            if in_initial_bed_cooling:
                corr_reason = f"initial_bed_cooling_{mins_since_onset:.0f}min"
            elif in_bedjet_window:
                corr_reason = f"bedjet_window_{mins_since_onset:.0f}min"
            elif pre_sleep_stage:
                corr_reason = f"pre_sleep_{str(sleep_stage).lower().strip()}"
            elif occupied_state is None and body_skin is not None:
                corr_reason = "body_fb_skipped_unknown_occupancy"
            elif occupied_state is False and body_skin is not None:
                corr_reason = "body_fb_skipped_unoccupied"
            elif (occupied_state is True
                  and cycle_num not in RIGHT_BODY_FB_SKIP_CYCLES
                  and body_skin is not None):
                delta = body_skin - RIGHT_BODY_FB_TARGET_F
                if delta > 0:
                    raw = RIGHT_BODY_FB_KP_HOT * delta
                    correction = -int(round(min(raw, RIGHT_BODY_FB_MAX_DELTA)))
                    corr_reason = f"hot_{delta:+.1f}"
                elif delta < 0:
                    raw = -RIGHT_BODY_FB_KP_COLD * delta
                    correction = int(round(min(raw, RIGHT_BODY_FB_MAX_DELTA)))
                    corr_reason = f"cold_{delta:+.1f}"

            if in_initial_bed_cooling or pre_sleep_stage:
                body_proposed = INITIAL_BED_RIGHT_SETTING
            else:
                body_proposed = max(-10, min(MAX_SETTING, base + correction))

            # Wife/right-side room compensation is in blower-proxy space, like
            # the left controller, but deliberately hot-only for now.  A room
            # below the shared 72°F reference yields 0, so it cannot warm her
            # side after last night's colder-please override around 68°F.
            right_room_comp = self._right_room_temp_to_blower_comp(room_temp)
            if in_initial_bed_cooling or pre_sleep_stage:
                right_room_comp = 0
                proposed = INITIAL_BED_RIGHT_SETTING
                source = "initial_bed_cooling" if in_initial_bed_cooling else "pre_sleep_precool"
            else:
                target_blower_pct = self._l1_to_blower_pct(body_proposed) + right_room_comp
                target_blower_pct = max(0, min(100, round(target_blower_pct)))
                proposed = self._blower_pct_to_l1(target_blower_pct)
                source = "cycle+body_fb"
                if corr_reason == "body_fb_skipped_unoccupied":
                    source += "+body_fb_skipped(unoccupied)"
                elif corr_reason == "body_fb_skipped_unknown_occupancy":
                    source += "+body_fb_skipped(unknown_occupancy)"
                if right_room_comp:
                    source += "+right_room_hot"

            # ── Right-zone proactive-cool rail (override-absence trap) ──────
            # 2026-05-01: wife rarely overrides — absence of overrides is NOT
            # evidence of comfort. If body_left stays ≥ RIGHT_HOT_RAIL_F for
            # ≥ RIGHT_HOT_RAIL_STREAK consecutive ticks while we're in normal
            # control (not BedJet, not initial-bed-cooling, occupied, sensor
            # available), bias one step cooler. Reset the streak in any
            # excluded window or when occupancy/sensor is missing.
            in_excluded_window = (
                in_initial_bed_cooling or in_bedjet_window or pre_sleep_stage
                or not occupied or body_skin is None
            )
            if in_excluded_window:
                self._state["right_hot_streak"] = 0
            elif body_skin >= RIGHT_HOT_RAIL_F:
                self._state["right_hot_streak"] = (
                    self._state.get("right_hot_streak", 0) + 1
                )
            else:
                self._state["right_hot_streak"] = 0

            right_hot_streak = self._state.get("right_hot_streak", 0)
            hot_rail_fired = (
                right_hot_streak >= RIGHT_HOT_RAIL_STREAK
                and not in_excluded_window
            )
            if hot_rail_fired:
                pre_rail_proposed = proposed
                proposed = max(-10, proposed + RIGHT_HOT_RAIL_BIAS)
                source += f"+hot_rail_{right_hot_streak}"
                self.log(
                    f"Right hot-rail fired: body_left={body_skin:.1f}°F "
                    f"streak={right_hot_streak} → "
                    f"{pre_rail_proposed:+d} → {proposed:+d}"
                )

            # ── Live actuation gate ──────────────────────────────────
            # ALL of the following must hold:
            #   - RIGHT_LIVE_ENABLED constant in code is True
            #   - HA helper input_boolean.snug_right_controller_enabled is on
            #   - bed is occupied
            #   - we are NOT in the BedJet window (don't fight her warm-blanket)
            #   - the user has not recently overridden (60-min freeze)
            #   - rate-limit since last controller write
            #   - proposed setting differs from firmware's current setting
            #   - proposed setting is within [-10, 0] (no heating)
            actuated = False
            actuation_blocked = "off"
            ha_flag_on = self._read_str(E_RIGHT_CONTROLLER_FLAG) == "on"
            if RIGHT_LIVE_ENABLED and ha_flag_on:
                if not occupied:
                    actuation_blocked = "unoccupied"
                elif in_bedjet_window and not in_initial_bed_cooling:
                    actuation_blocked = "bedjet_window"
                elif self._right_zone_in_freeze(now):
                    actuation_blocked = "override_freeze"
                elif not self._right_zone_rate_ok(now):
                    actuation_blocked = "rate_limit"
                elif firmware_setting is not None and proposed == firmware_setting:
                    actuation_blocked = "no_change"
                elif proposed < -10 or proposed > 0:
                    actuation_blocked = "out_of_range"
                else:
                    self._set_l1_right(proposed)
                    self._state["right_zone_last_change_ts"] = now.isoformat()
                    self._save_state()
                    actuated = True
                    actuation_blocked = ""
            elif not ha_flag_on:
                actuation_blocked = "ha_flag_off"

            entry = {
                "ts": now.isoformat(timespec="seconds"),
                "elapsed_min": round(elapsed_min, 1) if elapsed_min is not None else None,
                "cycle": cycle_num,
                "stage": sleep_stage,
                "occupied": occupied,
                "mins_since_onset": (
                    round(mins_since_onset, 1) if mins_since_onset is not None else None
                ),
                "in_bedjet_window": in_bedjet_window,
                "in_initial_bed_cooling": in_initial_bed_cooling,
                "body_skin": body_skin,           # body_left — primary input
                "body_avg": body_avg,             # diagnostic only
                "body_left": right_snap.get("body_left"),
                "body_center": right_snap.get("body_center"),
                "body_right": right_snap.get("body_right"),
                "ambient": right_snap.get("ambient"),
                "room_temp_f": room_temp,
                "firmware_setting": firmware_setting,
                "firmware_blower": firmware_blower,
                "right_v52_base": base,
                "right_v52_correction": correction,
                "right_v52_body_proposed": body_proposed,
                "right_room_comp": right_room_comp,
                "right_target_blower_pct": self._l1_to_blower_pct(proposed),
                "right_v52_proposed": proposed,
                "right_v52_reason": corr_reason,
                "right_v52_source": source,
                "hot_rail_fired": hot_rail_fired,
                "right_hot_streak": right_hot_streak,
                "right_v52_diff_vs_firmware": (
                    None if firmware_setting is None
                    else proposed - firmware_setting
                ),
                "right_live_enabled": RIGHT_LIVE_ENABLED and ha_flag_on,
                "actuated": actuated,
                "actuation_blocked": actuation_blocked,
            }
            import json as _json
            with open(RIGHT_SHADOW_LOG_PATH, "a") as f:
                f.write(_json.dumps(entry) + "\n")
            return entry
        except Exception as exc:  # noqa: BLE001
            self.log(f"right_v52_shadow skipped ({exc.__class__.__name__}): {exc}",
                     level="WARNING")
            return None

    def _shadow_log_decision(self, *, zone, elapsed_min, room_temp_f, body_f, v5_setting):
        """Append one shadow-policy evaluation to /config/snug_shadow.jsonl.

        Defensive: any failure here must NEVER affect the live control loop,
        so the whole thing is wrapped in a broad try/except that just logs.
        """
        try:
            from ml.policy import controller_decision  # lazy import (HA path)
            shadow_setting, rail = controller_decision(
                zone=zone, elapsed_min=elapsed_min,
                room_temp_f=room_temp_f, body_f=body_f,
            )
            entry = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "zone": zone,
                "elapsed_min": round(elapsed_min, 1) if elapsed_min is not None else None,
                "room_temp_f": room_temp_f,
                "body_f": body_f,
                "v5_setting": v5_setting,
                "shadow_setting": shadow_setting,
                "shadow_rail": rail,
                "differs": v5_setting != shadow_setting,
            }
            import json as _json
            with open("/config/snug_shadow.jsonl", "a") as f:
                f.write(_json.dumps(entry) + "\n")
        except Exception as exc:  # noqa: BLE001 — must never break control loop
            self.log(f"shadow log skipped ({exc.__class__.__name__}): {exc}",
                     level="WARNING")

    def _compute_setting(self, elapsed_min, room_temp, sleep_stage,
                          body_avg=None, body_left=None, current_setting=None,
                          mins_since_occupied=None, *, bed_occupied=None):
        """Compute target L1 from cycle/stage, body feedback, room compensation, and learned blower residuals."""
        cycle_num = self._get_cycle_num(elapsed_min)
        self._state["current_cycle_num"] = cycle_num

        data_source = "time_cycle"
        base_setting = CYCLE_SETTINGS.get(cycle_num, CYCLE_SETTINGS[max(CYCLE_SETTINGS.keys())])
        pre_sleep_stage = (
            sleep_stage is not None
            and str(sleep_stage).lower().strip() in PRE_SLEEP_STAGE_VALUES
        )
        in_initial_bed_cooling = (
            mins_since_occupied is not None
            and 0 <= mins_since_occupied <= INITIAL_BED_COOLING_MIN
        )

        # Explicit pre-cool / initial-bed gate.  This intentionally bypasses
        # body feedback, learned residuals, and room compensation so a cold body
        # sensor or cold room cannot warm the first 30 minutes.
        if pre_sleep_stage or in_initial_bed_cooling:
            forced = INITIAL_BED_LEFT_SETTING
            reason = (
                "pre_sleep_precool"
                if pre_sleep_stage
                else f"initial_bed_cooling({mins_since_occupied:.0f}m)"
            )
            forced_blower = self._l1_to_blower_pct(forced)
            return {
                "setting": forced,
                "target_blower_pct": forced_blower,
                "base_setting": forced,
                "base_blower_pct": forced_blower,
                "cycle_num": cycle_num,
                "room_temp_comp": 0,
                "learned_adj_pct": 0,
                "data_source": reason,
                "hot_safety": False,
                "overheat_hard": False,
            }

        if sleep_stage and sleep_stage not in ("unknown", ""):
            staged_setting = self._setting_for_stage(sleep_stage)
            if staged_setting is not None:
                base_setting = staged_setting
                data_source = "stage"

        # ── v5.2 body-temperature feedback ───────────────────────────
        # Closed-loop correction on the cycle baseline. Reads BODY_FB_INPUT
        # (defaults to body_left, the skin-contact channel) — see module-level
        # BODY_FB_* constants for the rationale and fit numbers.
        body_fb_correction = 0
        body_fb_input = body_left if BODY_FB_INPUT == "body_left" else body_avg
        body_fb_ready = (
            BODY_FB_ENABLED
            and cycle_num >= BODY_FB_MIN_CYCLE
            and not pre_sleep_stage
            and body_fb_input is not None
        )
        if body_fb_ready and bed_occupied is not True:
            reason = "unknown_occupancy" if bed_occupied is None else "unoccupied"
            data_source += f"+body_fb_skipped({reason})"
        elif body_fb_ready:
            body_delta = body_fb_input - BODY_FB_TARGET_F
            if body_delta < 0:
                # Body cooler than target → ease off (warmer correction)
                raw = -BODY_FB_KP_COLD * body_delta  # positive
                body_fb_correction = int(round(min(raw, BODY_FB_MAX_DELTA)))
                if body_fb_correction:
                    new_base = max(-10, min(MAX_SETTING, base_setting + body_fb_correction))
                    if new_base != base_setting:
                        base_setting = new_base
                        data_source += f"+body_fb({body_fb_correction:+d})"

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
        overheat_hard = False
        body_max = body_avg  # used by both rails
        # ── Hard overheat rail: sustained body ≥90°F → force -10 ──
        # Gated behind input_boolean so user can disable instantly. Hysteresis
        # via OVERHEAT_HARD_RELEASE_F prevents single-spike chattering.
        rail_enabled = self._read_str(E_OVERHEAT_RAIL_FLAG) == "on"
        if rail_enabled and body_avg is not None:
            already_engaged = self._state.get("overheat_hard_engaged", False)
            release_threshold = OVERHEAT_HARD_RELEASE_F if already_engaged else OVERHEAT_HARD_F
            if body_avg >= OVERHEAT_HARD_F:
                self._state["overheat_hard_streak"] = self._state.get("overheat_hard_streak", 0) + 1
            elif body_avg < release_threshold:
                self._state["overheat_hard_streak"] = 0
                self._state["overheat_hard_engaged"] = False
            if self._state.get("overheat_hard_streak", 0) >= OVERHEAT_HARD_STREAK:
                self._state["overheat_hard_engaged"] = True
                hard_blower_pct = self._l1_to_blower_pct(-10)
                if hard_blower_pct > target_blower_pct:
                    target_blower_pct = hard_blower_pct
                    overheat_hard = True
                    data_source += "+overheat_hard"

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
            "overheat_hard": overheat_hard,
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
            self._state["recent_changes"] = []
            self._state["override_count"] = 0
            self._state["last_change_ts"] = None
            self._state["last_restart_ts"] = None
            self._state["body_below_since"] = None
            self._state["hot_streak"] = 0
            self._state["right_hot_streak"] = 0
            self._state["left_zone_last_occupied"] = False
            self._state["left_zone_occupied_since"] = None
            self._state["left_bed_onset_ts"] = None
            self._state["left_bed_vacated_since"] = None
            self._state["right_zone_last_occupied"] = False
            self._state["right_zone_occupied_since"] = None
            self._state["right_bed_onset_ts"] = None
            self._state["right_bed_vacated_since"] = None
            self._ensure_responsive_cooling_off()
            self._ensure_3_level_off()
            self.call_service(
                "input_text/set_value",
                entity_id=E_SLEEP_STAGE, value="unknown",
            )
            self._learned = self._learn_from_history()
            self._save_learned()
            self.log(f"  Learned adjustments: {self._learned}")

            room_temp = self._read_temperature(self._get_room_temp_entity())
            initial_snapshot = self._read_zone_snapshot("left")
            initial_setting = INITIAL_BED_LEFT_SETTING
            plan = {"target_blower_pct": self._l1_to_blower_pct(initial_setting)}
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
            self._state["last_restart_ts"] = None
            self._state["hot_streak"] = 0
            self._state["right_hot_streak"] = 0
            self._state["right_rail_force_seen"] = False
            self._state["right_rail_force_seen_at"] = None
            for zone in ("left", "right"):
                self._state[f"{zone}_zone_last_occupied"] = False
                self._state[f"{zone}_zone_occupied_since"] = None
                self._state[f"{zone}_bed_onset_ts"] = None
                self._state[f"{zone}_bed_vacated_since"] = None
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

        # 2026-05-01: do NOT set a night-long floor. Override is a learning
        # event consumed cross-night by _learn_from_history; the 60-min freeze
        # below is the only short-term respect of the user's input.
        self._state["override_freeze_until"] = (
            now + timedelta(minutes=OVERRIDE_FREEZE_MIN)
        ).isoformat()
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
        """Persist passive right-side manual changes for future training data.

        Also engages a 60-min override-freeze when the controller is live
        (RIGHT_LIVE_ENABLED + HA helper on), so the controller doesn't fight
        the wife's manual adjustments.
        """
        if not self._is_sleeping():
            return

        try:
            old_val = int(float(old))
            new_val = int(float(new))
        except (ValueError, TypeError):
            return

        if new_val == old_val:
            return

        # If this change matches what the controller just wrote, suppress the
        # override classification (it was us, not her).
        expected = self._state.get("right_zone_last_setting")
        if expected is not None and new_val == expected:
            return

        snapshot = self._read_zone_snapshot("right")
        body_left = snapshot.get("body_left") if isinstance(snapshot, dict) else None
        now = datetime.now()
        rail_enabled = self._read_str(E_RIGHT_OVERHEAT_RAIL_FLAG) == "on"
        rail_streak = self._state.get("right_hot_streak", 0)
        rail_force = (
            new_val <= -10
            and body_left is not None
            and body_left >= RIGHT_HOT_RAIL_F
            and rail_enabled
            and rail_streak >= RIGHT_HOT_RAIL_STREAK
        )
        rail_seen_at = self._state.get("right_rail_force_seen_at")
        rail_seen_fresh = False
        if self._state.get("right_rail_force_seen") and rail_seen_at:
            try:
                rail_seen_fresh = (
                    now - datetime.fromisoformat(rail_seen_at)
                ).total_seconds() <= RIGHT_RAIL_MAX_ENGAGE_SEC
            except (TypeError, ValueError):
                rail_seen_fresh = False
        rail_release = (
            old_val <= -10
            and new_val > -10
            and rail_seen_fresh
            and body_left is not None
            and body_left <= RIGHT_RAIL_RELEASE_F + RIGHT_RAIL_RELEASE_TOLERANCE_F
        )

        if rail_force:
            self._state["right_rail_force_seen"] = True
            self._state["right_rail_force_seen_at"] = now.isoformat()
            self.log(
                f"RIGHT RAIL FORCE: {old_val:+d} → {new_val:+d} "
                f"body_left={body_left:.1f}°F streak={rail_streak} — not a manual override",
                level="WARNING",
            )
            self._log_override(
                "right",
                new_val,
                controller_value=old_val,
                delta=new_val - old_val,
                room_temp=self._read_temperature(self._get_room_temp_entity()),
                sleep_stage=self._read_str(E_SLEEP_STAGE),
                snapshot=snapshot,
                action="rail_force",
                source="rail_force",
            )
            self._save_state()
            return

        if rail_release:
            self._state["right_rail_force_seen"] = False
            self._state["right_rail_force_seen_at"] = None
            self.log(
                f"RIGHT RAIL RELEASE: {old_val:+d} → {new_val:+d} "
                f"body_left={body_left:.1f}°F — not a manual override",
                level="WARNING",
            )
            self._log_override(
                "right",
                new_val,
                controller_value=old_val,
                delta=new_val - old_val,
                room_temp=self._read_temperature(self._get_room_temp_entity()),
                sleep_stage=self._read_str(E_SLEEP_STAGE),
                snapshot=snapshot,
                action="rail_force",
                source="rail_release",
            )
            self._save_state()
            return

        if old_val <= -10 and new_val > -10:
            self._state["right_rail_force_seen"] = False
            self._state["right_rail_force_seen_at"] = None

        self.log(f"RIGHT SIDE CHANGE: {old_val:+d} → {new_val:+d}")
        # Engage override freeze (only meaningful when controller is live).
        ha_flag_on = self._read_str(E_RIGHT_CONTROLLER_FLAG) == "on"
        if RIGHT_LIVE_ENABLED and ha_flag_on:
            from datetime import timedelta as _td
            self._state["right_zone_override_until"] = (
                now + _td(minutes=RIGHT_OVERRIDE_FREEZE_MIN)
            ).isoformat()
            self._save_state()

        self._log_override(
            "right",
            new_val,
            controller_value=old_val,
            delta=new_val - old_val,
            room_temp=self._read_temperature(self._get_room_temp_entity()),
            sleep_stage=self._read_str(E_SLEEP_STAGE),
            snapshot=snapshot,
        )

    def _set_l1_right(self, value):
        """Write right-zone bedtime temperature. Mirrors _set_l1 for left."""
        value = max(-10, min(MAX_SETTING, int(value)))
        self._state["right_zone_last_setting"] = value
        self.call_service("number/set_value", entity_id=E_BEDTIME_TEMP_RIGHT,
                          value=value)
        self.log(f"RIGHT v5.2 LIVE: setting → {value:+d}")

    def _right_zone_in_freeze(self, now):
        """True if a recent manual right-zone change is freezing the controller."""
        until = self._state.get("right_zone_override_until")
        if not until:
            return False
        try:
            until_dt = datetime.fromisoformat(until)
        except (TypeError, ValueError):
            return False
        return now < until_dt

    def _right_zone_rate_ok(self, now):
        """True if enough time has passed since the controller last wrote."""
        last_ts = self._state.get("right_zone_last_change_ts")
        if not last_ts:
            return True
        try:
            last_dt = datetime.fromisoformat(last_ts)
        except (TypeError, ValueError):
            return True
        return (now - last_dt).total_seconds() >= RIGHT_MIN_CHANGE_INTERVAL_SEC

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

    def _zone_occupied_from_bed_presence(self, bed_presence, zone, fallback):
        """Use ESPHome bed-presence occupancy when present; fall back otherwise."""
        if isinstance(bed_presence, dict):
            occupied = bed_presence.get(f"occupied_{zone}")
            if occupied is not None:
                return bool(occupied)
        return bool(fallback)

    def _bed_onset_keys(self, zone):
        return (
            f"{zone}_bed_onset_ts",
            f"{zone}_zone_occupied_since",
            f"{zone}_zone_last_occupied",
            f"{zone}_bed_vacated_since",
        )

    def _on_bed_onset(self, entity, attribute, old, new, kwargs):
        """Bed-presence event hook: react within ~1s of off/unavailable→on."""
        zone = kwargs.get("zone") if isinstance(kwargs, dict) else None
        if zone not in ("left", "right") or new != "on":
            return

        now = datetime.now()
        onset_key, since_key, last_key, vacated_key = self._bed_onset_keys(zone)
        existing_onset = self._state.get(onset_key)
        self._state[last_key] = True
        self._state[vacated_key] = None
        if not existing_onset:
            ts = now.isoformat()
            self._state[onset_key] = ts
            self._state[since_key] = ts
            msg = (
                "Bed-onset detected (left): scheduling immediate tick — "
                "initial_bed_cooling will force L1=-10"
                if zone == "left"
                else "Bed-onset detected (right): scheduling immediate tick — "
                     "initial_bed_cooling will force right=-10"
            )
        else:
            if not self._state.get(since_key):
                self._state[since_key] = existing_onset
            msg = (
                f"Bed-onset detected ({zone}): scheduling immediate tick "
                "with existing onset timestamp"
            )

        self._save_state()
        self.log(msg)
        self.run_in(self._control_loop, 1)

    def _on_bed_vacated(self, entity, attribute, old, new, kwargs):
        """Debounce bed-vacancy before clearing onset to avoid bathroom-trip re-cooling."""
        zone = kwargs.get("zone") if isinstance(kwargs, dict) else None
        if zone not in ("left", "right") or new != "off":
            return
        _, _, last_key, vacated_key = self._bed_onset_keys(zone)
        self._state[last_key] = False
        self._state[vacated_key] = datetime.now().isoformat()
        if zone == "right":
            self._state["right_rail_force_seen"] = False
            self._state["right_rail_force_seen_at"] = None
        self._save_state()
        try:
            self.run_in(
                self._clear_bed_onset_if_still_vacant,
                int(BED_ONSET_CLEAR_DEBOUNCE_MIN * 60),
                zone=zone,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"Bed-vacancy debounce scheduling failed ({zone}): {exc}", level="WARNING")

    def _clear_bed_onset_if_still_vacant(self, kwargs):
        zone = kwargs.get("zone") if isinstance(kwargs, dict) else None
        if zone not in ("left", "right"):
            return
        occupied = self._read_bool(BED_PRESENCE_ENTITIES[f"occupied_{zone}"])
        if occupied is True:
            return
        onset_key, since_key, last_key, vacated_key = self._bed_onset_keys(zone)
        self._state[onset_key] = None
        self._state[since_key] = None
        self._state[last_key] = False
        self._state[vacated_key] = None
        self._save_state()
        self.log(f"Bed-vacancy confirmed ({zone}): cleared bed-onset timestamp")

    def _minutes_since_onset(self, zone, now):
        onset_key, since_key, _, _ = self._bed_onset_keys(zone)
        occ_since = self._state.get(onset_key) or self._state.get(since_key)
        if not occ_since:
            return None
        try:
            occ_dt = datetime.fromisoformat(occ_since)
        except (TypeError, ValueError):
            return None
        return (now - occ_dt).total_seconds() / 60.0

    def _update_zone_occupancy_onset(self, zone, occupied, now):
        """Track minutes since bed-presence occupancy onset for initial cooling."""
        onset_key, since_key, last_key, vacated_key = self._bed_onset_keys(zone)

        if occupied:
            changed = False
            existing_onset = self._state.get(onset_key)
            legacy_since = self._state.get(since_key)
            if existing_onset is None and legacy_since:
                self._state[onset_key] = legacy_since
                existing_onset = legacy_since
                changed = True
            if existing_onset is None:
                ts = now.isoformat()
                self._state[onset_key] = ts
                self._state[since_key] = ts
                changed = True
            elif not legacy_since:
                self._state[since_key] = existing_onset
                changed = True
            if not self._state.get(last_key):
                self._state[last_key] = True
                changed = True
            if self._state.get(vacated_key) is not None:
                self._state[vacated_key] = None
                changed = True
            if changed:
                self._save_state()
            return self._minutes_since_onset(zone, now)

        if self._state.get(last_key):
            self._state[last_key] = False
            self._state[vacated_key] = now.isoformat()
            self._save_state()
        vacated_since = self._state.get(vacated_key)
        if vacated_since:
            try:
                vacated_dt = datetime.fromisoformat(vacated_since)
            except (TypeError, ValueError):
                vacated_dt = now
            if (now - vacated_dt).total_seconds() >= BED_ONSET_CLEAR_DEBOUNCE_MIN * 60:
                self._state[onset_key] = None
                self._state[since_key] = None
                self._state[vacated_key] = None
                self._save_state()
        return None

    def _recover_zone_onset_from_presence(self, zone):
        occupied = self._read_bool(BED_PRESENCE_ENTITIES[f"occupied_{zone}"])
        onset_key, since_key, last_key, vacated_key = self._bed_onset_keys(zone)
        changed = False
        if occupied is True:
            if not self._state.get(onset_key):
                fallback = self._state.get(since_key)
                if not fallback:
                    last_changed = self.get_state(
                        BED_PRESENCE_ENTITIES[f"occupied_{zone}"],
                        attribute="last_changed",
                    )
                    if last_changed:
                        try:
                            dt = datetime.fromisoformat(last_changed)
                            if dt.tzinfo is not None:
                                dt = dt.astimezone().replace(tzinfo=None)
                            fallback = dt.isoformat()
                        except (TypeError, ValueError):
                            fallback = None
                fallback = fallback or datetime.now().isoformat()
                self._state[onset_key] = fallback
                self._state[since_key] = fallback
                changed = True
            elif not self._state.get(since_key):
                self._state[since_key] = self._state.get(onset_key)
                changed = True
            if not self._state.get(last_key):
                self._state[last_key] = True
                changed = True
            if self._state.get(vacated_key):
                self._state[vacated_key] = None
                changed = True
        elif occupied is False:
            if self._state.get(last_key):
                self._state[last_key] = False
                self._state[vacated_key] = datetime.now().isoformat()
                changed = True
        if changed:
            self._save_state()

    def _check_midnight_restart(self):
        """Gap 5: If AppDaemon restarts while sleeping, resume from correct position."""
        if self._is_sleeping():
            self._recover_zone_onset_from_presence("left")
            self._recover_zone_onset_from_presence("right")
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
        self._state["last_setting"] = value
        self.call_service("number/set_value", entity_id=E_BEDTIME_TEMP, value=value)

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

    def _right_room_temp_to_blower_comp(self, room_temp):
        """Right-zone room compensation in blower space.

        Shared physical baseline: 72°F room.  Wife-specific policy: hot-room
        cooling only for the initial deployment.  Cold-room warming is
        explicitly disabled (gain 0.0) so a 67–68°F room returns 0, preserving
        the colder-please signal from the most recent right-side override.
        """
        if room_temp is None:
            return 0
        if room_temp > RIGHT_ROOM_BLOWER_REFERENCE_F:
            return round(
                (room_temp - RIGHT_ROOM_BLOWER_REFERENCE_F)
                * RIGHT_ROOM_BLOWER_HOT_COMP_PER_F
            )
        if room_temp < RIGHT_ROOM_BLOWER_REFERENCE_F:
            return -round(
                (RIGHT_ROOM_BLOWER_REFERENCE_F - room_temp)
                * RIGHT_ROOM_BLOWER_COLD_COMP_PER_F
            )
        return 0

    def _ensure_responsive_cooling_off(self, force=False):
        changed = False
        for label, eid in (("left", E_RESPONSIVE_COOLING),
                           ("right", E_RESPONSIVE_COOLING_RIGHT)):
            try:
                rc_state = self.get_state(eid)
            except Exception:
                rc_state = None
            if force or rc_state == "on":
                if not force:
                    self.log(
                        f"WARNING: Responsive cooling was ON ({label}) — turning it back OFF",
                        level="WARNING",
                    )
                try:
                    self.call_service("switch/turn_off", entity_id=eid)
                    changed = True
                except Exception as err:
                    self.log(
                        f"FAILED to turn OFF responsive cooling ({label}): {err}",
                        level="ERROR",
                    )
        return changed

    def _ensure_3_level_off(self):
        """Watchdog: keep 3-level mode OFF on BOTH sides.

        The user runs single-stage L1 (bedtime_temperature). If 3-level mode
        drifts on, the firmware will start advancing L1→L2→L3 by run_progress
        and our writes to the bedtime entity stop affecting the live dial.
        Called from initialize(), _on_sleep_mode("on"), and every _control_loop
        tick. Idempotent and safe to call when already OFF (no service call
        emitted).
        """
        changed = False
        for label, eid in (("left", E_PROFILE_3LEVEL),
                           ("right", E_PROFILE_3LEVEL_RIGHT)):
            try:
                state = self.get_state(eid)
            except Exception:
                state = None
            if state == "on":
                self.log(
                    f"WARNING: 3-level mode ON ({label}) — turning OFF",
                    level="WARNING",
                )
                try:
                    self.call_service("switch/turn_off", entity_id=eid)
                    changed = True
                except Exception as err:
                    self.log(
                        f"FAILED to turn OFF 3-level mode ({label}): {err}",
                        level="ERROR",
                    )
        return changed

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
            value = float(state)
        except (ValueError, TypeError):
            return None
        # NaN guard: HA can publish "nan" or sensors can briefly report it on
        # device boot. float() succeeds, but downstream int() / comparisons
        # would crash or silently mis-evaluate. Treat NaN as missing.
        if math.isnan(value):
            return None
        return value

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

    def _log_passive_zone_snapshot(self, zone, elapsed_min, room_temp, sleep_stage,
                                   bed_presence=None, snapshot=None,
                                   data_source_suffix=None):
        """Persist a passive 5-minute telemetry snapshot for a non-controlled zone."""
        if snapshot is None:
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
            data_source=(
                f"passive_{zone}+{data_source_suffix}"
                if data_source_suffix else f"passive_{zone}"
            ),
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
                  AND COALESCE(notes, '') NOT LIKE '%initial_bed_cooling%'
                  AND COALESCE(notes, '') NOT LIKE '%bedjet_window%'
                  AND COALESCE(notes, '') NOT LIKE '%pre_sleep%'
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
                      room_temp=None, sleep_stage=None, snapshot=None,
                      action="override", source="manual"):
        """Log a right/left setpoint change classified outside normal control."""
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
                notes = f"cycle={cycle_num} src={source} zone={zone}"
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
                        NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
                    action,
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
