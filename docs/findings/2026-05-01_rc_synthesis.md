# PerfectlySnug Responsive Cooling — Reverse-engineering synthesis (2026-05-01)

> Combined findings from a fleet of 8 parallel agents (2 data audit, 6 deep-dive
> analysis) plus a controlled empty-bed experiment. This is the canonical
> document for what we now believe RC actually does and how we got there.

## TL;DR — what RC actually does

The firmware is a **two-stage cascade**, not a single PID:

**Stage 1 — Setpoint generator (slow integrator / leaky max-hold):**
```
setpoint(t) = min( cap_user,
                   max( body_max(t),
                        setpoint(t-1) - 0.002°F per 10s ) )
```
The firmware tracks the warmest body sensor (`max(body_l, body_c, body_r)`)
with a slow downward leak. It is **capped** at one of a small set of
quantized half-°C values:

| °C | °F | What it is |
|---:|---:|---|
| 29.5 | 85.1 | "user target" cap (one of) |
| 30.0 | 86.0 | "user target" cap (most common, 12,173 samples) |
| 30.5 | 86.9 | "user target" cap |
| 31.0 | 87.8 | "user target" cap |

The cap depends on the user's active setting (L1/L2/L3) and 3-level mode.
Firmware works in **Celsius internally**.

**Stage 2 — Controller (P + rate feedforward + ambient feedforward +
Hammerstein output):**
```
target = 46.8
       + 19.1 * (body_max  - setpoint)
       +  1.4 * (body_avg  - setpoint)
       - 1.45 * (body_avg  - ambient)
       + 0.96 * max(0, dbody_max/dt)
       + 0.46 * max(0, dbody_avg/dt)

blower(t) = 0                                    if target < 16.4
            clip(max(13.2, target), 0, 100)      otherwise
```

Updates every ~31 seconds. Output is integer percent. Notable: there is
**no integral term** in Stage 2 (Ki≈0). The integration is in Stage 1's
setpoint ratchet. The output has a **deadband + min-on jump** ("Hammerstein"
nonlinearity), which is why blower values 1–9 are never observed.

## Why we believe this is approximately correct

Three independent agents using different methods (kitchen-sink ML, control
theory system identification, regime decomposition) converged on essentially
the same structure:

| Agent | Approach | Best CV result | Equation form |
|---|---|---:|---|
| rc-deep-ml | LightGBM, RF, ensemble | **R² = 0.972 (TS-CV)**, MAE 1.23% | `blower[t+1] = blower[t] + K·(body_max−setpoint) + bias` (autoregressive) |
| rc-deep-physics | System ID, scipy.optimize, control library | **R² = 0.74 (TS-CV)** Stage 2 only | Two-stage cascade above |
| rc-deep-timeseries | ARMAX, Granger, VAR, Kalman | R² = 0.586 walk-forward (PI form) | Kp ≈ 2.56 on (max body − setpoint), no integral |
| rc-deep-regime | HMM regimes + per-regime fit | R² = 0.766 (TS-CV) | 5 regimes; holding regime: blower(t) ≈ 0.99·blower(t-1)+0.7 |
| rc-deep-symbolic | PySR / gplearn | R² = 0.39 | `clip(-1.02·(setpoint-ambient) - 6.21·setting + 8.23)` |
| rc-deep-empirical | Lookups, k-NN, kernel | R² = 0.10–0.13 | No clean rule from passive obs |

All of them implicitly or explicitly use some version of `body_max - setpoint`
as the dominant driver, with the firmware exhibiting strong autoregressive
behavior (ramp-limited / autoregressive-style updates).

## Key data findings (from the two audit agents)

### Misinterpretations / data quality bugs

1. **Recorder excluded the firmware's PID sensors.** `/config/configuration.yaml`
   `recorder.exclude.entity_globs` was filtering out
   `sensor.smart_topper_*_pid_*` and `sensor.smart_topper_*_run_progress`.
   That's the firmware's actual control output — invisible to us until today.
   Recorder fix has been deployed; data is collecting now.

2. **3-level mode was on the entire window.** `switch.smart_topper_right_side_3_level_mode = on`
   means the firmware advances setting through L1 → L2 → L3 by `run_progress`.
   We were treating `bedtime_temperature` (L1) as "the user setting", but most
   of the night, L2 or L3 is active. L2 ranges -4 to -10 in observed data,
   completely invisible to our prior analyses.
   Helper added: `PerfectlySnug/tools/lib_active_setting.py`.

3. **`temperature_setpoint` is the firmware's tracking value, not a user
   target.** Within active modulation (blower 5–60%), setpoint = max(body)
   within ±0.2°F. Outside, it slews toward an ambient-anchored quantized
   default (the 30°C / 86°F class of values). It is not a static target.

4. **`running=off` blower=0 rows are synthetic.** The HA integration's
   `PerfectlySnugOutputSensor.native_value` overrides to 0 when the topper
   is off, but firmware actually freezes the last value. Drop these rows
   in any analysis.

5. **Topper `ambient_temperature` is biased high** (per the integration's
   own commits). Use `sensor.bedroom_temperature_sensor_temperature` as the
   real room reference.

6. **L1_TO_BLOWER_PCT delta** under RC=on with body in bed is mean -21,
   median -18, std 22 — not -45 as my mid-day estimate said. The comparison
   should use **L_active**, not L1.

7. **Native temperature unit from the integration is °C** (HA converts to °F).
   The half-°C grid in setpoint caps is consistent with this.

8. **30s resampling is not the bottleneck.** Native cadence is ~31s for
   topper sensors; sub-second dynamics aren't there. 5/10/30/60s resampling
   all give similar R². Time alignment is fine.

### What's missing / next data window

After the recorder fix, going forward we'll have:

- `sensor.smart_topper_right_side_pid_control_output` (firmware's actual PID
  output before clip + Hammerstein)
- `sensor.smart_topper_right_side_pid_proportional_term`
- `sensor.smart_topper_right_side_pid_integral_term`
- `sensor.smart_topper_right_side_run_progress` (drives L1→L2→L3 transitions)

In 2-3 nights, with this data, we'll be able to:
- Validate Stage 2 P + rate-FF coefficients against the firmware's
  exposed PID output (no inference needed)
- Identify the L_active → cap mapping in Stage 1 (currently the weakest
  point in the pipeline; only 6 cap values observed so far)

## Empty-bed experiment (2026-05-01) takeaways

Conducted on 2026-05-01 ~15:50–17:15 ET on the right side with RC on:

- **Test 1 step response:** setting=-10 → blower=100% steady; setting=-5 →
  blower=41% steady. Confirms `L1_TO_BLOWER_PCT` is the **RC-off baseline**:
  with no body, the firmware just outputs the setting's blower mapping.
- **Test 2 ice on individual body sensors:** identified body_left (modest
  drop) and body_center (dropped to 58°F) sensor positions. **Blower did
  not modulate** despite huge drops in individual body sensors. Confirms
  the firmware does not respond to body-cooling alone in an empty bed.
- **Test 3 BedJet heat (after Turbo):** drove body sensors from 73°F to
  77°F and ambient to 87°F. **Blower stayed at 41%** still. Firmware
  needs more than warmth to engage; likely needs sustained occupancy or
  body crossing some internal threshold.
- **`setpoint = max(body)` was perfect** during the entire empty-bed
  experiment, because we were always in the active modulation regime.

Bottom line from the empty-bed test: **the firmware does not engage RC
modulation without an occupied bed**. To do further controlled experiments,
we'd need a body in the bed.

## Recommended action items

### Done today

- [x] Recorder fix deployed; PID sensors recording
- [x] `PerfectlySnug/tools/lib_active_setting.py` helper added
- [x] L1_TO_BLOWER_PCT comment corrected in `appdaemon/sleep_controller_v5.py`
- [x] PROGRESS_REPORT.md updated with audit summary

### Next (after 2-3 nights of new data)

- [ ] Re-fit Stage 2 controller using firmware's exposed `pid_control_output`
      directly instead of inferring from blower history
- [ ] Identify the Stage 1 cap-rule (L_active + 3-level mode → cap_user)
      using run_progress
- [ ] Potentially deploy a forward-prediction in our PerfectlySnug
      controller that uses this firmware model to choose setting changes

### Optional follow-ups

- [ ] Body-in-bed controlled experiment with sustained occupancy and
      varying setpoints, now that we know what regime to test
- [ ] Re-run the v5 controller body-feedback fit using the corrected
      L_active / setpoint understanding
- [ ] Update HA dashboard naming so `temperature_setpoint` is labeled
      "RC tracking value" or similar, and `body_max` is exposed separately

## Source files (for reproducibility)

- `PerfectlySnug/docs/findings/2026-05-01_setpoint_is_max_body.md`
- `PerfectlySnug/docs/findings/2026-05-01_data_audit_labels.md`
- `PerfectlySnug/docs/findings/2026-05-01_data_audit_timing.md`
- `PerfectlySnug/docs/findings/2026-05-01_rc_deep_ml.md`
- `PerfectlySnug/docs/findings/2026-05-01_rc_deep_physics.md`
- `PerfectlySnug/docs/findings/2026-05-01_rc_deep_symbolic.md`
- `PerfectlySnug/docs/findings/2026-05-01_rc_deep_regime.md`
- `PerfectlySnug/docs/findings/2026-05-01_rc_deep_timeseries.md`
- `PerfectlySnug/docs/findings/2026-05-01_rc_deep_empirical.md`
- `PerfectlySnug/tools/lib_active_setting.py`
- `PerfectlySnug/tools/rc_symbolic.py`
- `PerfectlySnug/tools/rc_empirical.py`
- `/tmp/rc_physics.py`, `/tmp/rc_timeseries.py`, `/tmp/rc_deep_ml.py`,
  `/tmp/rc_regime.py`

## Source data

- `/tmp/snug_v2.json` — 13 days of high-resolution HA history for right-side
  topper sensors and bed presence (2026-04-17 11:39 UTC → 2026-04-30 20:35 UTC,
  RC switch confirmed on)
- `/tmp/snug_full.json` — same but without bed presence
- `/tmp/snug_hires.json` — earlier 7-day pull
- HA recorder, going forward — now includes PID and run_progress
- `/config/rc_experiment_log.csv` — 2026-05-01 empty-bed experiment log
- Postgres `sleepdata.controller_readings` — full historical sensor + setting
  data, 5-min resolution
