# PerfectlySnug ML Sleep Controller — Progress Report

> **Read this together with `ML_CONTROLLER_PRD.md` at the start of every conversation
> about the PerfectlySnug controller.** The PRD is the design *spec*; this
> document is the running log of what's actually been built, what's deployed,
> what's been tried and rejected, and what the data actually says today.

**Last updated:** 2026-05-01 (RC fleet reverse-engineering — see findings/2026-05-01_rc_synthesis.md)

> 🎯 **2026-05-01 RC reverse-engineering — canonical synthesis:**
> [`findings/2026-05-01_rc_synthesis.md`](findings/2026-05-01_rc_synthesis.md).
> Combined output of 8 parallel agents (2 audit, 6 deep-dive analysis) plus
> a controlled empty-bed experiment. RC is now identified as a **two-stage
> cascade** (slow leaky-max-hold setpoint generator + Hammerstein P-controller
> with rate and ambient feedforward, no integral term in stage 2). Three
> independent agents converged on the same structure with TS-CV R² up to 0.97.

> 🔬 **2026-05-01 data audit (post setpoint finding):**
> 1. **HA recorder was excluding the firmware PID stream**
>    (`sensor.smart_topper_*_pid_*`) and `run_progress`, capping any
>    blower-prediction model at R²≈0.21. Excludes have been removed from
>    `/config/configuration.yaml` (backup at `.bak.20260501`); HA
>    restarted to begin collection.
> 2. **`bedtime_temperature` (L1) is not the active dial for most
>    rows.** `switch.smart_topper_<side>_3_level_mode` is ON, so the
>    firmware advances L1 → L2 → L3 by `run_progress`. All future
>    analysis must use **L_active**, not L1. Helper:
>    `tools/lib_active_setting.py`. Entity table and threshold formula in
>    `findings/2026-05-01_data_audit_labels.md` §9.
> 3. **L1_TO_BLOWER delta correction.** The earlier "−45 pts under RC"
>    figure was inflated by `running=off` synthetic-zero rows and
>    unoccupied rows. Correct value on occupied RC-on data (n=43,827):
>    mean −21, median −18, std 22. Comment in
>    `appdaemon/sleep_controller_v5.py:201` updated.
> 4. **Use external room sensor for ambient features in modeling**, not
>    `sensor.smart_topper_*_ambient_temperature` (which the audit
>    confirmed is biased high by 5–10 °F). `sleep_controller_v5.py`
>    already does this via `_get_room_temp_entity()` →
>    `sensor.bedroom_temperature_sensor_temperature`. Any offline
>    feature-extraction pipeline that has been pulling the
>    topper-onboard ambient must switch to the same entity.
> 5. **Drop `running=off` rows from training.** The integration
>    overrides output sensors to 0 when running=off; those zeros are
>    synthetic, not real labels.

> 🔬 **Earlier 2026-05-01 finding:** `sensor.smart_topper_*_temperature_setpoint`
> tracks `max(body_*)` only inside the active blower band (5–60 %); in
> deadband and saturation it slews toward an ambient-anchored default.
> See [`findings/2026-05-01_setpoint_is_max_body.md`](findings/2026-05-01_setpoint_is_max_body.md)
> and [`findings/2026-05-01_data_audit_labels.md`](findings/2026-05-01_data_audit_labels.md) §3.

---

## 1. Where we are in one sentence

v5.1 (heuristic, refit) is running on the **left zone only** as of 2026-04-30; the v5→v5.1 update adjusted the per-cycle baselines from `[-10,-9,-8,-7,-6,-5]` to `[-10,-8,-7,-5,-5,-6]` (motivated by user feedback the morning of 2026-04-30 — see "v5.1 update" subsection below). v5.1 still has known structural limitations (override-bias trap, no proxy comfort signal, c4/c5 thin samples), and we have not been able to demonstrate that any version of the proposed v6 ML policy improves comfort outcomes on the available data — so v6 is not deployed. The single largest gap is that **the wife's right zone has no automated controller at all**, which is also where the only sustained overheat events have been recorded.

---

## 2. Current production architecture

```
┌────────────────────────────┐    HA recorder       ┌──────────────────────────┐
│ Perfectly Snug topper      │◀─sensor entities────▶│ Home Assistant (HA Green) │
│ left + right zones         │                      │ 192.168.0.106             │
│ body/ambient/blower/setpt  │                      │ /addon_configs/.../apps/  │
└──────────────┬─────────────┘                      └──────┬─────────────┬─────┘
               │                                           │             │
               │ ESPHome bed-presence                      │ AppDaemon   │ recorder.statistics
               │ (pressure %)                              ▼             ▼
               │                                  sleep_controller_v5.py  ── 30-day SQLite purge
┌──────────────▼─────────────┐                    (left zone ACTIVE)
│ Apple Watch (SleepSync app)│                    (right zone PASSIVE log only)
│ stages, HR, HRV, resp rate │                            │
└──────────────┬─────────────┘                            │
               │ HTTPS POST                               │ writes 5-min snapshots
               ▼                                          ▼
       ┌─────────────────────────────────────────────────────────────┐
       │ Mac Mini 192.168.0.3 — PostgreSQL `sleepdata`                │
       │   controller_readings    (5-min, override-flagged, 20 nights)│
       │   ha_stats               (HA backfill, 57 hourly + 14 5-min) │
       │   ha_topper_hourly       (pivoted view, both zones)          │
       │   nightly_summary, sleep_segments, health_metrics            │
       └─────────────────────────────────────────────────────────────┘
```

### What controls what
| Component | Role | Where |
|---|---|---|
| `sleep_controller_v5.py` (rev `v5_rc_off`) | LEFT-zone control loop, every 5 min | HA AppDaemon |
| Right-zone behavior | **None** — Perfectly Snug firmware default; logged only via `_log_passive_zone_snapshot("right", ...)` | HA AppDaemon |
| `state_logger.py` | Mirror of HA topper sensors → PG `controller_readings` | HA AppDaemon |
| iOS Health Receiver (FastAPI :8080) | Apple Watch → PG `health_metrics`, `sleep_segments` | Mac Mini |
| `tools/backfill_ha_recorder.py` | Pulls HA SQLite stats → PG `ha_stats` (idempotent) | Run from laptop or Mac cron |

### v5.1 update — 2026-04-30 (live)

`CYCLE_SETTINGS` shipped from `[-10,-9,-8,-7,-6,-5]` to **`[-10,-8,-7,-5,-5,-6]`**
after a refit on 49 overrides / 30 nights, motivated by user report
("woke up cold mid-night, slightly warm in the morning"). Both events
were logged as overrides at 04:27 ET (cycle 5, asked +3 warmer) and 06:56 ET
(cycle 6, asked −2 cooler). Methodology: shrinkage prior_n=5 posterior for
c2..c5, with c5 capped one step cooler than the data fit (smooths the
c5→c6 transition) and c6 manually dipped one step cooler than v5
(addresses the pre-wake overheat that wouldn't otherwise show in the
override corpus). In-sample MAE dropped 6.3% (2.939 → 2.755) and signed
under-warming bias dropped 26% (−1.92 → −1.41). See
`_archive/v5_1_baseline_fit_2026-04-30.md` for full analysis. The pipeline
that ran the sweep is `tools/v5_1_baseline_sweep.py` against
`controller_readings`; rerun after every ~5 new overrides.

### v5.2 update — 2026-04-30 (live, same evening as v5.1)

User pushback: with two months of data, why aren't we beating the
firmware's "responsive cooling"? The honest answer was that the
override-fitted cycle baselines were optimizing within the wrong frame.
Investigation surfaced two things:

1. The topper firmware exposes its own PID
   (`sensor.smart_topper_*_pid_control_output/integral_term/proportional_term`,
   `temperature_setpoint`, `blower_output`). Our "setting" is just a knob
   on the firmware setpoint. The firmware closes the loop on surface
   temperature; nothing was closing the loop on body temperature.
2. Linear regression of `body_delta ~ setting + body + room_temp` had
   R²=0.03 over 14 days. Settings barely move body temp at our 5-min
   cadence. Cycle baselines optimized against override events were
   essentially fitting noise.

The v5.2 mechanism: after computing the v5.1 cycle baseline, apply a
**closed-loop body-temperature correction**:

```python
if cycle >= 3 and body_left < BODY_FB_TARGET_F (80°F):
    correction = min(1.25 * (BODY_FB_TARGET_F - body_left), 5)
    base_setting = clip(base_setting + correction, -10, 0)
```

Cycles 1–2 still honor bedtime aggressive cooling (skip body feedback).
Above 86°F, the safety rails handle hot-side. Asymmetric (no Kp_hot) by
design.

Held-out LOOCV on the override corpus: v5 MAE 3.116 → v5.2 MAE 1.633
(−48%). In-sample: hit-rate 26.8% → 61.0%, max error 8 → 7, signed bias
−2.00 → +0.32. Per-cycle: c1/c2 unchanged, c3 marginal, c4/c5/c6 cut by
1.5–2 MAE each.

`CONTROLLER_VERSION` bumped `v5_rc_off` → `v5_2_rc_off`. New live tag for
PG analysis.

### Room compensation reference — 2026-05-01

Changed `ROOM_BLOWER_REFERENCE_F` in `appdaemon/sleep_controller_v5.py` from
68°F to **72°F**. Rationale: last night's room range was roughly 67.3–72.2°F,
so the old 68°F anchor treated most of the night as neutral-to-warm and even
added cooling near 72°F. The 72°F anchor treats the same conditions as mildly
cold-to-neutral, reducing blower demand in the 67–71°F band while preserving
hot compensation above 72°F. Retrospective replay for 2026-04-30→2026-05-01:
left-zone room compensation shifted by −16 blower points at comparable room
temps (e.g. ~67°F: old −3 vs new −19; ~72°F: old +16 vs new 0). This made the
unfloored target 0–2 L1 steps warmer on most logged left-zone ticks, but live
actuation would still have been constrained by the user's manual override floor
and the normal freeze/rate-limit holds. Right-zone v5.2 does not use
`ROOM_BLOWER_REFERENCE_F`; it uses cycle baselines plus body feedback, so this
change has no direct effect on right-zone proposals.

### Right-zone v5.2 — 2026-04-30 (live, same evening)

The wife's right zone is no longer firmware-default. Two-key arming via
Python const + HA helper (`input_boolean.snug_right_controller_enabled`,
default off; flipped on at 16:34 ET).

Architecture (matches user's left-zone pattern):
- Responsive Cooling: **off** (deterministic setting→blower% via our table)
- 3-tier schedule: **off** (controller has full authority over `bedtime_temperature`)
- bedtime_temperature default: −8 (matches v5.2 c1 baseline)
- Right-zone-specific parameters (her n=6 overrides + audit-validated):
  - cycle baselines `[-8, -7, -6, -5, -5, -5]` (gentler than user's −10/−10)
  - body feedback target = 80°F, asymmetric Kp_hot=0.5, Kp_cold=0.3, cap=4
  - skip cycle 1
- BedJet 30-min suppression both in safety rail and shadow controller
- Safety rail body sensor swapped from `body_center_f` to `body_left_f`
  (skin-contact channel; eliminates warm-sheet contamination). Rail
  thresholds calibrated to her physiology: engage at 86°F (was 88°F),
  release at 82°F (was 84°F). User-stated rule: he runs hot at 84°F on
  body_left; her body_left runs +2°F warmer at every percentile.
- Override-freeze (60 min after manual change), rate limit (30 min
  between writes), self-write suppression (controller's own writes
  don't trigger override freeze).

Live operational kill-switch: toggle `input_boolean.snug_right_controller_enabled`
in HA UI; takes effect on the next 5-min tick, no redeploy. See
`_archive/right_zone_rollout_2026-04-30.md`.

### Right-zone room compensation — 2026-05-01

Added wife/right-zone room compensation to `sleep_controller_v5.py` using the
same physical room reference as the left side (`72°F`) but wife-specific gains:
`RIGHT_ROOM_BLOWER_HOT_COMP_PER_F=4.0` and
`RIGHT_ROOM_BLOWER_COLD_COMP_PER_F=0.0`. This addresses the user's point that
the 72°F reference is about the room, not a person, while keeping the personal
response separate. Initial deployment is deliberately **hot-only**: room
temperatures above 72°F add cooling in blower-proxy space; temperatures below
72°F add **zero** warming. Rationale: the only right-side manual override from
2026-04-30→2026-05-01 was colder (`−4→−5`) around 03:25 ET with room ≈68.3°F
and body_left ≈73°F, so a below-72°F warming multiplier would contradict fresh
evidence. Replay of that night: 67.3°F low → `right_room_comp=0`; 68.3°F
override → `right_room_comp=0`; 72.2°F high → `right_room_comp=+1` blower point,
which did not change the snapped right-zone proposal. Net changed ticks: 0/120.

### Explicit pre-cool / initial-bed cooling gate — 2026-05-01

Correction to the earlier same-day "early-sleep forced-cooling removal": the
user clarified they **do** want intentional max/aggressive cooling before bed
and for roughly the first 30 minutes after getting into bed. Deployed controller
change: `INITIAL_BED_COOLING_MIN=30.0`,
`INITIAL_BED_LEFT_SETTING=-10`, and `INITIAL_BED_RIGHT_SETTING=-10`.

Behavior now:
- pre-sleep Apple stages (`inbed` / `awake`) force pre-cool/max cooling and
  bypass body feedback, learned residuals, and room compensation;
- the first 30 minutes after bed-presence occupancy onset force max cooling on
  both zones (`-10`), using ESPHome bed occupancy when available rather than
  sleep-stage labels alone;
- after that occupancy-based gate expires, body feedback is active from cycle 1
  (`BODY_FB_MIN_CYCLE=1`, `RIGHT_BODY_FB_SKIP_CYCLES=()`), so low `body_left`
  can warm the model normally;
- right-zone BedJet suppression still prevents inflated body sensors from
  driving body-feedback corrections, but it no longer blocks the user-requested
  initial-bed max-cooling gate.

Replay of 2026-04-30→2026-05-01 early rows shows the corrected policy holds
left at `-10` through 26 minutes then allows body-feedback warming at 31+
minutes; right would change the first 30 minutes from the old gentler `-8`
behavior to `-10`, then return to the model at 35+ minutes. Tests cover left
and right first-30-minute forced cooling, post-window warming, and pre-sleep
forced pre-cooling.

### Discomfort proxy — bed-pressure movement signal added

User pointed out the bed-presence sensor publishes pressure% at sub-second
cadence (371 state changes between 22:00 and 08:00 in last night's data).
Previous discomfort proxy was using only the 5-min PG-snapshot pressure,
missing 90% of movement signal.

Added `sig_movement_density` to `ml/discomfort_label.py` candidate
signals. Built via `ml/data_io.load_movement_per_minute()` which fetches
HA history API state changes (sub-second), aggregates to per-minute
features (`n_movements`, `max_delta`, `pressure_std`).

Validation against override events (lead window 5–15 min):
- Old `sig_pressure_burst` (5-min PG snapshot): 12% recall, 5/41 caught
- **New `sig_movement_density` (sub-sec HA recorder): 28% recall, 11/41 caught**
- 2.2× improvement. Discomfort corpus effective sample size: 443 → 502.

This unlocks signal capture for the right zone (where the wife has only
6 overrides — too thin for fitting, but plenty of restless-minutes that
will show up in pressure data). Same pipeline, swap entity to
`sensor.bed_presence_2bcab8_right_pressure`.

### v5's heuristic algorithm (in one paragraph)
Cycle baselines (was hand-picked `[-10,-9,-8,-7,-6,-5]`; now refit
`[-10,-10,-7,-5,-5,-6]` per the v5.1 update above; v5.2 then adds a
body-feedback correction on top — see v5.2 section) plus room-temperature compensation (cool-comp below 72°F, hot-comp above) plus a learned per-cycle blower-percentage adjustment (clipped to ±15%) plus two safety rails (`hot_safety` steps one colder when body > 85°F sustained; nothing on the cold side). The "learning" updates the blower adjustment from the most recent override delta, which causes oscillation.

---

## 3. Data points — what we have, where, and how much

### 3.1 PG `controller_readings` (the gold dataset)
Every 5-minute control-loop snapshot written by AppDaemon since v5_rc_off launch.

| Field | Notes |
|---|---|
| Range | 2026-04-06 → 2026-04-29 |
| Nights | **20 left, 15 right** (right has fewer because right-zone passive logging started later) |
| Rows | 4,626 (2,492 L + 2,134 R) |
| Has override flags? | ✅ `action='override'` + `override_delta` |
| Has body/ambient/setpoint/room? | ✅ |
| Has bed-presence pressure? | ✅ (added mid-stream) |
| Has Apple Watch? | ❌ joined separately at analysis time via `health_metrics`/`sleep_segments` |

**Override events (the real training signal):**
- Left zone: **47 overrides** across 20 nights (~2.4/night)
- Right zone: **6 overrides** across 15 nights (~0.4/night)
- Override events are a **biased sample** — they only occur when v5 was wrong enough to bother the user. They cannot be naively regressed against to learn "preferred setting at cycle X". See §6.

### 3.2 PG `ha_stats` (HA recorder backfill)
Backfilled 2026-04-29. Created from HA `statistics` (hourly aggregates) and `statistics_short_term` (5-min) by `tools/backfill_ha_recorder.py`.

| Source | Granularity | Nights | Range |
|---|---|---|---|
| `long_term` | hourly mean/min/max | **57** | 2026-03-04 → 2026-04-29 |
| `short_term` | 5-min mean/min/max | 14 | 2026-04-17 → 2026-04-29 |

40 entities backfilled: full topper telemetry (body sensors L/R, blower L/R, heater head/foot L/R, ambient L/R, setpoint L/R, PID terms), bed-presence pressure L/R, bedroom temperature.

**View `ha_topper_hourly`** pivots both zones into one wide row per hour with `body_left_f`, `body_right_f`, `blower_left_pct`, `blower_right_pct`, `ambient_left_f/right_f`, `setpoint_left_f/right_f`, `room_temp_f`, `bed_left_pressure_pct`, `bed_right_pressure_pct`.

**What this gets us:** ~3× more body/blower/ambient history for both zones. Enough to *validate* baselines (does the chosen setting drive body temp into a comfort range?) and to fit per-zone *typical body trajectories*, but **does not include override labels** — so it cannot be naively used for preference fitting. HA's recorder doesn't tag manual setpoint changes vs controller-driven ones.

### 3.3 PG `health_metrics`, `sleep_segments`
Apple Watch sleep stages (core/deep/rem/awake), HR, HRV, respiratory rate. Pushed by SleepSync iOS app via Health Receiver (FastAPI :8080 on Mac Mini).
Coverage roughly matches the topper data range.

### 3.4 What we do NOT have
- **Pre-March-4 data.** HA recorder purged it; not recoverable.
- **Right-zone overrides at scale.** 6 events. Statistically meaningless on its own.
- **Setpoint override labels in HA.** No way to tell from HA recorder alone whether a setpoint change was manual.
- **`override_history.jsonl`** on AppDaemon — abandoned; only 5 entries from April 7-8.

---

## 4. What's proven (verified by data)

1. **v5 reaches max-cool too aggressively in early cycles for this user** — cycle-1 v5 baseline -10 plus active cool-comp matches user preference (47/47 cycle-1 overrides land at -9 or -10 which v5 already produces).
2. **v5 is too cold in cycles 4-6** — sample mean of override-revealed preference at cycle 4 is -3 vs v5's -7 across 5 events; cycle 5 is -2 vs v5's -6 across 5 events; cycle 6 is -3.7 vs v5's -5 across 6 events. **But** these samples are biased — they are only the times v5 was wrong enough to override. See §6.
3. **The wife's body-sensor distribution is much warmer than the user's:** median 83.2°F vs ~78°F, p95 94.6°F vs 84°F, max 98.9°F vs 88.6°F.
4. **Sustained overheat happens — but only on the right zone:** four stretches >30 min with body_right ≥ 90°F (max 98.9°F across 80 min on 2026-04-24). Zero such stretches on the left zone. This correlates with the right zone having no controller, not with v5 making bad choices.
5. **Body sensor reads ~5-10°F above true skin temp** (compared against the bedroom Aqara reference). The topper's onboard `ambient_temperature` reads similarly hot. Use the dehumidifier room-temp sensor or bedroom Aqara, never the topper ambient.
6. **HA recorder long-term (hourly) for topper sensors goes back 8 weeks** — useful as background validation set; raw 5-min only available for last 30 days due to recorder purge.

---

## 5. What's NOT proven (and what we tried)

| Claim | Evidence against it |
|---|---|
| "ML-fitted cycle baselines beat v5" | Counterfactual replay: NEW MAE 2.13 vs v5 1.81 on left (47 overrides). NEW better on 19, worse on 20, same on 8. Hit-rate +4.3pp but MAE worse. |
| "Per-zone fit will help once we have wife data" | Right-zone overrides n=6. Even with the +5 nights gained from the recent backfill (15 right-zone nights total), it's not enough to fit baselines per cycle (n≤2 per cycle). |
| "Body-too-cold rail (cap cooling at -3 below 76°F) helps" | Ablation showed it changes left-zone MAE by 0.07 step. The 538 non-override divergences it creates fight v5's intentional max-cool during early-cycle override-driven cooldowns. Net effect is noise. |
| "Smart_baseline (v6 layer 2) doesn't break anything outside override moments" | Mean drift between NEW and v5 on non-override minutes is **2.0-3.1 steps per cycle** in both zones. The new policy diverges materially from v5 thousands of times even when v5 was demonstrably fine. |
| "We have enough data to deploy a residual ML model" | PRD §4.2 calls for ~150+ override events for the LightGBM residual layer. We have 53 (47L + 6R). |

### Failure-mode summary (the things we keep re-discovering)
- **Override-only fitting is biased.** Overrides capture ~1% of minutes; fitting baselines to them overcorrects.
- **Hard rails written from a single user's data don't generalize across zones.** Initial per-zone calibrated thresholds would have suppressed the wife's overheat rail entirely (her p95 body sensor is 94.6°F).
- **Without baseline restraint, learned controllers oscillate.** v5 itself was the cautionary tale; the v6 smart_baseline replicates the failure mode.

---

## 6. The data-bias problem (read this before proposing any "fit baselines from overrides" approach)

The 47 override events are not a random sample of preferences. They are exactly the moments when v5's chosen setting was wrong enough that the user manually corrected it. The other ~99% of minutes are silent. Silence ≠ preference confirmation, but it's evidence that v5's choice was acceptable. Naive maximum-likelihood on the overrides treats the silent acceptance as zero-weight, so the fitted baselines pull strongly toward the override sample mean and overshoot. This is why even a Bayesian shrinkage prior (with prior_n=1 toward v5's hand-picked baselines) still produces a controller that does worse than v5 on held-out replay.

Any future training pipeline must either:
1. Treat non-override minutes as positive examples for the current setting (with appropriate weight), OR
2. Predict only the **residual** correction at moments where the model has high support / confidence (PRD §4.5), and defer to v5 elsewhere, OR
3. Wait for a much larger override corpus (~150+).

---

## 7. What's deployed today

**As of 2026-04-30 16:30 ET:**

| Component | State | Notes |
|---|---|---|
| `sleep_controller_v5.py` (`v5_2_rc_off`) — **left zone live** | ✅ Running on HA | v5.2 with closed-loop body feedback on `body_left_f` (target 80°F, Kp_cold 1.25, Kp_hot=0, max_delta 5, min_cycle 3) |
| `sleep_controller_v5.py` — **right zone live** | ✅ Running on HA | RIGHT_LIVE_ENABLED=True + input_boolean.snug_right_controller_enabled=on (two-key arming). Right-zone-specific params: baselines `[-8,-7,-6,-5,-5,-5]`, target 80°F, Kp_hot=0.5, Kp_cold=0.3, cap=4, skip cycle 1 |
| `right_overheat_safety.py` (`RightOverheatSafety`) | ✅ Live | Skin-side sensor (body_left_f), engage 86°F, release 82°F, BedJet 30-min suppression, force −10. Per-zone calibrated to her physiology |
| Hard overheat rail on left v5 (body ≥90°F → −10) | ✅ Deployed | Gated by `input_boolean.snug_overheat_rail_enabled`; **default off** |
| Shadow logger `/config/snug_shadow.jsonl` (ml.policy) | ✅ Live | Defensive try/except |
| Right-zone shadow log `/config/snug_right_v52_shadow.jsonl` | ✅ Live | Logs all proposals + actuated/blocked status; consumed by `tools/eval_right_shadow.py` |
| Discomfort proxy with `sig_movement_density` | ✅ Pipeline-only | Extracts sub-second pressure events from HA recorder; recall on overrides 28% (was 12% with PG-snapshot pressure); not yet wired to live control |
| `input_boolean.snug_overheat_rail_enabled` (left) | ✅ off (default) | |
| `input_boolean.snug_right_overheat_rail_enabled` (right) | ✅ **on** | |
| `input_boolean.snug_right_controller_enabled` (right v5.2 live arm) | ✅ **on** | Operational kill switch — toggle off in HA UI to instantly disable right-zone control, no redeploy |
| Right-side firmware: Responsive Cooling | ✅ **off** | Deterministic setting → blower% via L1_TO_BLOWER_PCT |
| Right-side firmware: 3-tier schedule | ✅ **off** | Controller has full authority over `bedtime_temperature` |
| Right-side `bedtime_temperature` default | −8 | Matches v5.2 c1 baseline; controller takes over from c2 onward |
| `ml/policy.py`, `ml/features.py`, `ml/state/fitted_baselines.json` | ✅ Deployed | NOTE: `fitted_baselines.json` is stale relative to v5.2 — read by shadow logger only, not live actuation path. Audit backlog item §10. |
| PG nightly backup (macmini cron 04:00) | ✅ Live | `/home/mike/backups/sleepdata/backup.sh`; pg_dump -F c, gzipped, 14-dump retention |

### Operational kill switches (instant, no redeploy)

| To disable | Toggle this in HA UI |
|---|---|
| Right-zone v5.2 controller | `input_boolean.snug_right_controller_enabled` → off |
| Right-zone safety rail | `input_boolean.snug_right_overheat_rail_enabled` → off |
| Left-zone hard-overheat rail | `input_boolean.snug_overheat_rail_enabled` → off (already off) |
| All AppDaemon (nuclear option) | Stop the AppDaemon addon in HA UI |

### Tomorrow morning's review pipeline

```bash
cd PerfectlySnug && .venv/bin/python tools/eval_right_shadow.py
```

Shows tick-by-tick what the right-zone controller proposed, whether it actuated, and the gate that blocked it (BedJet window / occupancy / freeze / rate-limit / no-change).

### Deploy command for future controller changes

```bash
scp PerfectlySnug/appdaemon/sleep_controller_v5.py PerfectlySnug/appdaemon/right_overheat_safety.py root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/
scp PerfectlySnug/ml/policy.py PerfectlySnug/ml/features.py PerfectlySnug/ml/contamination.py PerfectlySnug/ml/learner.py PerfectlySnug/ml/__init__.py root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/ml/
scp PerfectlySnug/appdaemon/apps.yaml root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/
ssh root@192.168.0.106 'ha addon restart a0d7b954_appdaemon'
sleep 30
ssh root@192.168.0.106 'ha addon logs a0d7b954_appdaemon 2>&1 | tail -20'
# For HA config changes (new input_boolean): scp ha-config/configuration.yaml + ha core restart
```

---

## 8. Pipeline / what's next (priority ordered)

1. **Tomorrow morning review** — run `tools/eval_right_shadow.py` plus subjective sleep reports. If she sleeps better, leave on and refine target/Kp from new data. If worse, toggle `input_boolean.snug_right_controller_enabled` off and tune.
2. **Right-zone movement-density signal** — same `tools/build_discomfort_corpus.py` pipeline using `sensor.bed_presence_2bcab8_right_pressure`. Her n=6 override corpus is too thin for fitting; movement signal is the path to actually capturing her late-night discomfort.
3. **Refit her baselines after ~10 nights of v5.2 right-zone data** — current params (target 80, Kp_hot 0.5, Kp_cold 0.3, baselines `[-8,-7,-6,-5,-5,-5]`) are educated starting points. Use `tools/v5_1_baseline_sweep.py` (extend to right-zone if needed) on her newly-accumulated override corpus.
4. **Audit backlog (§10)** — 11+ open items, prioritize by severity.
5. **Schedule `tools/backfill_ha_recorder.py` weekly on Mac Mini cron** — captures short-term raw rows before HA recorder purge.
6. **Defer LightGBM residual model** until override corpus crosses ~150 events (currently ~50 left-zone, ~6 right-zone).

---

## 9. Conventions for future sessions

- **Read both `ML_CONTROLLER_PRD.md` (design intent) and this file (current state) before proposing changes.**
- All data analysis goes through PG (`192.168.0.3`, db `sleepdata`, user `sleepsync`, password `sleepsync_local`). Never use HA history API or JSON files for analysis.
- Use `ssh macmini` for PG access, `ssh root@192.168.0.106` for HA.
- Use the dehumidifier room-temp sensor or bedroom Aqara, never the topper's onboard ambient (5-10°F too high — see §4.5).
- Use `body_*_f` from `controller_readings` (PG) or `sensor.smart_topper_*_body_sensor_center` (HA), not the side body sensors.
- Update this document at the end of any session that adds new evidence, deploys code, or changes the system architecture.

---

## 10. Audit backlog (8-agent deep audit, 2026-04-30)

Findings from `_archive/audit_2026-04-30/` (in-conversation, not separately committed). Severity per finding; cite file:line where actionable.

### CRITICAL — fix before next deploy

| ID | Issue | Location | Status |
|---|---|---|---|
| C1 | NaN crash in `_read_zone_snapshot`: `int(setting)` on transient `nan` sensor read | `appdaemon/sleep_controller_v5.py:1166` | ✅ FIXED 2026-04-30 (NaN guard in `_read_float`) |
| C2 | `apps.yaml` missing `right_overheat_safety` block (would silently drop the rail on next deploy) | `appdaemon/apps.yaml` | ✅ FIXED 2026-04-30 (backported from HA host) |
| C3 | Migration 006 fails — `PERCENTILE_DISC` cannot be window function | `tools/sql/006_discomfort_labels.sql` | ⚠ OPEN. View `v_discomfort_minutes_left` does not exist in PG. Rewrite as CTE. |
| C4 | `ml/contamination.py:SQL_VIEW_DDL` references non-existent column `body_f` | `ml/contamination.py:135` | ⚠ OPEN. Either delete the constant (deployed migration 007 is source of truth) or rewrite. |
| C5 | `ml/state/fitted_baselines.json` is stale relative to v5.2 | `ml/state/fitted_baselines.json` | ⚠ OPEN. Used only by shadow logger, but produces misleading "what-if" output. Regenerate or stop reading. |
| C6 | Test pollution: `tests/test_controller_v3.py` connected to live PG and inserted synthetic rows tagged controller_version='v3' | `tests/test_controller_v3.py` | ✅ FIXED 2026-04-30 (file deleted; 24 polluted rows purged from PG) |

### HIGH — should address this week

| ID | Issue | Location | Status |
|---|---|---|---|
| H1 | Rail engagement state not persisted on rail-only mutations (overheat_hard_streak, hot_streak) | `appdaemon/sleep_controller_v5.py:665-670, 678-687` | ⚠ OPEN. AppDaemon restart mid-engagement loses streak. Add `_save_state()` after rail mutations. |
| H2 | `_set_l1` race with `_on_setting_change` callback can mis-classify controller's own write as user override | `sleep_controller_v5.py:976-979, 787-823` | ⚠ OPEN. Update `last_setting` BEFORE `call_service`, or add 2-sec suppression flag. |
| H3 | `_setting_for_stage` clobbers v5.2 cycle baselines when SleepSync feeds fresh sleep stages | `sleep_controller_v5.py:611-615` | ⚠ OPEN. Stage table `{deep:-10, core:-8, rem:-6, awake:-5}` is unfitted v5-era data; v5.2 c4=-5 jumps to -10 the moment a 'deep' event arrives. Disable, make a delta, or refit. |
| H4 | Cold overrides have no all-night floor (warm overrides do) | `sleep_controller_v5.py:816-821, 419-423` | ⚠ OPEN. Asymmetric override-floor design. Document or symmetrize. |
| H5 | `hot_safety` anchored to `current_setting` erodes warm overrides one step every 5 min | `sleep_controller_v5.py:677-685` | ⚠ OPEN. Anchor to `max(base_setting, override_floor)` instead. |
| H6 | Right-zone shadow used contaminated `body_avg` instead of skin-side `body_left` | `sleep_controller_v5.py:_right_v52_shadow_tick` | ✅ FIXED 2026-04-30 (switched to body_skin = body_left + BedJet 30-min gate) |
| H7 | PG had no backups; full data loss risk on PG crash | macmini | ✅ FIXED 2026-04-30 (`/home/mike/backups/sleepdata/backup.sh`, cron 04:00 daily, 14-dump retention) |
| H8 | HRV cadence implausibly high (~172/day same as HR; should be ~24/day) — likely Health Receiver miscoding RR-interval as HRV | `health_metrics` table | ⚠ OPEN. Sample 10 rows; if values are 60-100 with units bpm, the discomfort proxy's HRV signal is garbage. |

### MEDIUM — backlog

| ID | Issue | Location |
|---|---|---|
| M1 | `tools/refit_with_proxy_labels.py` `PROXY_DIR=-1` is wrong direction for warm-running user | `tools/refit_with_proxy_labels.py:49` |
| M2 | `ml/features.py:289` body fusion uses mean (amplifies center-sensor sheet contamination) | `ml/features.py:289` |
| M3 | `tools/v5_1_baseline_sweep.py` LOOCV separates by override timestamps not full nights → leaky | `tools/v5_1_baseline_sweep.py` |
| M4 | BedJet window restarts on every bed re-entry, not first-onset-of-night | `appdaemon/right_overheat_safety.py:178-189` |
| M5 | `_blower_pct_to_l1` ties bias colder by accident (intentional?) | `sleep_controller_v5.py:985-990` |
| M6 | `learner.py` is dead code that disagrees with everything else | `ml/learner.py` |
| M7 | Right-zone shadow log + main shadow log not rotated | `sleep_controller_v5.py:567-569, 598` |
| M8 | `v_setting_timeline` action vocab outdated (preference, hot_safety, deadband not mapped) | `tools/sql/002_analysis_views.sql:91-93` |
| M9 | Custom component `client.py:_io_lock` held across all retries — UI commands block ~50s on flaky topper | `custom_components/perfectly_snug/client.py:138-155` |
| M10 | Custom component `client.py` set-setting has no ack matching → optimistic update sticks on dropped frames | `custom_components/perfectly_snug/client.py:201-252` |
| M11 | Right-zone telemetry: live controller writes go to JSONL only, not `controller_readings` rows. Need a controlled-zone branch in `_log_to_postgres`. | `sleep_controller_v5.py` |
