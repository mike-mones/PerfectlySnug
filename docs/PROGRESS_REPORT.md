# PerfectlySnug ML Sleep Controller — Progress Report

> **Read this together with `ML_CONTROLLER_PRD.md` at the start of every conversation
> about the PerfectlySnug controller.** The PRD is the design *spec*; this
> document is the running log of what's actually been built, what's deployed,
> what's been tried and rejected, and what the data actually says today.

**Last updated:** 2026-04-29 (23 days into v5_rc_off operation, 8 weeks of HA recorder data)

---

## 1. Where we are in one sentence

v5 (heuristic) is running on the **left zone only**, has known accuracy issues but no live safety regressions, and we have not been able to demonstrate that any version of the proposed v6 ML policy improves comfort outcomes on the available data — so nothing new has been deployed. The single largest gap is that **the wife's right zone has no automated controller at all**, which is also where the only sustained overheat events have been recorded.

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

### v5's algorithm (in one paragraph)
Cycle baselines (hand-picked: cycle 1=-10, 2=-9, 3=-8, 4=-7, 5=-6, 6=-5) plus room-temperature compensation (cool-comp below 68°F, hot-comp above) plus a learned per-cycle blower-percentage adjustment (clipped to ±15%) plus two safety rails (`hot_safety` steps one colder when body > 85°F sustained; nothing on the cold side). The "learning" updates the blower adjustment from the most recent override delta, which causes oscillation.

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

| Component | State | Notes |
|---|---|---|
| `sleep_controller_v5.py` (`v5_rc_off`) | ✅ Running on HA | Left-zone comfort controller |
| `right_overheat_safety.py` (`RightOverheatSafety`) | ✅ Deployed 2026-04-29 | Standalone safety-only rail for the right zone — engages -10 setting after 2 consecutive readings ≥88°F, releases <84°F, snapshots and restores prior setpoint, releases on bed-empty or rail-disabled. No comfort logic, no learning, no per-cycle baselines. Threshold tuned from right-zone p90 (87.9°F). |
| Right-zone comfort control | ❌ Not present | Only the safety rail above; otherwise wife controls the bed manually |
| Hard overheat rail on left v5 (body ≥90°F → -10) | ✅ Deployed 2026-04-29 | Gated by `input_boolean.snug_overheat_rail_enabled`; **default off** (left has no demonstrated overheat events) |
| Shadow logger writing `/config/snug_shadow.jsonl` | ✅ Deployed 2026-04-29 | Lazy-imports `ml.policy`; wrapped in broad try/except so it cannot break the live loop |
| `input_boolean.snug_overheat_rail_enabled` (left) | ✅ Loaded | initial: off |
| `input_boolean.snug_right_overheat_rail_enabled` (right) | ✅ Loaded | initial: **on** — wife has 4 sustained ≥90°F events on record |
| `ml/policy.py`, `ml/features.py`, `ml/state/fitted_baselines.json` | ✅ Deployed 2026-04-29 | Required by shadow logger; not on the live action path |
| `tools/backfill_ha_recorder.py` | ✅ Run once on 2026-04-29 | Should be cron'd weekly on Mac Mini to capture short-term rows before recorder purge |

To deploy further v5 changes (no behavior change unless you flip the input_boolean):

```bash
scp PerfectlySnug/appdaemon/sleep_controller_v5.py PerfectlySnug/appdaemon/right_overheat_safety.py root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/
scp PerfectlySnug/ml/policy.py PerfectlySnug/ml/features.py root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/ml/
scp -r PerfectlySnug/ml/state root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/ml/
scp ha-config/configuration.yaml root@192.168.0.106:/config/configuration.yaml
ssh root@192.168.0.106 'ha core check --no-progress && /config/scripts/reload_automations.sh'
# Restart HA (`ha core restart`) when adding new input_boolean / new AppDaemon module.
```

---

## 8. Pipeline / what's next (priority ordered)

1. **Right-zone comfort controller** — the safety rail (§7) only kicks in at body ≥90°F. Below that, the right zone is still fully manual. Easiest path: extract v5's `_compute_setting`/`_control_loop` into a per-zone function, instantiate twice, reuse the hard overheat rail. **Do NOT** copy the heuristic baselines blindly without per-zone validation against `ha_topper_hourly` — her body trajectory is structurally warmer.
2. **Schedule `tools/backfill_ha_recorder.py` weekly on Mac Mini cron** — captures short-term raw rows before the 30-day recorder purge, growing the high-resolution dataset.
3. **Find an unbiased preference signal** — candidates: presence-sensor movement spikes (proxy for restlessness), Apple Watch arousal density, sleep-stage fragmentation. Any of these would let us label "uncomfortable" minutes without requiring a manual override. Until we have one, the override-only training data is the binding constraint, not the model architecture.
4. **Fit per-zone "typical body trajectory" curves** from `ha_topper_hourly` (57 nights, both zones) — purely descriptive, not for control. Useful as the calibration target for any new hard rail and as a sanity check for the wife's right-zone controller.
5. **Defer LightGBM residual model** until override corpus crosses ~150 events (currently 53). This is straight from PRD §4.2.

---

## 9. Conventions for future sessions

- **Read both `ML_CONTROLLER_PRD.md` (design intent) and this file (current state) before proposing changes.**
- All data analysis goes through PG (`192.168.0.3`, db `sleepdata`, user `sleepsync`, password `sleepsync_local`). Never use HA history API or JSON files for analysis.
- Use `ssh macmini` for PG access, `ssh root@192.168.0.106` for HA.
- Use the dehumidifier room-temp sensor or bedroom Aqara, never the topper's onboard ambient (5-10°F too high — see §4.5).
- Use `body_*_f` from `controller_readings` (PG) or `sensor.smart_topper_*_body_sensor_center` (HA), not the side body sensors.
- Update this document at the end of any session that adds new evidence, deploys code, or changes the system architecture.
