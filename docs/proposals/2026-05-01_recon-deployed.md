# Recon-deployed audit — what v5.2 actually does at runtime
**Agent:** `recon-deployed` (v6 design fleet, 1 of 10)
**Date:** 2026-05-01
**Sources:** `appdaemon/sleep_controller_v5.py` @ `be151ca…6784` (repo HEAD), deployed `1dfcc8c…cc4d49` on `root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/` (functionally identical — diff is comment-only on `L1_TO_BLOWER_PCT`); `appdaemon/right_overheat_safety.py` @ `b0f9dcc…0216` (matches deployed); `tools/lib_active_setting.py`; PG `sleepdata.controller_readings` (last 7 nights, last v5.2 night 2026-04-30 22:51 → 2026-05-01 08:43 ET); `/config/configuration.yaml` recorder section.

---

## 0. Single-night reality check (last v5.2 night)

| metric | left v5.2 | right v5.2 |
|---|---:|---:|
| rows logged | 124 | 120 |
| `set` (controller wrote L1) | **0** | **0** |
| `hold` | 33 | 0 |
| `manual_hold` | **85** | 0 |
| `freeze_hold` | 1 | 0 |
| `override` | 5 | 1 |
| `passive` (firmware-default rows) | 0 | **119** |

Two structural facts visible from one night:

1. **Left zone tripped the kill switch at 01:43 ET** (`manual_mode=True` for the rest of the night → 85 `manual_hold` rows). The user pushed `-10 → -9 → -8 → -6` within ~95 s during c2 (01:37:49, 01:38:20, 01:39:23). With `KILL_SWITCH_CHANGES=3`, `KILL_SWITCH_WINDOW_SEC=300` this *correctly* fired — but the controller never wrote a single `set` row all night. Every change was the user's; the controller did exactly one `freeze_hold` and then lost authority for 7 hours. The cycle-baseline + body-feedback machinery had no observable effect on actuation last night. (See §5/6.)

2. **Right zone v5.2 `actuated=0` last night.** All 120 rows are `passive` (firmware-default telemetry from `_log_passive_zone_snapshot`). The right v5.2 path writes its decisions to `/config/snug_right_v52_shadow.jsonl` and only writes to PG via the passive-zone snapshot. The single right-zone `override` (03:25 ET, body_left=73.1°F, room=68.3°F, `-4 → -5`) is logged through `_on_right_setting_change`. **The right-zone live path emits zero PG rows tagged with the controller's actual decision** — exactly the M11 audit gap the PROGRESS_REPORT calls out.

This is the lens for everything below: v5.2's two-stage cascade is real in code, but on the only night it has run end-to-end the LEFT side gave up after 50 minutes and the RIGHT side never logged its actuation to the gold dataset.

---

## 1. Parameters & constants

### 1.1 Module-level constants (`sleep_controller_v5.py`)

| Constant | Value | Lines | Used in / purpose |
|---|---|---|---|
| `CONTROLLER_VERSION` | `"v5_2_rc_off"` | 197 | tag on every PG row; learning lookback filters by it (1710); end-of-night summary (1671) |
| `ENABLE_LEARNING` | `True` | 198 | gate on `learned_adj_pct` block (891) |
| `MAX_SETTING` | `0` | 199 | upper clip on every L1 write (1311, 1134, 882, 691); meaning "no heating" |
| `CYCLE_DURATION_MIN` | `90` | 91 | divisor in `_get_cycle_num` (971) |
| `CYCLE_SETTINGS` | `{1:-10, 2:-10, 3:-7, 4:-5, 5:-5, 6:-6}` | 64–90 | left-zone time baseline (826); also default `baseline` for PG override log (1921) |
| `RIGHT_CYCLE_SETTINGS` | `{1:-8, 2:-7, 3:-6, 4:-5, 5:-5, 6:-5}` | 147–154 | right-zone shadow/live baseline (623) |
| `BODY_FB_ENABLED` | `True` | 111 | gate on left body feedback (872) |
| `BODY_FB_INPUT` | `"body_left"` | 112 | which sensor feeds left FB (871) — note `body_avg` was the prior choice |
| `BODY_FB_TARGET_F` | `80.0` | 116 | left FB target (876); same as right |
| `BODY_FB_KP_COLD` | `1.25` | 120 | settings-warmer-per-°F-below-target (879). Asymmetric: no `Kp_hot` exists (rails handle hot side) |
| `BODY_FB_MAX_DELTA` | `5` | 126 | upward correction cap (880) |
| `BODY_FB_MIN_CYCLE` | **`1`** | 127 | (changed today from 3) — body FB now active from cycle 1 (873) |
| `RIGHT_BODY_FB_TARGET_F` | `80.0` | 155 | (678) |
| `RIGHT_BODY_FB_KP_HOT` | `0.5` | 156 | (680) — symmetric, unlike left |
| `RIGHT_BODY_FB_KP_COLD` | `0.3` | 157 | (684) |
| `RIGHT_BODY_FB_MAX_DELTA` | `4` | 158 | (681, 685) |
| `RIGHT_BODY_FB_SKIP_CYCLES` | `()` | 159 | (676) — empty tuple = no skipped cycles |
| `RIGHT_BEDJET_WINDOW_MIN` | `30.0` | 160 | shadow-tick BedJet gate (656); decoupled from `right_overheat_safety.BEDJET_SUPPRESS_MIN` |
| `RIGHT_SHADOW_ENABLED` | `True` | 146 | gate on shadow tick call (603) |
| `RIGHT_LIVE_ENABLED` | `True` | 190 | code-side arm; AND-gated with HA helper (723) |
| `E_RIGHT_CONTROLLER_FLAG` | `input_boolean.snug_right_controller_enabled` | 191 | UI-side arm (722) |
| `RIGHT_MIN_CHANGE_INTERVAL_SEC` | `1800` (30 min) | 193 | (1160) |
| `RIGHT_OVERRIDE_FREEZE_MIN` | `60` | 194 | (1118) |
| `INITIAL_BED_COOLING_MIN` | **`30.0`** | 177 | (today) initial-bed forced-cool window (661, 833) |
| `INITIAL_BED_LEFT_SETTING` | `-10` | 178 | (840, 1006) |
| `INITIAL_BED_RIGHT_SETTING` | `-10` | 179 | (689, 700) |
| `PRE_SLEEP_STAGE_VALUES` | `("inbed", "awake")` | 170 | bypass body FB; force `INITIAL_BED_*_SETTING` (829, 665) |
| `L1_TO_BLOWER_PCT` | `{-10:100,-9:87,-8:75,-7:65,-6:50,-5:41,-4:33,-3:26,-2:20,-1:10,0:0}` | 201–234 | core proxy table (1315, 1319) |
| `ROOM_BLOWER_REFERENCE_F` | **`72.0`** | 238 | (today, was 68) left room comp anchor (1332–) |
| `ROOM_BLOWER_COLD_COMP_PER_F` | `4.0` | 239 | (1335) — pts per °F below 72 |
| `ROOM_BLOWER_COLD_THRESHOLD_F` | `63.0` | 240 | extra-cold knee (1336) |
| `ROOM_BLOWER_COLD_EXTRA_PER_F` | `3.0` | 241 | extra slope below 63°F (1338) |
| `ROOM_BLOWER_HOT_COMP_PER_F` | `4.0` | 242 | (1333) — pts per °F above 72 |
| `RIGHT_ROOM_BLOWER_REFERENCE_F` | `72.0` (= left) | 249 | (1353) |
| `RIGHT_ROOM_BLOWER_HOT_COMP_PER_F` | `4.0` | 250 | (1356) |
| `RIGHT_ROOM_BLOWER_COLD_COMP_PER_F` | **`0.0`** | 251 | hot-only by design (1361) |
| `BODY_HOT_THRESHOLD_F` | `85.0` | 254 | hot_safety trigger (929) |
| `BODY_HOT_STREAK_COUNT` | `2` | 255 | (931) |
| `BODY_TEMP_MIN_F` / `BODY_TEMP_MAX_F` | `55.0` / `110.0` | 256–257 | sanity clip on every body read (1447) |
| `OVERHEAT_HARD_F` | `90.0` | 265 | left hard rail engage (916) |
| `OVERHEAT_HARD_STREAK` | `2` | 266 | (921) |
| `OVERHEAT_HARD_RELEASE_F` | `86.0` | 267 | hysteresis (915) |
| `E_OVERHEAT_RAIL_FLAG` | `input_boolean.snug_overheat_rail_enabled` | 268 | gate (912) — defaults OFF |
| `OVERRIDE_FREEZE_MIN` | `60` | 271 | (1068) |
| `MIN_CHANGE_INTERVAL_SEC` | `1800` (30 min) | 272 | (541) — also blocks RIGHT writes via `RIGHT_MIN_CHANGE_INTERVAL_SEC` |
| `KILL_SWITCH_CHANGES` | `3` | 273 | (1169) |
| `KILL_SWITCH_WINDOW_SEC` | `300` | 274 | (1059, 1165, 1169) |
| `BODY_OCCUPIED_THRESHOLD_F` | `74.0` | 277 | empty-bed hysteresis upper (1186) |
| `BODY_EMPTY_THRESHOLD_F` | `72.0` | 278 | empty-bed hysteresis lower (1190) |
| `BODY_EMPTY_TIMEOUT_MIN` | `20` | 279 | (1196) |
| `AUTO_RESTART_DEBOUNCE_SEC` | `300` | 280 | (1385) |
| `LEARNING_LOOKBACK_NIGHTS` | `14` | 345 | history window for `_learn_from_history` (1711) |
| `LEARNING_MAX_BLOWER_ADJ` | `30` | 346 | hard clip on learned adj (894) — note conflicts with comment "±15%" in §7 of PROGRESS_REPORT |
| `LEARNING_DECAY` | `0.7` | 347 | exponential weight by recency (1744) |
| `DEFAULT_ROOM_TEMP_ENTITY` | `sensor.bedroom_temperature_sensor_temperature` | 292 | (1308) |
| `_setting_for_stage` map | `{deep:-10, core:-8, rem:-6, awake:-5, inbed:-9}` | 962–967 | replaces base_setting whenever stage available (861) — see §8 H3 |

### 1.2 `right_overheat_safety.py` constants

| Constant | Value | Purpose |
|---|---|---|
| `OVERHEAT_HARD_F` | `86.0` | engage threshold (was 88, lowered today-ish) |
| `OVERHEAT_HARD_STREAK` | `2` | ~2 min |
| `OVERHEAT_RELEASE_F` | `82.0` | hysteresis floor (was 84) |
| `RAIL_FORCE_SETTING` | `-10` | force value |
| `POLL_INTERVAL_SEC` | `60` | every minute |
| `BEDJET_SUPPRESS_MIN` | `30.0` | suppression window |
| `E_RAIL_FLAG` | `input_boolean.snug_right_overheat_rail_enabled` | UI gate (default ON per PROGRESS_REPORT §7) |
| `E_BODY_LEFT_R` | `sensor.smart_topper_right_side_body_sensor_left` | skin-contact channel (was `_center`) |

---

## 2. Today's deployed changes — verified

### 2.1 `ROOM_BLOWER_REFERENCE_F = 72.0` (was 68)
- **In code:** line 238. Both branches in `_room_temp_to_blower_comp` (1329–1341) and the right-zone variant `RIGHT_ROOM_BLOWER_REFERENCE_F` (249, 1343–1363) use this anchor.
- **Effect on left:** at room=68°F → comp = `-(72-68)·4 = -16` blower pts (was `+0`); at room=72°F → `0` (was `+16`). PROGRESS_REPORT §"Room compensation reference" shows replay shifted unfloored target -16 pts at comparable room temps; live actuation last night was constrained anyway.
- **Effect on right:** the right hot-only comp uses the same reference but `RIGHT_ROOM_BLOWER_COLD_COMP_PER_F=0.0` (line 251), so the change has *zero* numeric impact below 72°F. Only the hot-room branch above 72°F is affected (and that branch is unchanged in slope).

### 2.2 `INITIAL_BED_COOLING_MIN = 30.0` + initial-bed gate
- **Constants:** lines 177–179.
- **Plumbing (left):** `_compute_setting` line 831–858 returns the forced `-10` early; `_control_loop` passes `mins_since_occupied=left_mins_since_onset` from `_update_zone_occupancy_onset` (line 457, 1214–1238). Onset is taken from `bed_presence.occupied_left` (ESPHome) when available, else falls back to body-occupancy heuristic (1206–1212).
- **Plumbing (right):** in `_right_v52_shadow_tick` line 658–662 / 670–671 / 688–691 / 698–701. Right uses its own `right_zone_occupied_since` derived directly from `BED_PRESENCE_ENTITIES["occupied_right"]` (632, 637–643) — **deliberately decoupled** from `right_overheat_safety.py`'s onset tracker so the two don't trample each other.
- Right's BedJet gate (`RIGHT_BEDJET_WINDOW_MIN=30.0`) coincides with `INITIAL_BED_COOLING_MIN=30.0`, so under live arming the initial-bed window *overrides* the BedJet suppression for actuation (line 726: `elif in_bedjet_window and not in_initial_bed_cooling`). The shadow-tick correction logic also prefers `in_initial_bed_cooling` over `in_bedjet_window` (line 670 vs 672).

### 2.3 `BODY_FB_MIN_CYCLE = 1` (was 3)
- Line 127. Means body feedback is now active in c1/c2 — previously the early-cycle baselines `[-10,-10]` were forced. The user's preference statement quoted in PROGRESS_REPORT (precool + 30 min hard cooling) is now enforced via the *initial-bed* gate (which runs **before** the body-FB block at 870–885) rather than via the cycle gate.
- Net behavior: cycles 1–2 outside the initial-bed window (e.g., user got into bed >30 min before sleep onset, or sensors missed onset) will now allow body-FB warming. Before today, c1/c2 were always forced cool.

### 2.4 Recorder fix — PID/run_progress/L2/L3 sensors now record
- Verified on host: `/config/configuration.yaml.bak.20260501` (5pm today) vs current shows the following globs **removed** from `recorder.exclude.entity_globs`:
  - `sensor.smart_topper_*_pid_*`
  - `sensor.smart_topper_*_heater_*_raw`
  - `sensor.smart_topper_*_run_progress`
- This unlocks the firmware's exposed PID stream (`pid_control_output`, `pid_proportional_term`, `pid_integral_term`) and `run_progress`, which are the prerequisite for L_active computation (see §6) and for fitting the firmware's Stage-2 model directly (rc_synthesis §"What's missing").
- L2/L3 (`number.smart_topper_*_sleep_temperature` / `*_wake_temperature`) were never in the exclude list — they record because they're `number` not `sensor.smart_topper_*_pid_*`. The "L2/L3" benefit framed in the task brief is implicit: with `run_progress` now recording, joining L1/L2/L3 with the active dial via `lib_active_setting` is finally possible.

---

## 3. Gates, freezes, kill-switches — every actuation guard

### 3.1 Left zone (`_control_loop`)

```
not _is_sleeping()                   → return (no PG row, no actuation)
not body_occupied                    → log empty_bed row, return
firmware running=off                 → call switch.turn_on (debounced 5 min)
sleep_start unset                    → return (no row)
current setting unavailable          → return WARNING

# Now compute plan, then check actuation gates in order:
override floor                       → setting = max(setting, floor)
manual_mode                          → blocked, action=manual_hold
override_freeze_until > now          → blocked, action=freeze_hold
last_change_ts < 30 min ago          → blocked, action=rate_hold
setting == current                   → no write (action remains 'hold')
all clear AND changed                → _set_l1(setting), action=set/hot_safety/overheat_hard
```

Code: `_control_loop` 423–581.

### 3.2 Left zone — body/safety paths inside `_compute_setting`

| Path | Trigger | Effect |
|---|---|---|
| `pre_sleep_stage` | stage in `("inbed","awake")` | force `-10`; bypass body FB, learning, room comp; data_source=`pre_sleep_precool` |
| `in_initial_bed_cooling` | `0 ≤ mins_since_occupied ≤ 30` | force `-10`; same bypass; data_source=`initial_bed_cooling(Nm)` |
| `_setting_for_stage` | stage ∈ {deep,core,rem,awake,inbed} | **replaces** `base_setting` (NOT a delta) → see §8 H3 |
| body FB cold | `cycle≥1` AND `body_left < 80` | `+round(min(1.25·Δ, 5))` to base_setting |
| `learned_adj_pct` | `ENABLE_LEARNING` | adds clipped (`±30`) blower pts to target_blower_pct |
| `room_temp_to_blower_comp` | always | linear in pts; extra knee below 63°F |
| `overheat_hard` rail | `body_avg≥90` for 2 polls AND HA flag on | force blower pts ≥ proxy(-10); hysteresis at 86°F; **default off** |
| `hot_safety` | `body_avg>85` for 2 polls | force blower pts ≥ proxy(`current-1`) — note anchored to `current` not `base` (H5) |

### 3.3 Right zone — `_right_v52_shadow_tick` actuation gates

```
not RIGHT_LIVE_ENABLED                       → blocked='off'
not ha_flag_on                               → blocked='ha_flag_off'
not occupied (occupied_right)                → blocked='unoccupied'
in_bedjet_window AND not in_initial_bed_cooling → blocked='bedjet_window'
right_zone_override_until > now              → blocked='override_freeze'
last right_zone_last_change_ts < 30 min      → blocked='rate_limit'
proposed == firmware_setting                 → blocked='no_change'
proposed not in [-10, 0]                     → blocked='out_of_range'
otherwise                                    → _set_l1_right(proposed), actuated=True
```

Code: 720–741.

### 3.4 `right_overheat_safety.py` gates

```
input_boolean.snug_right_overheat_rail_enabled != 'on'  → release if engaged, return
binary_sensor...occupied_right != 'on'                  → release if engaged, return
body_left_f read fails                                  → no streak update, no engage
mins_since_occupied ≤ 30 (BedJet window)                → suppress: streak forced to 0
body_left ≥ 86 → streak++                               → engage when streak≥2
body_left < (82 if engaged else 86) → streak=0          → release if was engaged
on engage: snapshot current setpoint, force -10
on release: restore snapshot
```

### 3.5 Kill-switch paths

| Switch | Where | What it does |
|---|---|---|
| `manual_mode` (in-state) | `_check_kill_switch` (1162) on 3 manual changes / 5 min | All future ticks `manual_hold`; persists for the rest of the night until `_on_sleep_mode` resets it (981) |
| `input_boolean.snug_right_controller_enabled` | UI | Forces right path to `blocked='ha_flag_off'`; takes effect next tick |
| `input_boolean.snug_overheat_rail_enabled` | UI | Disables left hard-overheat rail |
| `input_boolean.snug_right_overheat_rail_enabled` | UI | Disables right safety app; **also** triggers immediate `_release` if engaged |
| `RIGHT_LIVE_ENABLED` | code | Code-side arm; AND with HA flag |
| Stop AppDaemon | nuclear | nothing runs |

---

## 4. Override handling per zone — the LEFT vs RIGHT divergence

This is the part where the two zones look superficially symmetric but actually behave very differently.

### 4.1 LEFT (`_on_setting_change`, 1037–1085)

Triggers when `number.smart_topper_left_side_bedtime_temperature` changes:

1. Skip if not sleeping.
2. **Self-write suppression:** `if expected == new_val: return` where `expected = self._state["last_setting"]`. This is set in `_set_l1` line 1313 — but **set AFTER `call_service`**, so there is a race window where HA fires the state callback before `last_setting` is updated. (PROGRESS_REPORT H2.)
3. Append timestamp to `recent_changes`, prune old, call `_check_kill_switch` → if `len(recent) ≥ 3` within 300 s, `manual_mode = True`.
4. Set `override_freeze_until = now + 60 min`.
5. Set `override_floor = min(new_val, MAX_SETTING)` — **floor is the new value, no matter the direction.** This means a *cold* override floor (e.g., `-10`) caps the controller from ever going warmer than `-10` for the rest of the night via the floor (it can only be moved by another override). A *warm* override floor (e.g., `-5`) caps the controller from going cooler than `-5`. The asymmetry: PROGRESS_REPORT §10 H4 says "cold overrides have no all-night floor" — but that's wrong as written; the line 1066 `floor = min(new_val, MAX_SETTING)` *does* set a cold floor. What's actually asymmetric is the *application* at line 514: `if floor is not None and setting < floor: setting = floor` only fires when the computed setting is *cooler* than the floor (i.e., the floor is a warm-side guardrail). A cold `floor` of `-10` would only kick in if `setting > -10` and `floor=-10` … which is `setting < -10` … never. So in practice the floor is a no-op for cold overrides. Net effect = warm-only floor, even though the state field is set both ways.
6. `last_setting = new_val`, `last_target_blower_pct = pct(new_val)`, `override_count++`.
7. PG insert via `_log_override` (action=`override`, has `override_delta`).

### 4.2 RIGHT (`_on_right_setting_change`, 1087–1130)

Triggers when `number.smart_topper_right_side_bedtime_temperature` changes:

1. Skip if not sleeping.
2. Skip if `new_val == old_val`.
3. **Self-write suppression:** `expected = self._state["right_zone_last_setting"]` set in `_set_l1_right` line 1135 **BEFORE `call_service`** — so the right path is *correctly* race-free here (a small but real inconsistency vs the left path).
4. **Kill-switch:** **none.** The right path does not call `_check_kill_switch`, does not add to `recent_changes`. Three rapid right-zone overrides will *not* drop the right zone into manual_mode.
5. **Override floor:** **none.** `override_floor` is a left-only field; the right zone has only `right_zone_override_until` (60-min freeze). After the freeze expires, the right controller is free to slide back to whatever cycle baseline + body FB says — including straight into the user's last manual direction.
6. **Override-freeze only set when armed:** lines 1114–1120 — `if RIGHT_LIVE_ENABLED and ha_flag_on: ...`. If the live arm is off, manual changes log but **do not establish any freeze** (because the controller wasn't fighting them anyway).
7. PG insert via `_log_override`.

### 4.3 Divergences that matter for v6 design

| Aspect | Left | Right |
|---|---|---|
| Self-write race | bug (set after call_service) | correct (set before) |
| Kill switch | yes (3-in-5min) | **none** |
| Warm-side override floor | yes (silent if cold) | **none** |
| Override freeze duration | 60 min | 60 min (but only when armed) |
| Rate limit | 30 min | 30 min |
| Recent-changes window | 5 min | n/a |
| Logged action when blocked | `manual_hold`/`freeze_hold`/`rate_hold` | `passive` (because PG row comes from `_log_passive_zone_snapshot`, not the right tick) |
| L2/L3 written by controller | no (only `bedtime_temperature`) | no |

**Crucial v6 implication:** the right zone has **only the 60-min freeze** as an override response. After the freeze, baseline + body FB silently restore the controller's choice — that's the override-absence trap (§5). The left zone has the warm-only floor *plus* freeze *plus* kill switch — three layers, of which only the freeze and kill switch actually fire on cold-side overrides.

---

## 5. Structural issues v6 must NOT reproduce

### 5.1 Override-bias trap
Cycle baselines are fit from override-only events (rc_synthesis methodology, PROGRESS_REPORT §6). Overrides are ~1 % of minutes and only the moments v5 was wrong; naive ML on them shifts toward the override sample mean and overshoots, then gets re-corrected by the next override. v5.2 partially mitigated this by adding closed-loop body FB, but the underlying *baselines* (`CYCLE_SETTINGS` and `RIGHT_CYCLE_SETTINGS`) are still override-fit. PROGRESS_REPORT §5 documents the failed v6 smart_baseline replication of the failure.

### 5.2 Override-absence trap — right zone
On the right zone, after a 60-min freeze the controller rolls back to baseline+FB with no memory of the user's manual direction. The user gives one signal per 60 min and the controller ignores it forever after. In contrast the left zone *does* keep the warm-side floor (when warm). Worse: with `bedtime_temperature` being the only dial we touch, even setting it back to baseline can be experienced as "the controller fought me again" 60 min later.

Last night's right-zone single override (`-4 → -5` at 03:25, asking *cooler*) shows the cold-side flavor of this trap: the right controller has *no* concept of an override floor at all, so even on the left-zone semantics it would have done nothing for the cold-direction request beyond the 60-min freeze.

### 5.3 Thin c4/c5 samples
PROGRESS_REPORT §3.1 and the comment in lines 76–77: per-cycle override counts are `c1=11, c2=7, c3=10, c4=5, c5=7, c6=9`. c4=5 and c5=7 are statistically meaningless on their own — yet `CYCLE_SETTINGS[4]=-5` and `[5]=-5` were set by shrinkage prior on those tiny samples. Right zone is an order of magnitude worse: 6 total overrides → `RIGHT_CYCLE_SETTINGS` per-cycle n is in {0,1,2}.

### 5.4 No occupancy-triggered learning
`_learn_from_history` (1686–1757) runs once at sleep_mode-on (1000) and uses a 14-day SQL window over PG `controller_readings` filtered by `controller_version`. It does *not* re-fit during the night, does not see the night's own overrides until the next bedtime. For every changed `controller_version` (e.g., the v5 → v5.2 cutover at 22:51 on 2026-04-30) the learning corpus is empty and `_learned = {}` (silent no-op of the entire `+learned` term). On any v6 cutover the learner will be useless until ~14 nights of new data accumulate.

### 5.5 No proxy comfort signal in the live loop
The discomfort-proxy work (`ml/discomfort_label.py`, `sig_movement_density`) lives offline only. The live loop ingests body sensors, room temp, sleep stage, bed presence — **no movement / arousal signal**. Yet the movement-density signal is the only path to actually capturing the wife's discomfort given her n=6 override corpus.

### 5.6 Other structural issues already in the audit backlog (PROGRESS_REPORT §10) v6 should preserve fixes for
- **C5** stale `ml/state/fitted_baselines.json` (shadow-only consumer)
- **H1** rail engagement state not persisted on rail-only mutations
- **H2** `_set_l1` race vs `_on_setting_change`
- **H3** `_setting_for_stage` clobbers v5.2 baselines (a `deep` event jumps c4=-5 to -10 in one tick)
- **H4** cold overrides have no all-night floor (see §4.1 #5 above for the actual mechanism)
- **H5** `hot_safety` anchored to `current_setting` not `max(base, override_floor)` → erodes warm overrides one step every 5 min
- **M11** right-zone live writes never land in `controller_readings` (verified empirically: 0 `set` rows for right v5.2 last night)

---

## 6. L_active vs L1 — where v5.2 reads which dial

`tools/lib_active_setting.py` exists and is well-specified. **`sleep_controller_v5.py` does not import it.** Search:

```
grep -n "lib_active_setting\|active_setting" appdaemon/sleep_controller_v5.py  →  no matches
```

Where v5.2 touches the firmware dials:

| Site | Reads | Writes | Uses L_active? |
|---|---|---|---|
| `_read_zone_snapshot.setting` (1528) | L1 only (`bedtime`) | n/a | **No** — assumes L1 = active dial |
| `_set_l1` (1310) | n/a | L1 (`bedtime`) | n/a |
| `_set_l1_right` (1132) | n/a | L1 (right `bedtime`) | n/a |
| `_on_setting_change` callback (394) | L1 listen | n/a | **No** — manual L2/L3 changes are invisible |
| `_on_right_setting_change` callback (396) | L1 listen | n/a | **No** |
| `_end_night` (1614–1620) | reads L2/L3 once at end-of-night, only if `3_level_mode == 'on'` | n/a | partial — but only for the nightly_summary dump, not control |
| firmware status: `E_PROFILE_3LEVEL = switch.smart_topper_left_side_3_level_mode` (286) | ✗ never read except in `_end_night` | n/a | the switch is *not* checked in the control loop |

**This is the L1-misuse the task brief asks about, in three concrete forms:**

1. **Control reasons in L1 space, but the firmware may be reading L2 or L3.** When `3_level_mode = on` (PROGRESS_REPORT confirms it has been on through the audit window for the right side; left side is RC-off so 3-level should be moot but is not explicitly forced off in the loop — only RC is, line 431), the firmware advances dials by `run_progress`. `_set_l1(value)` writes to the L1 dial regardless; if the live phase is L2/L3, the write is silently parked until `run_progress` rolls back.
2. **L1_TO_BLOWER_PCT is keyed by L1.** Lines 201–234. All blower-proxy math (room comp, learned adj, base→target conversion, snap back to a setting) uses L1's blower mapping. If the firmware was actually on L2 = -4 because `run_progress` is in the sleep band, our proxy thinks blower is at L1's value and scales/snaps off the wrong rung.
3. **Override classification reads L1.** A user knob-twist on the L2 (`sleep_temperature`) or L3 (`wake_temperature`) entity is *not* listened to and not classified as an override. If she changes the sleep dial mid-night, our PG corpus misses it and the freeze never fires.

**Mitigations actually present:**
- Left side has `_ensure_responsive_cooling_off` (1365) — *RC* is forced off but **3-level mode is not**. If the L1-only assumption is to hold for the left zone the controller should also force `3_level_mode = off` on the left (PROGRESS_REPORT §"Right-zone v5.2" claims 3-tier is off on the right side; nothing in code enforces it on the left).
- Right side: PROGRESS_REPORT line 168 ("Architecture matches user's left-zone pattern: ... 3-tier schedule: off") — but again, no code in `sleep_controller_v5.py` enforces or reads this. It's a manual-config assertion.

**Conclusion:** v6 must (a) explicitly disable 3-level mode at sleep_mode-on for any zone we control, OR (b) integrate `lib_active_setting` and write to the *active* dial, listen on all three dials for overrides, and key proxy lookups by L_active. Today the entire v5.2 codebase silently assumes L1 = active.

---

## 7. Coordination with `right_overheat_safety.py` — who owns what

Two AppDaemon apps run in the same process; they share *no state object* and *no IPC*. Coupling is purely through the HA entity bus.

### 7.1 `right_overheat_safety.RightOverheatSafety` owns
- Reading `binary_sensor.bed_presence_2bcab8_bed_occupied_right`.
- Reading `sensor.smart_topper_right_side_body_sensor_left`.
- Tracking its own `occupied_since` (its own state file `right_overheat_safety_state.json` in `STATE_DIR`).
- Writing `number.smart_topper_right_side_bedtime_temperature` to `-10` on engage and back to its `snapshot_setting` on release.
- Persisting `engaged`, `streak`, `snapshot_setting`, `engaged_at`, `released_at`, `engage_count_session`, `occupied_since`, `last_occupied`.

### 7.2 `SleepControllerV5` owns (right zone)
- Reading the same right-side sensors plus body_center, body_right, ambient, setpoint, blower_pct, room temp, sleep stage.
- Tracking its own `right_zone_occupied_since` (its own onset clock, **not** synced with the safety app's clock).
- Tracking `right_zone_last_change_ts`, `right_zone_override_until`, `right_zone_last_setting`.
- Writing the same `number.smart_topper_right_side_bedtime_temperature` (when armed).
- Logging `_right_v52_shadow.jsonl` and the passive-zone PG snapshots.

### 7.3 Conflict surface
- **Both write the same entity.** If the safety app engages and forces `-10`, then the v5.2 right tick fires within the next 5 min, the v5.2 path may compute proposed=-8 (cycle baseline), see `firmware_setting=-10`, classify it as "no_change wait we're cooler? but proposed is -8 not -10" — line 732 `proposed == firmware_setting` doesn't hit, then 734 `proposed < -10 or proposed > 0` doesn't hit → **v5.2 will write `-8` over the rail's `-10`**, releasing the safety force *before* the rail's release condition triggers.
  - This is mitigated only by chance: v5.2 rate-limit (30 min) usually fires first via `right_zone_last_change_ts`.
  - But on a fresh night with no prior controller write, the rate gate doesn't block, and the freeze gate is also empty (because the safety app's write to `bedtime_temperature` doesn't hit `_on_right_setting_change`'s freeze block — that block only fires `if RIGHT_LIVE_ENABLED and ha_flag_on`, but the listener does run; however the freeze is set on *any* unexpected change, so the rail's write **does** trigger a 60-min override-freeze on v5.2). So the rail's engage *does* freeze v5.2 for 60 min — accidentally, via the override-detection callback (line 1112). On rail *release*, the rail writes `snapshot_setting` (whatever was there before engage), which *also* trips `_on_right_setting_change` and starts another 60-min freeze.
- **Both have a BedJet 30-min suppression window**, but **they track onset independently**. Safety app uses its own `_state["occupied_since"]` (right_overheat_safety.py 192–195). v5.2 uses `_state["right_zone_occupied_since"]` (sleep_controller_v5.py 638–639). If AppDaemon restarts mid-night, the two reset independently and may disagree on whether we're in the BedJet window.
- **Onset semantics differ.** Safety app sets `occupied_since` on every off→on transition (M4 in §10 audit backlog: BedJet window restarts on every bed re-entry). v5.2 right-tick does the same (line 638). So a 3 AM bathroom break re-arms a fresh BedJet 30-min suppression on both apps.

### 7.4 Net partition
Safety app = **uncontested final word above 86°F skin** (it gets to write to the entity any second; v5.2 is on a 5-min tick and is rate-limited anyway).
v5.2 right path = **owns sub-86°F policy** but is at the mercy of the freeze-on-rail-release pattern, so after each rail engage/release v5.2 effectively goes silent for 60 min × 2 = 2 hours.

v6 should either move both behaviors into one app, or (cheap fix) have the safety app set a known-controller `right_zone_last_setting` flag that the v5.2 self-write suppression can recognize so the rail's writes don't trigger user-override freezes.

---

## 8. Drift between docs and code

| Doc claim | Code reality |
|---|---|
| PROGRESS_REPORT §7 ("v5's heuristic algorithm in one paragraph"): "learned per-cycle blower-percentage adjustment (clipped to ±15%)" | `LEARNING_MAX_BLOWER_ADJ = 30` (line 346). Clipped to **±30%**, not ±15%. |
| PROGRESS_REPORT §10 H4: "Cold overrides have no all-night floor" | Code *sets* `override_floor = min(new_val, MAX_SETTING)` for both directions (line 1066), but the *application* at line 514 (`if setting < floor`) makes it a no-op for cold-direction floors. The audit finding is correct in effect, wrong in cause. v6 should either symmetrize or remove the cold-path state mutation. |
| PROGRESS_REPORT §"v5.1 update": "`CYCLE_SETTINGS` shipped from `[-10,-9,-8,-7,-6,-5]` to `[-10,-8,-7,-5,-5,-6]`" | Current code is `[-10,-10,-7,-5,-5,-6]` (lines 78–89). c2 was reverted from `-8` to `-10` after the bimodal-distribution analysis described in lines 70–74. PROGRESS_REPORT §1 ("Where we are") *does* say `[-10,-10,-7,-5,-5,-6]`. PROGRESS_REPORT §2 v5.1-update paragraph is stale. |
| PROGRESS_REPORT: "`switch.smart_topper_<side>_3_level_mode` is ON" (audit-window assumption) | `sleep_controller_v5.py` does not check this switch in the control loop and does not force it off. PROGRESS_REPORT §"Right-zone v5.2 — 2026-04-30" claim "3-tier schedule: off" is a manual config statement, not enforced by the controller. |
| PROGRESS_REPORT §"Recommended action items / Done today": "L1_TO_BLOWER_PCT comment corrected" | The repo has the corrected comment (lines 201–222). The deployed file does **not** have the comment — only repo HEAD does. The functional table is identical (the diff to deployed is comments-only on this block). |
| PROGRESS_REPORT §7 deployed-state table: "right v5.2 ... live actuation gated on input_boolean.snug_right_controller_enabled" | Code matches — but verified empirically: 0 right-zone `set` rows in PG last night because the path emits to JSONL only (M11). The doc does not call out that PG is blind to right-zone live actuation. |
| Module docstring (lines 1–27): "Right side remains passive-only telemetry" | Stale. With `RIGHT_LIVE_ENABLED=True` and HA helper on, right side actively writes. The class docstring claim is now false. |
| `_setting_for_stage` (962): table `{deep:-10, core:-8, rem:-6, awake:-5, inbed:-9}` | This is unfit v5-era data, applied **before** v5.2 body FB and **replaces** `base_setting` whenever a non-`unknown` stage is present (line 861). PROGRESS_REPORT H3 documents this as open. The v5.2 body-FB and cycle-baseline machinery is silently override-able by a single 'deep' Apple Health event. |

---

## 9. Per-tick data flow — a v6 design map

```
sleep_mode=on     ┐
                  ▼
_on_sleep_mode  → reset state, learn_from_history (PG SELECT, 14-day window),
                  initial _set_l1(INITIAL_BED_LEFT_SETTING=-10)

every 300 s     ┐
                ▼
_control_loop:
  if not _is_sleeping(): return
  _ensure_responsive_cooling_off()          # left RC watchdog
  read room_temp, sleep_stage, left_snap, bed_presence
  body_occupied = _check_occupancy(body_max, now)         # body sensor heuristic
  left_mins_since_onset = _update_zone_occupancy_onset(   # uses ESPHome occupancy first
      'left', _zone_occupied_from_bed_presence(...))
  if not body_occupied: PG empty_bed row; right passive snapshot; return
  _ensure_topper_running(now)                              # auto-restart watchdog
  if not sleep_start: return

  plan = _compute_setting(elapsed_min, room_temp, sleep_stage, body_avg,
                          body_left, current_setting, mins_since_occupied=
                          left_mins_since_onset)
    → if pre_sleep_stage or in_initial_bed_cooling: forced -10, no other terms
    → base_setting = CYCLE_SETTINGS[cycle_num]
    → if sleep_stage in {deep,core,rem,awake,inbed}: base_setting = stage table
    → body FB cold:  base_setting += round(min(1.25·(80-body_left), 5))    (cycle≥1)
    → base_blower_pct = L1_TO_BLOWER_PCT[base_setting]
    → target_blower_pct += learned_adj_pct                                (clipped ±30)
    → target_blower_pct += room_temp_to_blower_comp(room_temp)
    → if hard rail flag on AND body_avg≥90 streak:  target ≥ proxy(-10)
    → if body_avg>85 streak:                         target ≥ proxy(current-1)
    → setting = blower_pct_to_l1(clip(target, 0, 100))                    (snap to ladder)

  setting = max(setting, override_floor) if floor set                      (warm-side floor)
  if manual_mode:        action='manual_hold', no write
  elif freeze active:    action='freeze_hold', no write
  elif rate active:      action='rate_hold',   no write
  elif setting==current: action='hold',        no write
  else:                  _set_l1(setting); action='set'/'hot_safety'/'overheat_hard'

  _log_to_postgres(left fields) +
  _log_passive_zone_snapshot('right')         # right PG row is ALWAYS passive
  _shadow_log_decision('left', ...)           # JSONL via ml.policy
  if right_snap.body_left:
    _shadow_log_decision('right', ...)
  if RIGHT_SHADOW_ENABLED:
    _right_v52_shadow_tick(...)               # right v5.2 + actuation (own JSONL only)

every 60 s (other app):
right_overheat_safety._tick → engage/release on body_left_R skin channel
```

---

## 10. Recommendations for v6 (kept short — other agents will design)

- Make L_active first-class. Read 3-level switch + `run_progress` + T1/T3 (`number.*_start_length_minutes` / `*_wake_length_minutes`) + L1/L2/L3, route writes and reads through `lib_active_setting`. Listen for overrides on all three dials.
- Either: (a) explicitly force `3_level_mode = off` on every controlled zone at sleep_mode-on, AND assert it on every tick (mirroring `_ensure_responsive_cooling_off`), OR (b) commit to the L_active machinery and stop pretending L1 is the dial.
- Symmetrize override floor (or document: warm-only by design). Apply same kill-switch semantics to right side or document why not.
- Move right-zone live actuation rows into `controller_readings` (M11) so PG is the single source of truth.
- Reconcile `_setting_for_stage` with v5.2 — make it a delta on top of `base_setting`, not a replacement; OR fit a stage-aware baseline; OR retire it.
- Single-app integration of safety + body-FB on the right zone, OR a self-write contract so the safety app's writes don't accidentally trip 60-min override-freezes.
- Live discomfort signal in the loop (movement density), at least as a freeze-extend / freeze-shorten signal initially.
- Be cautious: yesterday's only v5.2 night had 0 controller writes on the left (kill-switch fired in c2) and 0 writes-logged-to-PG on the right (M11). The runtime evidence base for v5.2 is almost zero. Don't refit on it.

---

## 11. Appendix — verified runtime evidence (last 7 nights, PG)

| controller_version | zone | rows | overrides | sets | passive | hold | freeze_hold | rate_hold | hot_safety | manual_hold | empty_bed |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| v5_2_rc_off | left  | 124 | 5 | 0 | 0 | 33 | 1 | 0 | 0 | 85 | 0 |
| v5_rc_off   | left  | 816 | 14 | 16 | 0 | 574 | 138 | 74 | 0 | 0 | 0 |
| v5_2_rc_off | right | 120 | 1 | 0 | 119 | 0 | 0 | 0 | 0 | 0 | 0 |
| v5_rc_off   | right | 805 | 3 | 0 | 802 | 0 | 0 | 0 | 0 | 0 | 0 |

First v5.2 row: 2026-04-30 22:51:46 ET (left) / 22:53:03 ET (right). Last: 2026-05-01 08:43 ET both. Only one v5.2 night exists.

Last-night left-zone trigger sequence (all UTC offset removed):
```
01:37:49  override   -10 → -9  (Δ=+1, body_avg=80.3)
01:38:03  freeze_hold      -9
01:38:20  override    -9 → -8  (Δ=+1, body_avg=80.0)
01:39:23  override    -8 → -6  (Δ=+2, body_avg=80.3)
01:43:03  manual_hold      -6  ← kill switch latched (3 changes in 94 s)
... 84 more manual_hold rows through 08:43 ...
```

Recorder fix verified: `/config/configuration.yaml.bak.20260501` (May 1 18:00 ET) vs current diff removes
`sensor.smart_topper_*_pid_*`, `sensor.smart_topper_*_heater_*_raw`, `sensor.smart_topper_*_run_progress`
from `recorder.exclude.entity_globs`.

Deployed `sleep_controller_v5.py` differs from repo HEAD only by missing the L1_TO_BLOWER_PCT clarifying comment block (lines 202–222 in repo). Functionally identical. `right_overheat_safety.py` matches deployed exactly.
