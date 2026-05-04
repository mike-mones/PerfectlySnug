# PerfectlySnug — Control Policy Design (2026-05-04)

**Status:** design proposal · **Scope:** the `(state, user_offsets, prev) → action` mapping that consumes the §2026-05-04 state estimator and emits the dial setting written to `number.smart_topper_<zone>_side_bedtime_temperature`. **Companions:** `docs/proposals/2026-05-04_state_estimation.md` (input), `docs/findings/2026-05-04_context_audit.md` (motivation, esp. findings #1 + #4).

This proposal retires audit finding #1 (cycle-index axis encoded in 6 places) and finding #4 (16-token patch chain on a single function). It is the **third** of three layers in the v6+ stack:

```
state_estimator  →  control_policy  →  safety_layer  →  HA write
   (this doc's input)   (THIS DOC)      (untouched)       (untouched)
```

The control policy is a **pure function**. No I/O, no clocks, no PG, no HA reads. Easy to unit-test against the state estimator's output enum.

---

## 1. Action space

The policy returns a single dataclass:

```python
@dataclass(frozen=True)
class Action:
    setting: int            # ∈ [-10, 0]   the dial to write (never positive)
    reason: str             # human-readable, logged
    confidence: float       # passes through state.stability_confidence
```

### 1.1 What is NOT in the action

- **No `blower_pct`.** Verified by code audit: `_set_l1` (`sleep_controller_v5.py:1852-1855`) is the *only* dial-write path on the left, and it calls `number/set_value` on `number.smart_topper_left_side_bedtime_temperature`. The right zone uses `_apply_right_setting` (`:1442`) which writes the same `number.*_bedtime_temperature` entity. There is **zero** `switch.*blower*` or `number.*blower*` write in the controller. The internal `target_blower_pct` field is a **proxy quantity** for logging and learner math only — never actuated. Per the findings audit (§3.4), Stage-1 setpoint °F is approximately linear in the dial; the firmware's PID computes blower% from `setpoint − body_delta` on a slow internal cadence. The policy's only lever is the integer dial.

- **No clock value.** `elapsed_min`, `cycle_num`, `time_of_night`, `night_progress`: forbidden. The state estimator already absorbed `seconds_since_presence_change` as a weak prior on `WAKE_TRANSITION` (state-est §5); the policy may not re-introduce it.

- **No regime label.** The `regime` enum is the v6 transition layer; it is being replaced by `state`. The policy does not branch on `regime`.

### 1.2 Setting cap (hard, file-level)

`MAX_SETTING = 0` (`sleep_controller_v5.py:226`). The policy clamps to `[-10, 0]` after every arithmetic step. This is the single line that enforces "user runs warm — never heat under any circumstances" (PRD §3, user fact). The policy carries this invariant by construction, not by safety layer.

---

## 2. State → base action table

The policy is **primarily a lookup**, not a function. The base setting per state is fixed, modulated only by user offsets, rate limits, and the comfort floor (§5).

| State | base_setting | dial floor (clamp colder) | max_delta_per_tick | rationale (1 line) |
|---|---|---|---|---|
| `OFF_BED` | **0** | n/a (forced 0) | n/a | No body load; firmware should idle. Body sensors at room+3°F are noise — `body_fb` MUST be off (state-est §6.4). |
| `AWAKE_IN_BED` | **−2** | clamp ≥ −5 | ±2 | Sheets unequilibrated, body sensors invalid for ≥10 min after onset (state-est §2.3, `BODY_VALID_WARMUP_S=600`). Aggressive cooling here = freezing-air complaint (audit §2.1, 2026-05-02 morning). |
| `SETTLING` | **−4** | clamp ≥ −8 | ±1 | Movement decreasing, body warming toward steady-state. This is where v5.2's cold-room comp + body_fb double-fire bug lived (audit §2.2). One-step ramping prevents outrunning the body. |
| `STABLE_SLEEP` | **−5 + user_offset** | clamp ≥ −10 | ±1 (with §4 soft cap) | Most of the night by tick-count. The user offset is the *primary lever*; base is the prior. −5 is the empirical mid-night convergence point from the v5.1 baseline refit (`CYCLE_SETTINGS[c4]=−5, c5=−5`, `sleep_controller_v5.py:84-86`). |
| `RESTLESS` | **prev_setting − 2** (i.e. 2 colder than current) | clamp ≥ −10 | ±2 cooler only | Variance spike in stable period ⇒ user is too warm (the user only owns "cool harder" overrides, audit §2.6 / state-est §6.4). One-tick bias, then re-evaluate. |
| `WAKE_TRANSITION` | **−3** | clamp ≥ −3 (comfort floor, §5) | ±1 | Rising body trend + rising movement after 5h+. Addresses "too hot near wake-up" (audit §2.3) and "warm later in the morning" (`sleep_controller_v5.py:88-89` comment). The firmware caps blower regardless of dial in this regime; cooler dial buys nothing. |
| `DISTURBANCE` | **hold prev_setting** | n/a | **0** (frozen) | Single-event spike (partner motion, BedJet, dog). Must not transition behavior. State-est §3.2 Rule 3 already labels this; policy honors the freeze. |

### 2.1 Why these specific numbers

- The numeric grid is intentionally one step **softer** than v5.1 cycle baselines [−10, −10, −7, −5, −5, −6]. The audit established that cycle-1 −10 caused the bed-onset override storm (audit §2.1, "user climbed in at 23:36, immediately overrode `−4 → −7 → −10`" — note: the −10 *was the user's preference*; what hurt was the controller's *cold spike before* presence was confirmed). Starting at −2/−4 in `AWAKE_IN_BED`/`SETTLING` lets the user's offset learning *raise* the baseline if their body wants it, without our controller pre-committing to −10 on stale empty-bed body data.
- −5 in `STABLE_SLEEP` matches the override corpus mid-night posterior mean (`c3=−6.0`, `c4=−3.0`, `c5=−2.86`, `sleep_controller_v5.py:76`). The user's *learned offset* (next layer) shifts this; the policy's prior is the conservative middle of that distribution.
- `WAKE_TRANSITION = −3` directly negates the v5.1 `c6=−6` "intentional non-monotonic dip" (`sleep_controller_v5.py:88-89`). The audit explicitly called that hand-tuning a tell of overfit-to-noise (audit §5 row 1).

---

## 3. User-offset application order

The policy composes the final setting in this **strict** order:

```
1.  base_setting       = STATE_BASE[state]                     # §2 lookup
2.  candidate          = base_setting
                       + user_offset[user, state]              # §3.1 — learned per-state per-user
                       + global_offset[user]                   # §3.2 — learned night-mean residual
3.  candidate          = max(STATE_FLOOR[state], candidate)    # comfort floor (§5)
4.  candidate          = clamp(candidate, -10, 0)              # action-space cap (§1.2)
5.  if confidence < 0.5:
        candidate     += 1                                     # asymmetric softening (§5)
        candidate      = clamp(candidate, -10, 0)
6.  final_setting      = rate_limit(prev_setting, candidate,
                                    state, ticks_since_change) # §4
```

**Order matters.** Clamping before rate-limiting (step 4 before step 6) means the user offset can saturate against the [-10, 0] cap; the rate limiter only sees feasible targets. Confidence softening (step 5) runs *after* clamping, so it cannot push the candidate out of bounds.

### 3.1 `user_offset[user, state]` shape

A 2-user × 7-state table of integers in `[-3, +3]`, learned offline from override deltas filtered by state label (the learning doc spec). Defaults to all zeros on first deploy. Bounded so a single bad night cannot warp the policy by more than 3 steps.

### 3.2 `global_offset[user]` shape

A scalar `int` in `[-2, +2]` per user, captured from the rolling 14-night mean of `(observed_user_setting − policy_setting)` during `STABLE_SLEEP` only. This is the "the user just runs colder than our prior" knob.

The policy itself never modifies these tables. They are read at controller startup and refreshed nightly (same cadence as the state-est percentile cache, state-est §4).

---

## 4. Rate limiting (anti-oscillation)

| Rule | Bound | Rationale |
|---|---|---|
| Hard per-tick cap | `\|final − prev\|` ≤ **2 dial steps** at every tick | Prevents the 0 → −10 single-write jump that today's `_set_l1` permits (`sleep_controller_v5.py:1852-1855` — no step cap). |
| Soft cap, `STABLE_SLEEP` only | `\|final − prev\|` ≤ **1 step**, plus **4-tick hold** between deltas | Stable sleep is the no-op regime; one-step micro-corrections are sufficient. The hold prevents thrash on borderline body-trend signals. |
| State-transition hysteresis | A new state must persist **≥ 2 consecutive ticks** before its `base_setting` is applied. Single-tick state flips revert to the previous state's base. | Decision-tree boundary noise (movement RMS hovering near `p25`) was the audited failure mode (state-est §3.2 Rule 6 vs Rule 7 boundary). |
| Hysteresis exception | State transitions to/from a **degraded** state (state-est §6) skip hysteresis and apply immediately. | Fail-safe: when sensors die we want conservative behavior *now*, not in 2 ticks. |
| Safety bypass | Hysteresis + rate caps **do not apply** when the safety layer (§6) is engaged. | Already implemented (`sleep_controller_v5.py:646-666`); preserved verbatim. |

### 4.1 Tick cadence note

Today: control loop runs every **300 s** (`sleep_controller_v5.py:442`). The state-estimator design assumes a future 60s cadence (state-est §4). The policy is cadence-agnostic — `max_delta_per_tick` and the 4-tick hold both scale with whatever the configured tick is. The `STABLE_SLEEP` 4-tick hold = 4× tick interval = 20 min at today's cadence, 4 min at the planned 60s cadence. This is intentional: faster ticks ⇒ tighter physical control loop, softer hold.

### 4.2 What the rate limiter is NOT

- **Not** an alternative to the override freeze. The 60-min `OVERRIDE_FREEZE_MIN` (`sleep_controller_v5.py:298`) lives in the safety/coordination layer and gates the control loop entirely; the rate limiter only acts when the policy is allowed to act.
- **Not** a setpoint smoother. The firmware's internal PID does that. We rate-limit the *commanded dial*, not the *actuated blower*.

---

## 5. Asymmetric cost — overcooling > undercooling

For this user, an overcooled night triggers an immediate manual override and disturbs sleep; an undercooled night drifts toward warm without an override (audit §2.6 — *the user only owns "cool harder" overrides*). The policy encodes this asymmetry in three places:

### 5.1 Adjacent-state preference

When the state estimator returns a state with `confidence < 0.7`, and the previous state was warmer (smaller `|base|`), the policy **stays in the previous state** for the current tick (this is the §4 hysteresis already, but the asymmetry shows up because we never give the cooler state's base the benefit of low-confidence evidence). When the previous state was colder, the new (warmer) state's base **does** apply immediately.

### 5.2 Confidence softening

Encoded in step 5 of §3: `if confidence < 0.5: candidate += 1` (warmer). One-line implementation, applies to all states except `OFF_BED` (which is forced 0) and `DISTURBANCE` (which is held).

### 5.3 Comfort floor table

Per-state lower bound (the `dial floor (clamp colder)` column in §2). Even with a maximally-negative user offset, the policy cannot emit colder than the floor.

| State | comfort floor | drives which behavior |
|---|---|---|
| `OFF_BED` | force 0 | no actuation while empty |
| `AWAKE_IN_BED` | **−5** | bed-onset cap; was the source of the 2026-05-02 freezing-air complaint at −10 |
| `SETTLING` | −8 | leave room for `STABLE_SLEEP` to go further |
| `STABLE_SLEEP` | **−10** (no floor — full authority) | this is the regime where the user actually sleeps, full range allowed |
| `RESTLESS` | −10 (full) | the asymmetric "cool harder, never warmer" response |
| `WAKE_TRANSITION` | **−3** | the load-bearing line that addresses "too hot near wake-up" — never colder than −3, regardless of user offset or learned residual |
| `DISTURBANCE` | hold | n/a |

---

## 6. Safety layer (NON-BYPASSABLE, ABOVE policy)

The control policy runs *first*, producing `Action(setting, reason, confidence)`. Four safety checks then run in this fixed order; each may **veto** (replace `setting` with a forced value) or **block** (skip the write and re-emit `prev_setting`). The policy cannot influence them; they cannot be disabled by code change without flipping a labelled HA helper.

### Layer ordering (top wins)

1. **Right-zone overheat rail** — `right_overheat_safety.py`, runs in its own AppDaemon class on a 60s tick (`right_overheat_safety.py:131`).
   - Trigger: `body_left_f ≥ 86°F` for 2 consecutive polls (`right_overheat_safety.py:73, 80`)
   - Effect: writes `bedtime_temperature = -10` directly via `number/set_value` (`right_overheat_safety.py:297-298`)
   - Release: `body_left_f < 82°F` (hysteresis, `right_overheat_safety.py:81`)
   - BedJet suppression: first 30 min after right-bed onset (`right_overheat_safety.py:96`)
   - Coordination with policy: sets `input_boolean.snug_right_rail_engaged`; the policy's main controller honors this via `rail_engaged` actuation block (`sleep_controller_v5.py:914-926`).
   - **Bypasses** policy entirely. The rail is independent code, not a policy branch.

2. **Left-zone hard-overheat rail** — `sleep_controller_v5.py:1118-1138`.
   - Trigger: `body_avg_f ≥ 90°F` for 2 polls (`sleep_controller_v5.py:292-293`); release at `< 86°F` (`:294`).
   - Gated by `input_boolean.snug_overheat_rail_enabled` (`sleep_controller_v5.py:295`).
   - Effect: forces `target_blower_pct` to L1=−10 equivalent; bypasses freeze, manual mode, rate-limit (`sleep_controller_v5.py:646-653`).
   - Note: historical max body_avg_f in 30 nights = 88.6°F (`sleep_controller_v5.py:288-289`); this is a future-only safety net, never observed firing.

3. **Two-key arming (right zone live actuation)** — `sleep_controller_v5.py:195-207`.
   - Key 1: Python constant `RIGHT_LIVE_ENABLED = True` (`:204`).
   - Key 2: HA helper `input_boolean.snug_right_controller_enabled` (`:205`).
   - **Both** required for any write to `number.smart_topper_right_side_bedtime_temperature` from the policy. Either false ⇒ shadow log only.
   - Operational: HA helper is the instant kill switch; defaults OFF (`:201`). Audit §2.4 caught a silent-off case — keep this pattern but add a startup health-check that logs WARN if HA helper is off while `RIGHT_LIVE_ENABLED=True`.

4. **Override freeze** — `sleep_controller_v5.py:621-629, 1291-1293`.
   - Trigger: any user write to the `bedtime_temperature` entity (`_on_setting_change`, `:1259-1306`).
   - Effect: `override_freeze_until = now + 60 min` (`OVERRIDE_FREEZE_MIN=60`, `:298`); during freeze, `_control_loop` skips the policy's setting write entirely (`action="freeze_hold"`, `:625`).
   - Bypassable by safety layers 1 + 2 only (audit §2.5 + the existing `safety_bypass` flag, `:639-641`).

### 6.1 Currently-aspirational safety items (NOT in code today)

The user prompt asks for two safety items that are **not currently implemented**. Spec'd here for the policy interface, with deploy as a follow-up patch:

5. **Dead-man timer (heartbeat)** — *to-add.*
   - Spec: every successful policy invocation writes `now()` to `input_datetime.snug_controller_last_tick`. A separate AppDaemon class polls this every 60 s; if `now − last_tick > 720 s`, it forces `bedtime_temperature = 0` on both zones and fires a `persistent_notification`.
   - Justification: 720 s = 12 min = 2.4× the current 5-min tick, accommodates one missed cycle without false alarm.
   - File to add: `appdaemon/snug_deadman.py` (~80 LOC, modeled on `right_overheat_safety.py`).
   - Closes the gap that today's only liveness check is the 10:30 morning data-loss notification (`sleep_controller_v5.py:448-453`).

6. **Bedtime-override grace (`snug_user_lockout`)** — *to-add.*
   - Spec: `input_boolean.snug_user_lockout` toggled ON via HA mobile shortcut. While on, the policy emits `Action(setting=prev_setting, reason="user_lockout", confidence=0.0)` for both zones. Auto-clears after `LOCKOUT_GRACE_MIN = 90` minutes via `run_in` callback registered at the toggle.
   - Distinct from `OVERRIDE_FREEZE_MIN` (60 min, automatic on dial change) — this is **explicit "leave me alone tonight"** semantics.
   - File: 30 LOC addition to `sleep_controller_v5.py` `__init__` + 5-line guard at the top of `_control_loop`.

The control policy proposed in this doc is **forward-compatible** with both #5 and #6: they intercept the `Action` before the write, identical to the existing `freeze_hold` path.

---

## 7. Migration: cycle baselines → state-driven

### 7.1 v5.1 cycle table (current, to be deleted)

```python
CYCLE_SETTINGS = {1:-10, 2:-10, 3:-7, 4:-5, 5:-5, 6:-6}  # sleep_controller_v5.py:64-90
```

### 7.2 New mapping (state-driven, what cycles look like in practice)

A representative night under the new policy. State labels come from the state estimator (state-est §3.2). User offset assumed = −0 (first-night bootstrap); converged offset for this user ≈ −1 in `STABLE_SLEEP` per the override corpus.

| Wall time | Elapsed | v5.1 cycle base | v5.1 setting | New state | New base | + user_offset | Final (post-clamp + rate-limit) |
|---|---|---|---|---|---|---|---|
| 22:30 | −15 min | n/a | 0 | `OFF_BED` | 0 | 0 | **0** |
| 22:45 | 0 (sleep_mode on) | c1 = −10 | **−10 (cold spike — audit §2.1)** | `OFF_BED` (no presence yet) | 0 | 0 | **0** |
| 22:50 | 5 | c1 = −10 | −10 | `AWAKE_IN_BED` (presence transition) | −2 | 0 | **−2** |
| 23:00 | 15 | c1 = −10 | −10 | `AWAKE_IN_BED` | −2 | 0 | **−2** (held) |
| 23:15 | 30 | c1 = −10 | −10 | `SETTLING` | −4 | 0 | **−4** (Δ=−2 OK, hard cap) |
| 23:45 | 60 | c1 = −10 | −10 | `SETTLING` | −4 | 0 | **−4** |
| 00:15 | 90 | c2 = −10 | −10 | `STABLE_SLEEP` | −5 | −1 | **−6** (Δ=−2 OK) |
| 01:00 | 135 | c2 = −10 | −10 | `STABLE_SLEEP` | −5 | −1 | **−6** (held, soft cap) |
| 02:30 | 225 | c3 = −7 | −7 | `STABLE_SLEEP` | −5 | −1 | **−6** (held) |
| 03:30 | 285 | c4 = −5 | −5 | `STABLE_SLEEP` | −5 | −1 | **−6** |
| 03:35 | 290 | c4 = −5 | −5 | `RESTLESS` (one tick) | prev−2 = −8 | n/a | **−8** (asymmetric cool) |
| 03:45 | 300 | c4 = −5 | −5 | `STABLE_SLEEP` (recovered) | −5 | −1 | **−6** (back to base) |
| 05:00 | 375 | c5 = −5 | −5 | `STABLE_SLEEP` | −5 | −1 | **−6** |
| 06:00 | 435 | c6 = −6 | **−6 (intentional dip — addresses `WARM` complaint badly)** | `WAKE_TRANSITION` | −3 (floored at −3) | −1 → still −3 (floor) | **−3** ← addresses "too hot near wake-up" directly |
| 07:00 | 495 | c6 = −6 | −6 | `WAKE_TRANSITION` | −3 | −1 → −3 (floor) | **−3** |

### 7.3 Trajectory diff (single line per tick)

```
elapsed:    0    15    30    60    90   135   225   285   290   300   375   435   495
v5.1:    -10   -10   -10   -10   -10   -10    -7    -5    -5    -5    -5    -6    -6
NEW:       0    -2    -4    -4    -6    -6    -6    -6    -8    -6    -6    -3    -3
diff:    +10    +8    +6    +6    +4    +4    +1    +1    -3    -1    -1    +3    +3
         |--bed-onset softer--|  |---STABLE colder by user offset---|       |-wake warmer-|
```

The two arrows in `diff` are exactly the two audit findings being closed:
- **Bed onset (+10/+8/+6 warmer than v5.1):** kills the cold-spike-before-presence bug (audit §2.1).
- **Wake transition (+3/+3 warmer):** addresses "too hot near wake-up" (audit §2.3).

The colder excursion at elapsed=290 is the asymmetric `RESTLESS` response — a *one-tick* cool bias triggered by a movement variance spike, not a cycle-baseline change.

---

## 8. Removed behaviors (explicit deletes)

### 8.1 DELETE

| Item | File:line | Replaced by |
|---|---|---|
| `CYCLE_SETTINGS` dict | `sleep_controller_v5.py:64-90` | §2 state-base table |
| `RIGHT_CYCLE_SETTINGS` dict | `sleep_controller_v5.py:147-154` | same §2 table, right-zone column (separate doc) |
| `_get_cycle_num()` | `sleep_controller_v5.py:1180-1182` | nothing (audit §3.1: cycle-of-night is not a control axis) |
| `CYCLE_DURATION_MIN`, `cycle_num` field everywhere | `sleep_controller_v5.py:91, all `_state["current_cycle_num"]`` references | nothing |
| `_setting_for_stage()` table | `sleep_controller_v5.py:1066-1070, 1169-1178` | state estimator (which absorbs `sleep_stage` as one input among many, not a clobber) |
| `RegimeConfig.cycle_baseline_left/right` | `ml/v6/regime.py:80-85` | §2 table |
| `_normal_cool_base`, `_cycle_index` | `ml/v6/regime.py:316-325, 271-275` | §2 table |
| `policy.py` WAKE_COOL cycle-aware override | `ml/v6/policy.py:117-125` | §2 (`WAKE_TRANSITION` is its own state with floor −3) |
| Cold-room comp as a separate code branch | `ml/v6/policy.py:236-246`, `_room_temp_to_blower_comp` in v5 | subsumed: `STABLE_SLEEP` policy adds `+1` (warmer) when `room_f < 65°F` AND `body_trend_15min < +0.20°F/15m` (the partial fix from `regime.py:203-204` becomes the *only* fix). |
| 3-level mode watchdog explicit branch in `_compute_setting` | `sleep_controller_v5.py:517-519` (call site) | retained as init-time + tick-prelude assertion only; the policy itself does not branch on it. Audit confirmed it adds no policy behavior. |
| `_learn_from_history` EMA on per-cycle deltas | `sleep_controller_v5.py:2282-2356` | offline retrain on `(state → setting)` pairs (separate learning doc) |

### 8.2 KEEP (in safety layer, not policy)

- `right_overheat_safety.py` entire file
- `OVERHEAT_HARD_F=90.0` left rail (`sleep_controller_v5.py:292`)
- `OVERRIDE_FREEZE_MIN=60` (`:298`)
- `KILL_SWITCH_*` (`:300-301`) — 3 manual changes in 5 min ⇒ manual-mode-for-night
- Two-key arming (`RIGHT_LIVE_ENABLED` + `input_boolean.snug_right_controller_enabled`)
- Initial-bed event listener (`_on_bed_onset`, `:467-479`) — but it now schedules a tick that runs the *new* policy (which already has `AWAKE_IN_BED` as the right behavior), instead of calling `INITIAL_BED_LEFT_SETTING=-10`.

---

## 9. Determinism & testability

### 9.1 Function signature

```python
def policy(
    state: State,                    # enum, from state_estimator
    confidence: float,               # 0.0–1.0
    user_offsets: UserOffsetTable,   # immutable dataclass
    prev_setting: int,               # last commanded setting
    prev_state: State,               # for hysteresis + RESTLESS prev-relative base
    ticks_since_change: int,         # for soft cap in STABLE_SLEEP
    state_persisted_ticks: int,      # for hysteresis (≥2 to apply new base)
    state_degraded: Optional[str],   # 'movement' | 'body_validity' | 'both' | None
    room_f: Optional[float],         # for STABLE_SLEEP cold-room +1 (subsumed branch)
    body_trend_15m: Optional[float], # for STABLE_SLEEP cold-room gate
) -> Action:
    ...
```

No I/O. No `datetime.now()`. No `self.read_state()`. **All inputs are values.** The caller (`_control_loop` or `sleep_controller_v6.py`) marshals these from HA + the state estimator + its in-process counters.

### 9.2 Test file outline — `tests/test_v6_control_policy.py`

```python
# Per-state base output
test_off_bed_returns_zero_regardless_of_offset
test_awake_in_bed_base_minus_two_clamped_floor_minus_five
test_settling_base_minus_four
test_stable_sleep_base_minus_five_plus_user_offset
test_restless_base_is_prev_minus_two
test_wake_transition_base_minus_three_floored
test_disturbance_holds_prev_setting

# User offset application
test_user_offset_applied_after_base
test_global_offset_added_after_state_offset
test_offsets_cannot_violate_dial_clamp
test_offsets_cannot_violate_state_floor

# Rate limiting
test_hard_cap_two_steps_per_tick
test_stable_sleep_soft_cap_one_step_per_four_ticks
test_state_transition_hysteresis_two_ticks
test_degraded_state_skips_hysteresis
test_safety_bypass_skips_rate_limit  # via Action.metadata flag

# Asymmetric cost
test_low_confidence_softens_warmer_by_one
test_low_confidence_does_not_soften_off_bed
test_wake_transition_floor_minus_three_holds_against_user_offset
test_adjacent_state_low_confidence_prefers_warmer

# Cold-room subsumed branch
test_stable_sleep_cold_room_adds_warmer_one_when_body_trend_flat
test_stable_sleep_cold_room_no_op_when_body_trend_rising

# Pure-function invariants
test_policy_no_io                      # mock all stdlib I/O, assert never called
test_policy_deterministic_replay       # call 1000× with same inputs → same Action
test_policy_no_positive_setting_ever   # property test over 100k random inputs

# Trajectory regression (locks §7.3)
test_representative_night_trajectory
```

Target: ≥ 30 tests, ≥ 95 % branch coverage. Runtime budget: < 200 ms for the full file.

### 9.3 Property-based tests (via `hypothesis`, optional)

```python
@given(state=sampled_from(State), confidence=floats(0, 1),
       user_offset=integers(-3, 3), global_offset=integers(-2, 2),
       prev_setting=integers(-10, 0))
def test_action_setting_always_in_range(...):
    action = policy(...)
    assert -10 <= action.setting <= 0  # the §1.2 invariant
```

---

## 10. Logging

### 10.1 Per-invocation log line

Single structured INFO line per zone per tick, unconditional:

```
policy[left] state=STABLE_SLEEP conf=0.92 base=-5 user_off=-1 global_off=0
             pre_clamp=-6 post_clamp=-6 post_softening=-6 post_rate=-6
             prev=-5 deg=None reason=stable_sleep+user_offset
```

State-transition events get an additional INFO line (mirroring state-est §7.2):

```
policy[left] STABLE_SLEEP -> RESTLESS base_change=-5 -> -7 (prev_minus_2)
             rate_limited_to=-7 (Δ=2, hard cap OK)
```

Safety bypass / freeze / lockout each get a WARN line. Format matches the existing `OVERHEAT_HARD bypass:` line at `sleep_controller_v5.py:649-651`.

### 10.2 PG row contribution

The policy's contribution to a `controller_readings` row (additive — does not replace existing columns):

| Column | Source | Notes |
|---|---|---|
| `policy_base` (new) | `STATE_BASE[state]` | matches §2 |
| `policy_user_offset` (new) | `user_offsets.per_state[user][state]` | bounded `[-3,+3]` |
| `policy_global_offset` (new) | `user_offsets.global_[user]` | bounded `[-2,+2]` |
| `policy_pre_clamp` (new) | step 3 of §3 | int, may exceed `[-10,0]` |
| `policy_softened` (new) | step 5 of §3 | bool |
| `policy_rate_limited` (new) | step 6 of §3 | bool |
| `setting` (existing) | the final `Action.setting` | what was actually written |

Migration: additive `ALTER TABLE controller_readings ADD COLUMN ...` in `sql/v7_control_policy.sql` (matches state-est §7 pattern).

### 10.3 Per-night summary

Add to `v6_nightly_summary.notes` JSONB:

```json
{
  "policy_setting_histogram": {"-10": 0, "-9": 0, ..., "0": 23},
  "policy_state_setting_means": {"STABLE_SLEEP": -5.8, "WAKE_TRANSITION": -3.0, ...},
  "policy_rate_limit_clipped_ticks": 12,
  "policy_softened_ticks": 4,
  "policy_safety_bypassed_ticks": 0
}
```

`policy_safety_bypassed_ticks > 0` is the per-night canary that the rail or hard-overheat fired — a number to surface in the morning report, not just bury in logs.

---

## Summary

The control policy is a **pure function** `policy(state, user_offsets, prev_setting, prev_state, …) → Action(setting∈[-10,0], reason, confidence)`. It is a **lookup table**, not a function:

| State | base_setting | floor (warmest clamp) | max Δ/tick |
|---|---|---|---|
| `OFF_BED` | 0 | force 0 | n/a |
| `AWAKE_IN_BED` | −2 | −5 | ±2 |
| `SETTLING` | −4 | −8 | ±1 |
| `STABLE_SLEEP` | −5 + user_offset | −10 | ±1 (4-tick hold) |
| `RESTLESS` | prev − 2 | −10 | ±2 cooler only |
| `WAKE_TRANSITION` | −3 | **−3** (load-bearing — kills "too hot near wake-up") | ±1 |
| `DISTURBANCE` | hold prev | n/a | 0 |

User offsets apply *after* the base lookup; clamp to `[-10, 0]` runs *before* rate-limiting; low-confidence ticks soften toward warmer by +1 (asymmetric cost — overcooling > undercooling). Safety layer (right rail @ 86°F, left hard-rail @ 90°F, two-key arming, override freeze) sits **above** the policy and may veto without policy involvement; dead-man heartbeat and `snug_user_lockout` are spec'd for follow-up patches with the same interface.

**Three most important deletions:** (1) `CYCLE_SETTINGS` and the entire `_get_cycle_num` axis (`sleep_controller_v5.py:64-90, 1180-1182`) — cycle-of-night was the wrong control axis encoded in 6 places; (2) `_setting_for_stage` clobber table (`:1066-1070, 1169-1178`) — three competing baseline systems collapse to one; (3) cold-room comp as a separate branch — subsumed into `STABLE_SLEEP` policy with the body-trend-flat gate from `regime.py:203-204` becoming mandatory rather than optional.

Representative-night trajectory (§7.3) shows the migration kills the cycle-1 cold spike (+10/+8/+6 warmer at bed onset) and the late-night "wake-up too hot" (+3/+3 warmer at 6–7 AM) without losing the mid-night cooling authority (−6 vs −5 at the user's converged offset).
