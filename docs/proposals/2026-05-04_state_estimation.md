# PerfectlySnug — State Estimation Design (2026-05-04)

**Status:** design proposal · **Scope:** the per-zone `(features → discrete state + stability_confidence)` block that v6+ control consumes in place of the cycle-index axis. **Companion:** `docs/proposals/2026-05-04_evaluation.md` (offline scoring harness).

This proposal directly retires audit findings #1 (time-of-night is the wrong axis, encoded in six places — `CYCLE_SETTINGS`, `RIGHT_CYCLE_SETTINGS`, `_get_cycle_num`, `RegimeConfig.cycle_baseline_*`, `_normal_cool_base`, `_cycle_index`) and #5 (60s movement aggregates already in PG `controller_pressure_movement` are read by `right_comfort_proxy.py` only, never by live control). Body-sensor-validity (audit #2) is treated as a first-class input, not a patch token.

The output of state estimation is **the only sleep-stage-shaped input** the control layer is allowed to read. Cycle index, `elapsed_min`-keyed baselines, and `_setting_for_stage` tables are out.

---

## 1. Output state schema

Two outputs per zone, recomputed every controller tick:

| Output | Type | Range |
|---|---|---|
| `state` | enum (one of 7) | see table below |
| `stability_confidence` | float | `[0.0, 1.0]` |

### 1.1 The seven states

| State | Behavioral meaning for **cooling** control |
|---|---|
| `OFF_BED` | No body load on this zone. The dial setting is irrelevant; firmware should sit at `0` (neutral). Body sensors are reading mattress equilibrium (~room+3°F) and **must not** drive `body_fb`. Setpoint changes here are free of comfort consequence — this is the safe window to test, calibrate, or repower. |
| `AWAKE_IN_BED` | User on the bed but not asleep (recent presence transition, high movement, or stage label `inbed`/`awake`). Body skin still ramping toward steady-state; sheets unequilibrated. **Cooling commands risk a freezing-air complaint.** Conservative dial only (≥ −5 left, ≥ −3 right). This is the regime that historically eats overrides at bed-onset. |
| `SETTLING` | Movement is decreasing from `AWAKE_IN_BED` levels; body is warm but trend is flat-or-rising as sheets equilibrate. Aggressive cooling now is appropriate but must not *outrun* the body — this is exactly where the v5.2 cold-room comp + body_fb double-fire bug lived. |
| `STABLE_SLEEP` | Sustained low movement with body in band. The controller may pursue its target setpoint at full authority. Most of the night by clock-time lives here; this is where small body-trend signals carry the most information. |
| `RESTLESS` | Movement variance spike *during* an otherwise stable period without a presence change. Most likely thermal discomfort (too warm — the user only owns "cool harder" overrides). The controller should bias **cooler by 1–2 dial steps** for one tick and re-evaluate, *not* warm. (See §6 for the wife/right-side asymmetry where this signal is much stronger per `right_comfort_proxy.py:117`, weight 0.30.) |
| `WAKE_TRANSITION` | Rising movement variance plus rising body trend after extended `STABLE_SLEEP`. This is the user surfacing toward wake; cooling commands are still appropriate but the freeze on warm overrides should shorten (the user is about to evaluate the dial themselves anyway). |
| `DISTURBANCE` | Sharp single-event movement (e.g. partner movement on the *other* zone bleeding through, BedJet airflow blast, dog jumping on the bed). Transient — must **not** trigger a regime change in either direction. The state machine flags this and falls back to the *previous* state's behavior for one tick. |

`stability_confidence` is the controller's "how strongly do I believe this state label" gauge. It feeds into how aggressively the residual / future learner is allowed to act. Low confidence ⇒ controller hugs the deterministic base; high confidence ⇒ controller may apply small biases. It is **not** allowed to widen control authority (cap at deterministic policy bounds always).

---

## 2. Inputs (per-zone)

All features are computed once per tick from in-process rolling buffers (see §4). Each feature has a source, a staleness gate, and a NaN-handling rule.

### 2.1 Movement features (PG-derived rolling window held in RAM)

The controller subscribes to `sensor.bed_presence_2bcab8_<zone>_pressure` directly (same pattern as `appdaemon/v6_pressure_logger.py:74-79`) and maintains its own 15-minute deque of `(monotonic_ts, value)` per zone. **Live control does not read PG on the critical path** — `controller_pressure_movement` remains the durable record for offline analysis only.

| Feature | Window | Definition | Staleness gate | NaN handling |
|---|---|---|---|---|
| `movement_rms_5min` | last 300s | `sqrt(mean(Δp²))` over consecutive samples | last sample > 90s old ⇒ feature unavailable | unavailable ⇒ treat as missing in §3 |
| `movement_rms_15min` | last 900s | same, broader window | last sample > 90s old ⇒ unavailable | as above |
| `movement_variance_15min` | last 900s | sample variance of `|Δp|` per second-bucket | sample_count < 30 ⇒ unavailable | as above |
| `movement_max_delta_60s` | last 60s | `max(|Δp|)` between consecutive samples | last sample > 90s old ⇒ unavailable | as above |

Why RMS not the existing `abs_delta_sum_60s`: RMS is robust to varying sample density (the underlying sensor is event-driven, not periodic, so `sample_count` ranges 1–~20 per minute per the live PG data). The 60s aggregate logger keeps writing to PG unchanged for offline percentile recompute (§4).

### 2.2 Presence features

| Feature | Source (HA entity) | Staleness gate | NaN handling |
|---|---|---|---|
| `presence_binary` | `binary_sensor.bed_presence_2bcab8_bed_occupied_<zone>` | state age > 5 min ⇒ unknown | unknown ⇒ fail-closed `OFF_BED` (per `bodyFbFailClosed` patch, `sleep_controller_v5.py:bodyFbFailClosed`) |
| `seconds_since_presence_change` | derived; mirror of `_on_bed_onset` mechanism in `sleep_controller_v5.py:_on_bed_onset` | n/a | if presence unknown ⇒ unavailable |

### 2.3 Body features

| Feature | Source | Staleness gate | NaN handling |
|---|---|---|---|
| `body_avg_f` | mean of `sensor.smart_topper_<zone>_body_{left,center,right}_temperature` (3-sensor mean as in `ml/features.py:289`) | any one sensor > 10 min stale ⇒ use mean of remaining two; all three stale ⇒ unavailable | unavailable ⇒ suppress all body-trend transitions (§6) |
| `body_skin_f` | `body_left_f` only (driver of `body_fb` cold path, mirrors `regime.py:body_skin_f`) | > 10 min stale ⇒ unavailable | as above |
| `body_trend_15min` | OLS slope of `body_avg_f` over last 15 min (units: °F per 15 min) | < 5 valid samples in window ⇒ unavailable | unavailable ⇒ treated as 0 with `confidence -= 0.2` |
| `body_sensor_validity` | `(body_avg_f - room_temp_f) >= BODY_VALID_DELTA_F` AND `seconds_since_presence_change >= BODY_VALID_WARMUP_S` | n/a | falsy ⇒ see §6 degraded behavior |

`BODY_VALID_DELTA_F = 6.0` (matches existing `RIGHT_BODY_SENSOR_VALID_DELTA_F`, `sleep_controller_v5.py:176-177`; generalize to left zone per audit "KEEP" §4 row 8).
`BODY_VALID_WARMUP_S = 600` (10 minutes — empirically the sheet/mattress equilibration window, audit §2.1).

### 2.4 Room features

| Feature | Source | Staleness gate | NaN handling |
|---|---|---|---|
| `room_temp_f` | `sensor.bedroom_temperature_sensor_temperature` (Aqara, **never** topper onboard ambient — audit §3.1, `_get_room_temp_entity`) | > 10 min stale ⇒ unavailable | unavailable ⇒ assume `room_temp_f = 70.0` and `confidence -= 0.1` (don't fail-close, just downgrade) |
| `room_trend_30min` | OLS slope, °F per 30 min | < 10 valid samples ⇒ unavailable | unavailable ⇒ treated as 0 |

### 2.5 Control-history features

| Feature | Source | Staleness gate | NaN handling |
|---|---|---|---|
| `setting_recent_change_30min` | count of `_on_setting_change` events in last 30 min (mirror the existing in-process counter that drives override freeze) | n/a | n/a |

### 2.6 Per-user rolling percentiles (the only PG-read path, off-critical)

Feature `movement_rms_5min` and `movement_rms_15min` are compared against per-user `P25`, `P75`, `P90` thresholds derived from the prior **7 nights of `STABLE_SLEEP`-labeled rows** in `controller_pressure_movement`. (Bootstrap problem: until 7 nights are labeled, use the empirical defaults derived from the 943 currently-collected left-side rows — see §3.4.)

Recompute schedule: nightly at `04:00 ET` from a single `psycopg2` query, results cached in-process. A failed recompute keeps the previous values. **Never queried during a control tick.** Implementation lives in a new helper class `StatePercentileCache` (file: `ml/v6/state_percentiles.py`).

---

## 3. Inference engine — explainable rule-based decision tree

### 3.1 Choice & justification

**Pick: rule-based decision tree with explicit thresholds.**

Justification:
- The data volume is small (n=53 overrides across 30 nights; 943 rows in `controller_pressure_movement`). A logistic regression's weights would be undertrained; an HMM's transition matrix would be hand-tuned anyway.
- The audit explicitly catalogs the failure mode of "implicit ML eats the override-bias trap" (PROGRESS §6, audit §2.6, §3.3). A decision tree is auditable line-by-line in the controller log — every transition emits its trigger feature and threshold.
- The 7 states are not orthogonal — they have a natural priority ordering (presence > recency > body trend > movement). Decision trees encode priority natively.
- Tree leaves cleanly map onto the existing `regime.py` priority-ordered cascade (`UNOCCUPIED > PRE_BED > INITIAL_COOL > BEDJET_WARM > SAFETY_YIELD > OVERRIDE > COLD_ROOM_COMP > WAKE_COOL > NORMAL_COOL`) so the integration surface is small.

A tiny logistic regression (≤30 weights) is the **fallback path** if the tree's calibration plateaus on offline scoring (§8); switching is a one-file change.

### 3.2 The tree (priority order — first match wins)

State estimation runs **after** the existing regime safety priorities (`UNOCCUPIED`, `SAFETY_YIELD`, `OVERRIDE`) but **before** `COLD_ROOM_COMP / WAKE_COOL / NORMAL_COOL`. Its output is consumed by those base-setting selectors, replacing `_normal_cool_base` and `_cycle_index`.

```
def estimate_state(zone, features, prev_state, percentiles) -> (state, confidence):

  # Rule 0 — degraded modes (see §6) checked first
  if movement features all unavailable:
      return _degraded_body_only(features, prev_state)

  # Rule 1 — OFF_BED
  if features.presence_binary == False:
      if features.seconds_since_presence_change >= OFF_BED_DEBOUNCE_S:  # 120
          return ("OFF_BED", 1.0)
      return ("OFF_BED", 0.7)  # recent transition, lower conf

  # Rule 2 — AWAKE_IN_BED (recent entry OR sustained high movement)
  if (features.seconds_since_presence_change < AWAKE_RECENT_S    # 600
      or features.movement_rms_5min > percentiles.movement_p75):
      return ("AWAKE_IN_BED", 0.9 if features.body_sensor_validity else 0.6)

  # Rule 3 — DISTURBANCE (single-tick spike, no presence change)
  if (features.movement_max_delta_60s > DISTURBANCE_DELTA      # 8.0
      and features.movement_rms_15min < percentiles.movement_p75
      and prev_state in ("STABLE_SLEEP", "SETTLING")):
      return ("DISTURBANCE", 0.5)
      # NOTE: caller must restore prev_state's *control behavior* on next tick

  # Rule 4 — RESTLESS (variance spike inside an otherwise stable period)
  if (prev_state == "STABLE_SLEEP"
      and features.movement_variance_15min > percentiles.movement_var_p90
      and features.movement_rms_15min > percentiles.movement_p75):
      return ("RESTLESS", 0.7)

  # Rule 5 — WAKE_TRANSITION (rising movement + rising body, late session)
  if (prev_state == "STABLE_SLEEP"
      and features.movement_variance_15min > percentiles.movement_var_p75
      and features.body_trend_15min is not None
      and features.body_trend_15min > 0.30                     # °F/15min rising
      and features.seconds_since_presence_change > 5 * 3600):  # weak prior, §5
      return ("WAKE_TRANSITION", 0.6)

  # Rule 6 — STABLE_SLEEP
  if (features.movement_rms_15min < percentiles.movement_p25
      and features.body_sensor_validity
      and abs(features.body_trend_15min or 0.0) < 0.30):       # °F/15min flat
      return ("STABLE_SLEEP", 0.9)

  # Rule 7 — SETTLING (transitional, decreasing movement, body warming)
  if (features.movement_rms_5min < features.movement_rms_15min  # decreasing
      and features.movement_rms_15min < percentiles.movement_p75
      and (features.body_trend_15min or 0.0) >= -0.10):
      return ("SETTLING", 0.7 if features.body_sensor_validity else 0.5)

  # Default — treat as SETTLING with low confidence
  return ("SETTLING", 0.4)
```

### 3.3 Starting thresholds (constants block in `ml/v6/state_estimator.py`)

```python
OFF_BED_DEBOUNCE_S        = 120
AWAKE_RECENT_S            = 600
DISTURBANCE_DELTA         = 8.0   # raw pressure-pct units; see §3.4 calibration
BODY_TREND_FLAT_F_PER_15M = 0.30
BODY_TREND_RISE_F_PER_15M = 0.30
LATE_SESSION_S            = 5 * 3600
```

Body trend constants intentionally match `RegimeConfig.cold_room_body_trend_max_f_per_15m=0.20` order-of-magnitude (kept slightly looser to accommodate noise at the SETTLING boundary). Adjust together via the eval harness (§8), never independently.

### 3.4 Per-user percentiles (bootstrap defaults)

Empirical from the 943 rows in `controller_pressure_movement` (left, occupied=true) at this writing:

| Quantity | Value | Default until 7-night recompute kicks in |
|---|---|---|
| `movement_p25` (`movement_rms_15min`) | 0 | **0.05** (don't seed at exact zero — that makes "STABLE" trivially true) |
| `movement_p75` (`movement_rms_15min`) | 0 | **0.20** |
| `movement_p90` (`movement_rms_15min`) | 0.15 | **0.50** |
| `movement_var_p75` | (recompute) | **0.10** |
| `movement_var_p90` | (recompute) | **0.30** |

The current zero-inflation at p75 reflects how rare large pressure swings are in the steady-sleep distribution — not a bug. The defaults above are calibrated above zero so the rule cascade actually fires and produces non-`STABLE_SLEEP` labels; they will tighten as the per-night percentile recompute (§4) sees real data.

Existing `right_comfort_proxy.py:32` already uses `zone_baseline_movement_p75: float = 0.05` — keep this exact value as the left-side `movement_p75` default for consistency.

---

## 4. Real-time guarantees

| Requirement | How it's met |
|---|---|
| Runs every controller tick | The state estimator is called once per `_compute_setting` invocation (≤60s tick today, configurable). State + confidence written to in-process attribute and to PG (§7). |
| O(1) compute given prebuilt rolling buffers | The 15-min pressure deque is bounded by the listener (max ~900 samples ≈ once-per-second worst case). All RMS/variance computed in O(N) over the deque, but with N ≤ ~900 and the tick budget of 60s this is O(1) practically. No `pd.DataFrame` in the hot path (re-use the explicit `for v in dq` pattern from `v6_pressure_logger._aggregate`). |
| No PG read on critical path | Percentiles are loaded into a `StatePercentileCache` object at startup and refreshed by an AppDaemon `run_daily("04:00:00", _refresh)` callback. The control tick reads `cache.snapshot()` (a frozen dict). PG reads are **only** in the daily callback, with the same 3s timeout pattern as `v6_pressure_logger._get_pg`. |
| Failure isolation | If the daily refresh fails, the previous percentiles are kept; a structured warning is logged and `state_percentiles_stale_min` PG column (§7) increments. Failure of the listener subscription degrades to the body-only path (§6); the controller never crashes. |

---

## 5. Time-of-night handling

**Time-of-night is NOT a primary input to the rule cascade.** It does not appear in any state-deciding rule's *primary* threshold.

It is allowed in exactly **two** places, both bounded:

### 5.1 As a weak prior on the `STABLE_SLEEP → WAKE_TRANSITION` transition

Rule 5 uses `seconds_since_presence_change > 5 * 3600` as a *necessary* condition for `WAKE_TRANSITION`. This is a one-direction prior: it can only *delay* a `WAKE_TRANSITION` label until the user has been in bed at least 5 hours. It cannot induce a `WAKE_TRANSITION` on its own (movement variance + body trend are both required); it cannot change *any* other state. The 5-hour value is a conservative lower bound on a full night's deep+REM cycle complement (PRD §2.1: cycles are 80–110 min; 5h ≈ 3 full cycles).

This prior is **disabled** entirely if movement features are available and clear. It only matters in close-call cases where the variance is at the boundary.

### 5.2 As a tiebreaker only when movement signal is degraded

In the §6 movement-degraded fallback, `seconds_since_presence_change` is used to bias the collapsed state set toward `OCCUPIED_QUIET` after 90 minutes (long enough for the user to plausibly have fallen asleep). Cap on its influence: `confidence` is hard-capped at `0.5` for any state inferred from this prior — the controller will treat it as "weak label, hug the deterministic base."

What is **explicitly forbidden**:

- ❌ No `cycle_num = floor(elapsed_min/90)` index anywhere in this file.
- ❌ No `cycle_baseline[c]` lookup table.
- ❌ No "deep is more likely in cycle 1" hardcoded transition probabilities.
- ❌ No `night_progress` or `sin_cycle/cos_cycle` features (`ml/features.py:284-286` — discarded per audit §5).

---

## 6. Fallback / graceful degradation

### 6.1 Movement sensor stale > 5 min

Collapsed state set: `{OFF_BED, OCCUPIED_AWAKE, OCCUPIED_QUIET}`. Confidence capped at `0.5`.

```
def _degraded_movement(features, prev_state):
    if features.presence_binary != True:
        return ("OFF_BED", 0.5)
    if (features.seconds_since_presence_change < 1800             # < 30 min
            or (features.body_trend_15min or 0.0) > 0.50):        # body still ramping
        return ("OCCUPIED_AWAKE", 0.5)
    return ("OCCUPIED_QUIET", 0.5)
```

Downstream control treats `OCCUPIED_AWAKE` as `AWAKE_IN_BED` (conservative dial), `OCCUPIED_QUIET` as `STABLE_SLEEP` *but with confidence 0.5* (so any residual / learner contribution is suppressed). PG row records `state_degraded='movement'` (§7).

### 6.2 Body sensor validity false (audit #2)

When `body_sensor_validity == False` (either `(body_avg_f - room_temp_f) < 6.0°F` OR `seconds_since_presence_change < 600s`):

- All transitions whose **rule** depends on `body_trend_15min` are suppressed:
  - Rule 5 (`WAKE_TRANSITION`) — cannot fire
  - Rule 6 (`STABLE_SLEEP`) — cannot fire (body_trend gate is required)
- The estimator falls through to Rule 7 (`SETTLING`) or Rule 2 (`AWAKE_IN_BED`).
- Confidence is reduced by 0.2 from the rule's nominal value.
- This addresses the open hole flagged in audit §2.1: `bodyFbFailClosed` patches occupancy → controller, but it does **not** address the 10–15 min sheet-equilibration window after a `True` occupancy edge. This rule does.
- PG row records `state_degraded='body_validity'`.

### 6.3 Both degraded

Very conservative: state = `OCCUPIED_AWAKE`, confidence `0.3`. Controller treats as "do nothing fancy, hold the existing setting until one signal comes back."

### 6.4 Documented downstream behavior

The control layer (`policy.py` / future `state_policy.py`) consumes the state per this contract:

| State | Allowed dial range (left) | Body_fb authority | Cold-room comp authority | Residual authority |
|---|---|---|---|---|
| `OFF_BED` | force 0 | OFF | OFF | OFF |
| `AWAKE_IN_BED` | [-5, 0] | OFF | OFF | OFF |
| `SETTLING` | [-8, 0] | half (Kp × 0.5) | half | OFF |
| `STABLE_SLEEP` | [-10, 0] | full | full | full × confidence |
| `RESTLESS` | [-10, max(prev−2, -10)] | OFF (avoid double-fire) | full | OFF |
| `WAKE_TRANSITION` | [-8, 0] | full | full | OFF |
| `DISTURBANCE` | hold prev_state's dial | use prev_state's settings | use prev_state's settings | OFF |

The right-zone version uses tighter ranges and the same authority gating. (The downstream policy spec is its own deliverable; this table fixes the contract surface.)

---

## 7. Logging

### 7.1 Per-tick (writes to existing `controller_readings` table)

Three new columns to add via additive migration `sql/v7_state_estimation.sql` (matches the additive pattern of `sql/v6_schema.sql`):

```sql
ALTER TABLE controller_readings
    ADD COLUMN IF NOT EXISTS state               VARCHAR(20),
    ADD COLUMN IF NOT EXISTS state_confidence    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS state_degraded      VARCHAR(20),  -- NULL | 'movement' | 'body_validity' | 'both'
    ADD COLUMN IF NOT EXISTS movement_rms_5min   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS movement_rms_15min  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS movement_var_15min  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS body_trend_15min    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS body_sensor_valid   BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_controller_readings_state
    ON controller_readings (state) WHERE state IS NOT NULL;
```

The existing `regime` column remains and is set in parallel — `state` is the new authoritative axis; `regime` is kept for one transition release so eval can compare.

### 7.2 Per-tick (AppDaemon log)

Single structured line per tick per zone (unconditional, INFO):

```
state[left] = STABLE_SLEEP conf=0.92 mrms5=0.03 mrms15=0.02 var15=0.01
              btrend=-0.05 bvalid=1 (prev=STABLE_SLEEP held)
```

State-transition events are additionally logged at INFO with the trigger:

```
state[left] STABLE_SLEEP -> RESTLESS conf=0.70 trigger=movement_var_15min(0.42 > p90 0.30)
```

This matches the `regime` log format in `sleep_controller_v5.py` and is greppable by the existing `journalctl`-style triage workflow.

### 7.3 Per-night (writes to `v6_nightly_summary`)

Add to the existing `notes` JSONB column on `v6_nightly_summary`:

```json
{
  "state_histogram": {
    "OFF_BED": 23, "AWAKE_IN_BED": 18, "SETTLING": 12,
    "STABLE_SLEEP": 312, "RESTLESS": 6, "WAKE_TRANSITION": 4,
    "DISTURBANCE": 2
  },
  "state_degraded_minutes": {"movement": 0, "body_validity": 14, "both": 0},
  "state_transitions": 22,
  "stability_confidence_mean": 0.81,
  "state_percentiles_stale_min": 0
}
```

Counts are in tick-units (one per ~60s). Computed by the existing nightly summary callback.

---

## 8. Validation hooks

State estimation is offline-replayable end-to-end against `controller_readings` + `controller_pressure_movement`. The companion `docs/proposals/2026-05-04_evaluation.md` defines the harness; this section names the hooks the harness needs.

### 8.1 The `replay_state(night_id, zone) -> DataFrame` API

A pure function in `ml/v6/state_estimator.py` that takes one night of historical pressure samples + body/room/presence rows and emits the same per-tick `(state, confidence, features)` tuples the live controller would have written. Inputs come straight from the two PG tables; no HA dependency.

### 8.2 Scoring against existing labels

Three score buckets the harness must compute, all derivable from existing PG data:

1. **Override correlation.** For each user override event in `controller_readings WHERE action='override'` (the n=53 set), the state at the *5 minutes preceding* the override should not be `STABLE_SLEEP` with high confidence. A `STABLE_SLEEP/conf>0.8 → override` transition is a precision miss — the state estimator believed the user was content while they were warming up to override. Target: ≥ 70 % of overrides have state ∈ {`AWAKE_IN_BED`, `SETTLING`, `RESTLESS`, `WAKE_TRANSITION`} in the 5-min lead window.

2. **Empty-bed false-positive rate.** For all rows where presence == False for ≥ 10 min, state must equal `OFF_BED`. Target: ≥ 99 %.

3. **Stability mass.** Across all nights, the share of *occupied, mid-night* (90 min < `seconds_since_presence_change` < `5h`) tick-minutes labeled `STABLE_SLEEP` should be **30–80 %**. Below 30 % means the rule is too strict (everything looks transitional); above 80 % means it's a no-op (everything is "stable"). Both failures invalidate the design.

### 8.3 Reachability check

The harness must verify all 7 states are reached at least once across the historical corpus (or surface unreached states for re-tuning). A state never reached in 30+ nights is a design defect, not a calibration issue.

### 8.4 Comparison gate against current behavior

The harness computes the same three buckets for the existing `regime` column on the same nights. The state estimator must:

- Equal-or-beat the regime classifier on bucket #1 (override lead-time recall).
- Equal-or-beat on bucket #2 (≥ 99 % empty-bed correctness — `regime=UNOCCUPIED` already does this).
- Land in the 30–80 % band on bucket #3 (the existing `regime=NORMAL_COOL` is roughly 90 %+ of mid-night ticks today, which is *exactly* the failure mode the audit calls out — `NORMAL_COOL` is a no-op label).

Promotion to live control requires passing on the **last 14 nights** of data (matches the existing `recommendation.md §11.2` Canary-L gate).

---

## Summary

A 7-state per-zone estimator (`OFF_BED`, `AWAKE_IN_BED`, `SETTLING`, `STABLE_SLEEP`, `RESTLESS`, `WAKE_TRANSITION`, `DISTURBANCE`) plus a `[0,1]` `stability_confidence`, computed on every controller tick from in-process rolling buffers over the bed-pressure stream (already collected by `v6_pressure_logger`), the 3-sensor body mean, room temp, and presence — never from `elapsed_min` or cycle index. Inference is a 7-rule priority decision tree with explicit thresholds, calibrated against per-user 7-night percentiles refreshed nightly via PG (off the critical path). Movement-stale and body-validity-fail paths degrade to a 3-state collapsed set with confidence ≤ 0.5. Time-of-night appears only as a weak ≥ 5h prior on `WAKE_TRANSITION` and as a tiebreaker in the degraded path; no cycle index, no `CYCLE_SETTINGS`, no `night_progress`. Output drives the dial-authority table in §6.4, replacing `_normal_cool_base` and `_cycle_index`. Validation harness scores against the existing 53-override set and 30 nights of `controller_readings`/`controller_pressure_movement`.

**Three key thresholds** (tunable via `ml/v6/state_estimator.py` constants block):

1. `STABLE_SLEEP` requires `movement_rms_15min < movement_p25` (default `0.05`) AND `|body_trend_15min| < 0.30 °F/15m` AND `body_sensor_validity == True`.
2. `AWAKE_IN_BED` fires on `seconds_since_presence_change < 600 s` OR `movement_rms_5min > movement_p75` (default `0.20`).
3. `BODY_VALID_DELTA_F = 6.0` and `BODY_VALID_WARMUP_S = 600` gate every body-trend-using transition, addressing audit finding #2's 10–15 min sheet-equilibration window head-on.
