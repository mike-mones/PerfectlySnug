# 2026-05-01 — `temperature_setpoint` sensor tracks `max(body_*)` during active RC

## TL;DR (corrected after deeper analysis)

The HA entity `sensor.smart_topper_right_side_temperature_setpoint` (and the
left-side equivalent) is **not** a static user-selected target. During
**active RC operation (blower modulating in roughly 5–60%)**, the sensor
value tracks `max(body_left_f, body_center_f, body_right_f)` to within ~0.2°F.
At the extremes (blower stuck at 0%, or fully saturated >60%, or topper off)
the sensor reports default/idle values like 86.0°F, 87.8°F, 85.1°F that are
unrelated to live body readings.

The original "setpoint = max(body) always" claim was **only true during
the empty-bed 2026-05-01 experiment** because RC was sitting in a single
regime. Across 13 days of normal RC-on operation, the relationship is
regime-dependent.

## Evidence — by-regime breakdown across all 13 days of RC-on data

n = 35,981 rows at 30-second resolution, RC on, topper running.

| Blower bin | Mean (setpoint − max body) °F | Std °F | Count | Interpretation |
|---|---:|---:|---:|---|
| 0–5% | +2.25 | 4.72 | 22,287 | Blower idle; setpoint shows default values |
| **5–15%** | **-0.04** | **0.22** | 1,106 | Active RC; setpoint = max(body) |
| **15–30%** | **-0.13** | **0.24** | 4,170 | Active RC; setpoint = max(body) |
| **30–60%** | **-0.15** | **0.27** | 6,795 | Active RC; setpoint = max(body) |
| 60–100% | -4.70 | 7.02 | 1,623 | Saturated cooling; setpoint < max body |

Top 20 most common setpoint values include the suspicious quantized defaults:

| Setpoint °F | Count | Notes |
|---:|---:|---|
| 86.0 | 12,173 | Idle/default |
| 87.8 | 5,162 | Idle/default |
| 85.1 | 3,911 | Idle/default |
| 86.9 | 2,493 | Idle/default |
| 73.976, 80.510, 77.432, 78.458, … | 1,300+ each | Real readings tied to actual body temps |

## What the firmware setpoint probably *is*

The firmware's internal tracking variable. In active modulation, RC's
process value is `max(body)` itself (or near it), and the firmware exposes
that as `setpoint`. When idle or saturated, it shows a default/clamp value.

This is consistent with the empty-bed experiment where setpoint perfectly
tracked max(body): RC was in moderate-output mode the whole time.

## Why this still matters

1. The `controller_readings.setpoint_f` column is **not the user target**
   in any regime. Treating it as such was always wrong.
2. ML reverse-engineering should use it as the firmware's PV (max body),
   not as the user's target.
3. The actual user-configured target maps to the user `bedtime_temperature`
   number (-10..+10) via the L1 blower table — there is no separate
   user-target temperature exposed.

## Empty-bed RC behavior also observed

During the same 2026-05-01 experiment:

- Cold body-sensor injection (ice packs that drove individual sensors to
  58°F) produced **no change** in the blower output. It stayed at the L1
  baseline for the user setting (41% at setting=-5).
- BedJet ambient warming up to topper-ambient ≈ 87°F and body sensors ≈ 77°F
  also produced **no change** in the blower (still 41%).

This suggests RC may not engage meaningfully when the bed is unoccupied or
when body readings have not crossed some internal occupancy/threshold.
Needs more investigation, possibly with a real body in the bed or a
sustained heat source on the body sensors.

## Action items

- [ ] Re-fit RC reverse-engineering models. Keep `setpoint` as a feature
      but document it as "firmware PV ≈ max(body) during modulation".
- [ ] Add a derived column `body_max` to analysis tools and prefer it over
      `setpoint` in feature engineering — it's interpretable always, while
      `setpoint` is only interpretable during active RC.
- [ ] Update the `controller_readings` schema comment for `setpoint_f`.
- [ ] When updating the HA dashboard, consider exposing `body_max` as a
      separate sensor and labeling `temperature_setpoint` more honestly
      (e.g., "RC tracking value").

## Sister finding — L1_TO_BLOWER_PCT is the RC-off baseline only

While re-fitting on the cleaned data we discovered that under RC=on with an
occupied bed, the firmware modulates blower far below the L1 table:

- Mean actual blower across 18,500+ occupied rows: **14.6%**
- Mean L1 baseline expected from the user setting: **59.6%**
- Mean delta (actual − expected): **−45 percentage points**
- Median delta: **−50 percentage points**
- Std of delta: 30 percentage points

The empty-bed experiment confirmed L1 is correct for the *RC-off baseline*
(blower exactly matched the table for setting=-10 → 100%, setting=-5 → 41%
when no body was present). But in normal operation with a body in bed, RC
takes over and drives blower well below those values.

The `L1_TO_BLOWER_PCT` table comment in `appdaemon/sleep_controller_v5.py`
has been updated to reflect this. Any planning code that needs "what blower
will the firmware actually output for setting X with RC on" should be
rebuilt empirically rather than reading the table directly.

## Best reverse-engineering result so far

After all corrections (RC-on data only, occupied only, proper labeling):

| Model | TS-CV R² | MAE |
|---|---:|---:|
| Best linear (body_max + ambient) | 0.013 | 18.3% |
| GB (all features) on raw blower | +0.21 | 14.2% |
| GB on (blower − L1[setting]) RC delta | −0.49 | 20.5% |

We can't fully predict the firmware's output from the sensors we observe.
The strongest features are `body_r`, `body_l`, integrals of ambient_err
over 30+ minutes, and `setpoint`. The likely missing inputs are firmware
internal state we don't see externally (occupancy detection, time-since-
bed-entry, mode transitions).


## Source data

- HA recorder pull from 2026-04-17 11:39 UTC (RC on event) onward, 30-second
  resolution, 13 days
- Empty-bed experiment 2026-05-01 ~15:50–17:15 ET
- Experiment log `/config/rc_experiment_log.csv`

