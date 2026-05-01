# 2026-05-01 — Data audit: entity meanings, units, and the missing PID stream

Agent: DATA AUDIT (1 of 8). Subject: why `sensor.smart_topper_right_side_blower_output`
ML stalls at R²≈0.21. Verdict: **the dominant feature for predicting blower
is excluded from the HA recorder, and the "user target" feature people have
been using is the wrong knob during most of the night**. Plus several entity
meanings are subtly wrong in our prior notes.

All RC-on data is ${5\,\text{April}\rightarrow30\,\text{April UTC}}$ (~13 days).
77,397 30-second rows after `responsive_cooling=on` AND `running=on` filter.
56% of those rows have `bed_presence_2bcab8_right_pressure > 20`.

---

## 1. The single most important finding

**HA recorder is configured to exclude the PID controller signals**.
`/config/configuration.yaml`:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.smart_topper_*_pid_*
      - sensor.smart_topper_*_heater_*_raw
      - sensor.smart_topper_*_run_progress
```

This silently drops, for both zones:

| Entity | Setting ID | Encoding | What it is |
|---|---|---|---|
| `sensor.smart_topper_right_side_pid_control_output` | 42 (`SETTING_CTRL_OUT`) | `(raw-32768)/100` | **The firmware's PID output that drives the blower** |
| `sensor.smart_topper_right_side_pid_integral_term` | 43 | same | I-term |
| `sensor.smart_topper_right_side_pid_proportional_term` | 44 | same | P-term |
| `sensor.smart_topper_right_side_run_progress` | 23 | int 0–100 | % through current run; selects L1/L2/L3 in 3-level mode |

Live snapshots (5 polls, 30 s apart, 2026-05-01 21:50–21:53Z, RC=on, running=on, blower=0):

```
PID_OUT  P_term  I_term   blower  setpoint  body_R  body_C  body_L  ambient
 5.00    3.03    1.97       0     84.236    76.44   74.66   75.76   86.86
 5.20    3.17    2.04       0     84.326    76.19   74.57   75.69   87.08
 5.41    3.30    2.11       0     84.416    75.94   74.48   75.76   86.86
 5.38    3.20    2.18       0     84.506    76.28   74.48   75.85   86.86
 5.56    3.31    2.25       0     84.614    76.10   74.41   75.69   86.72
```

Things this snapshot pins down:

- `pid_control_output ≡ p_term + i_term` (4.19=2.68+1.51 in the post-fetch
  state, 5.00=3.03+1.97 etc.). There is no D-term entity exposed.
- The blower is **0** while PID output is climbing 5.0→5.6. There is a
  **blower deadband / activation threshold** below which the blower stays
  off even when the PID controller wants cooling. The 13-day blower
  histogram has a huge spike at 0 (≈25%) that is consistent with this.
- Setpoint can drift several degrees away from `max(body)` even with
  `running=on, RC=on` (here: setpoint 84.6, body_max 76.4 → +8.2 °F). The
  prior "setpoint = max(body) when running" claim is too strong; it only
  holds in the active-modulation band (see §3 below).

**Implication for ML**: any model that does not include `pid_control_output`
(or P/I terms) will at best learn a smoothed proxy of it via lagged body
temperatures. The integral term is, by definition, path-dependent — no
combination of current sensor readings can reconstruct it. R² ≈ 0.21 is
exactly the ceiling you'd expect.

**Action — required before any further ML iteration**:

1. Remove the three lines from `recorder.exclude.entity_globs` (or move
   them to an explicit per-entity exclude that doesn't catch these), reload
   recorder, and start collecting. Three nights of new data > 13 nights of
   old data for this purpose.
2. Until then, use a foreground logger that polls these three sensors
   every 30 s into a side table (`pid_readings`) so the next sleep is
   not lost.
3. Consider also enabling `run_progress` — see §5 below for why.

---

## 2. Hidden / forgotten entities

Compared with `/tmp/snug_v2.json` (10 entities), the live HA instance
exposes the following right-side entities the analysis pipeline does not
read:

```
sensor.smart_topper_right_side_pid_control_output            (excluded from recorder)
sensor.smart_topper_right_side_pid_integral_term             (excluded from recorder)
sensor.smart_topper_right_side_pid_proportional_term         (excluded from recorder)
sensor.smart_topper_right_side_run_progress                  (excluded from recorder)
sensor.smart_topper_right_side_heater_head_output            (recorded; flat 0 over RC window)
sensor.smart_topper_right_side_heater_foot_output            (recorded; flat 0 over RC window)
sensor.smart_topper_right_side_heater_head_raw               (currently unavailable)
sensor.smart_topper_right_side_heater_head_temperature       (currently unavailable)
sensor.smart_topper_right_side_heater_foot_raw               (currently unavailable)
sensor.smart_topper_right_side_heater_foot_temperature       (currently unavailable)
switch.smart_topper_right_side_3_level_mode                  (ON for entire RC window)
switch.smart_topper_right_side_schedule                      (ON for entire RC window)
switch.smart_topper_right_side_quiet_mode                    (off)
number.smart_topper_right_side_sleep_temperature  (L2)       (varies -4..-10)
number.smart_topper_right_side_wake_temperature   (L3)       (varies -4..-10)
number.smart_topper_right_side_foot_warmer
number.smart_topper_right_side_speaker_volume
number.smart_topper_right_side_start_length_minutes  (T1)
number.smart_topper_right_side_wake_length_minutes   (T3)
counter.snug_right_side_override_count
climate.smart_topper_right_side_right_side                   (unavailable)
```

**The most important missed entity besides the PID series** is
`switch.smart_topper_right_side_3_level_mode`, which is **on the whole
RC window**.

---

## 3. Entity meanings, verified

Source: `PerfectlySnug/custom_components/perfectly_snug/{const,client,sensor,number,switch}.py`.

### Encoding
- **Temperature settings** (30, 31, 32, 33, 34) decode as
  `°C = (raw - 32768) / 100`. HA emits **Celsius natively**, displays
  Fahrenheit. Quantization step is 0.01 °C.
- **PID values** (42, 43, 44) decode the same way: `(raw - 32768)/100`,
  signed.
- **Output sensors** (39 BL_OUT, 40 HH_OUT, 41 FH_OUT) are **integer
  percent 0..100**. Verified: blower distribution has 100% of values
  with fractional part 0.0.
- **Run progress** (23) is an integer.
- **L1 / L2 / L3 numbers** (settings 0/1/2) decode as `display = raw - 10`,
  range −10..+10.
- **T1 / T3** are minutes (raw equals minutes).

### What the temperature_setpoint sensor really is

`SETTING_TEMP_SETPOINT` (30) is the **firmware's internal control
setpoint**, not a user-set target. The user does not have a temperature
target — they have an L1/L2/L3 dial in -10..+10 space.

Behaviour by regime (RC-on, running-on, n=77,397):

| Blower bin | n | mean(setpoint − max(body)) °F | std °F |
|---|---:|---:|---:|
|  0–5%  | 18,956 | **+7.53** | 5.67 |
|  5–15% |  3,545 | −0.03 | 0.26 |
| 15–30% | 17,329 | −0.12 | 0.25 |
| 30–60% | 30,378 | −0.14 | 0.29 |
| 60–100%|  7,189 | **−4.39** | 6.53 |

So the previous note (`2026-05-01_setpoint_is_max_body.md`) is correct
**inside the active band (5–60%)** but is wrong elsewhere. In the
deadband (0–5%) and in saturation (>60%), setpoint is *not* `max(body)`.
That changes how it can be used as a feature.

The most common setpoint values (which the prior note flagged as "idle
defaults") all sit on the 0.5 °C grid:

```
86.0 °F = 30.00 °C   (29,459 rows)
87.8 °F = 31.00 °C   (19,703 rows)
85.1 °F = 29.50 °C   (5,800 rows)
78.8 °F = 26.00 °C   (4,082 rows)
71.6 °F = 22.00 °C   (2,454 rows)
86.9 °F = 30.50 °C   (1,328 rows)
```

These are firmware-internal default/clamp values — likely an
ambient-anchored ceiling that the controller drifts toward when there is
no demand. The live snapshot above (84.6 °F, ambient 86.8 °F, blower 0)
is consistent with setpoint **slewing toward ambient** when the PID is
below the activation threshold.

### Other entities

| Entity | Real meaning |
|---|---|
| `bedtime_temperature` (number, L1) | User dial −10..+10 active during the **start (T1) phase only** when `3_level_mode=on`. Otherwise active for the whole run. |
| `sleep_temperature` (number, L2) | User dial active during **sleep phase** (between T1 and T3). Only matters when `3_level_mode=on`. |
| `wake_temperature` (number, L3) | User dial active during **wake (T3) phase**. Only matters when `3_level_mode=on`. |
| `start_length_minutes` (T1) / `wake_length_minutes` (T3) | Phase durations in minutes. |
| `3_level_mode` switch | If on, firmware advances L1→L2→L3 by run progress. **Was on for entire RC window.** |
| `schedule` switch | Whether scheduled run starts. |
| `responsive_cooling` switch | Setting 53. ON ⇒ firmware uses PID + body sensors to modulate. OFF ⇒ blower is forced to the L_active→blower table baseline. |
| `running` switch | Setting 21. **Integration overrides every output sensor to 0 when running=False**, regardless of firmware state (which freezes last value). So "blower=0 when running=off" rows are *synthetic*, not real firmware behavior. |
| `body_sensor_*` | TSL/TSC/TSR — strap or pad sensors. Confirmed accurate per integration comments. |
| `ambient_temperature` | TA — topper-onboard ambient sensor. Per repo commit `72a8fe4` ("Fix ambient temperature: use real room sensor instead of topper's inflated onboard reading"), this sensor is **biased high**. Use the external room sensor for room-temp features. |
| `heater_head_output`, `heater_foot_output` | % output to head/foot heaters. **Both are 0 across the entire RC window** (this user is in cooling mode only). Useless features for now. |
| `heater_head_temperature`, `heater_foot_temperature` | Currently unavailable; integration commit `6dcf86b` documents the THH/THF encoding is unknown. |
| `pid_control_output`, `pid_proportional_term`, `pid_integral_term` | See §1. **The actual control signal driving the blower.** |
| `run_progress` | 0..100 % of the way through the current run. Critical for knowing which of L1/L2/L3 is active. |

---

## 4. Switch semantics — confirmed

- `running=off` → integration returns **0** for blower / HH / FH outputs
  even though firmware actually freezes its last value (see
  `sensor.py::PerfectlySnugOutputSensor.native_value`). Therefore any
  `running=off` row in the historical data is synthetic; do **not** use
  these rows as "off → blower 0" supervision.
- `responsive_cooling=on` does NOT mean the firmware will drive the
  blower above zero. It means the PID controller is enabled. The PID may
  produce sub-threshold output and the blower stays at 0 (verified live).
- 4 RC switch transitions over the whole 13-day window: ON 2026-04-17
  11:39 UTC, OFF 2026-04-30 20:35 UTC. No rapid toggling.
- `schedule=on` and `3_level_mode=on` for the entire RC window.

---

## 5. The L1 problem — `bedtime_temperature` is not the active dial

`switch.smart_topper_right_side_3_level_mode` is **ON throughout the RC
window**, with `schedule=on`. In that mode the firmware advances:

```
phase = start  (length T1 = 90 min)  → L1  active
phase = sleep                         → L2  active
phase = wake   (length T3 = 90 min)   → L3  active
```

L2 history shows it is independently set on most days, ranging from -4
to -10:

```
-4 (until 2026-04-22), -6 (-22..-26), -7 (-26 brief), -6, -7, -8, -9
-10 (2026-04-30), -7 (2026-04-30 morning), -10 (final).
```

So **the "user target" feature in the existing analysis (L1 only) is
wrong for any row whose run is past the T1 minutes**. Without
`run_progress` logged, we cannot know when the firmware switched from
L1→L2 inside a given night. This adds material noise to the regression
target and probably explains a portion of the unexplained variance.

The L1_TO_BLOWER_PCT table in `appdaemon/sleep_controller_v5.py:201` is
RC-OFF baseline; the prior note says blower runs ~45 pts below it under
RC. Re-checked with the actual table values: under `RC=on, running=on,
occupied (pressure>20)`, mean `blower − baseline = -20.95`, median −18,
std 22.2 (n=43,827). The "−45" figure was high by ~25 points, probably
because of (a) using the wrong baseline table or (b) including
unoccupied rows or (c) including running=off zero-clamped rows. Our
baseline-delta is dominated by the L=-6 rows (n=20k, mean blower=31 vs
baseline=50, delta=−19). Moot anyway, because the relevant baseline
should use **L_active**, which depends on `run_progress`.

---

## 6. Other data-quality bugs found

- **Ambient sensor bias** (commit `72a8fe4`): the topper-onboard ambient
  sensor reads high. Use `room_temp_entity` configured in the
  integration instead. The 13-day file in /tmp uses
  `sensor.smart_topper_right_side_ambient_temperature`, which is the
  biased one. Re-extract with the real room sensor.
- **`bed_presence_2bcab8_right_pressure` updates every ~6 s**, an order
  of magnitude faster than the topper sensors (~31 s). Forward-fill is
  fine, but resampling to the topper cadence loses information. Better
  to compute presence-window features (mean/min over last 60 s) at the
  topper cadence.
- **All topper sensors share a 31 s update cadence** because the
  coordinator polls everything once per `UPDATE_INTERVAL=30 s`
  (`coordinator.py`). Their timestamps are essentially synchronous —
  no fancy alignment needed; use coordinator timestamp as the row key.
- **`controller_readings.setpoint_f`** in Postgres is populated by
  `_log_to_postgres` in `sleep_controller_v5.py:1801` from
  `sensor.smart_topper_<zone>_temperature_setpoint`. So PG `setpoint_f`
  is **the firmware setpoint, not a user target**, and inherits all the
  regime-dependent quirks above. Any analysis that has been treating it
  as "user target" is wrong.
- **Blower distribution** has fractional part 0.0 for 100% of values
  (n=8,316 changes), confirming clean integer-percent encoding. No
  hidden quantization weirdness on the target itself.

---

## 7. Concrete recommendations for ML

1. **Stop modeling without the PID stream.** Until
   `pid_control_output` (or P-term + I-term) is recorded, do not invest
   more in this regression. Current cap of R²≈0.21 is structural.

2. **Edit `/config/configuration.yaml` recorder excludes** to drop the
   three lines, restart recorder, and rerun nightly with the same
   household conditions. Until that's done, the cheapest mitigation is a
   tiny AppDaemon app that polls the three PID entities every 30 s into
   a Postgres table.

3. **Drop `running=off` rows entirely.** They are integration-injected
   zeros, not real labels.

4. **Compute `L_active`** from `run_progress`, T1, T3, L1, L2, L3 and
   `3_level_mode`. Use that — not `bedtime_temperature` — as the user
   dial feature.

5. **Treat `temperature_setpoint` as the firmware PV, not user target.**
   Use it only inside the active band; outside it, replace with
   `max(body)` and a categorical "regime" flag (`deadband`, `active`,
   `saturation`).

6. **Replace `ambient_temperature` with the external room sensor** as
   configured in the integration's `CONF_ROOM_TEMP_ENTITY`. The
   topper-onboard one is biased high (the 87 °F we keep seeing while the
   room is mid-70s).

7. **Use `bed_presence` as a fast feature**: 60 s rolling
   mean/min/max of the 6-second pressure stream, evaluated at each
   topper poll. Treat `pressure ≤ 20` rows as a separate regime — the
   firmware's behaviour with no body is clearly different (empty-bed
   experiment showed no blower response to body sensor manipulation).

8. **Heater outputs (HH/FH)** are flat zero across the RC window — drop
   as features for cooling-only modeling.

9. **L1_TO_BLOWER_PCT comment update**: the "mean −45 below baseline
   under RC" claim should be replaced with "mean −21, median −18, std 22"
   for occupied RC-on rows using the correct table from line 201.

---

## 8. Raw artifacts

- `/Users/mikemones/Documents/GitHub/HomeAssistant/PerfectlySnug/audit_states.json`
  — full HA `/api/states` snapshot.
- `/Users/mikemones/Documents/GitHub/HomeAssistant/PerfectlySnug/audit_hist.json`
  — RC-window history for `pid_*`, `heater_*_output`, `run_progress`,
  `schedule`, `3_level_mode`, `sleep_temperature`, `wake_temperature`.
  PID series come back empty, confirming the recorder exclude.
- `/Users/mikemones/Documents/GitHub/HomeAssistant/PerfectlySnug/audit_pid_recent.json`
  — empty array confirming PID has no history at all even for 2026-05-01.
- `/Users/mikemones/Documents/GitHub/HomeAssistant/PerfectlySnug/audit_rc_filtered.csv`
  — 77,397-row joined dataframe (RC-on, running-on, ffill'd) used for
  the by-regime stats.

---

## 9. Resolution log (2026-05-01, post-audit)

**Recorder excludes removed.** `/config/configuration.yaml` no longer
excludes `sensor.smart_topper_*_pid_*`,
`sensor.smart_topper_*_run_progress`, or
`sensor.smart_topper_*_heater_*_raw`. Backup at
`/config/configuration.yaml.bak.20260501`. HA restarted to apply.

### Entity names confirmed (both sides)

For `<side>` ∈ {`left_side`, `right_side`}:

| Role | Entity | Setting ID | Display range |
|---|---|---|---|
| L1 (bedtime / start phase dial) | `number.smart_topper_<side>_bedtime_temperature` | 0 | -10..+10 |
| L2 (sleep phase dial) | `number.smart_topper_<side>_sleep_temperature` | 1 | -10..+10 |
| L3 (wake phase dial) | `number.smart_topper_<side>_wake_temperature` | 2 | -10..+10 |
| T1 (start phase length) | `number.smart_topper_<side>_start_length_minutes` | 12 | minutes |
| T3 (wake phase length) | `number.smart_topper_<side>_wake_length_minutes` | 13 | minutes |
| 3-level mode | `switch.smart_topper_<side>_3_level_mode` | n/a | on/off |
| Run progress | `sensor.smart_topper_<side>_run_progress` | 23 | 0..100 (int) |

(Source: `custom_components/perfectly_snug/{const,number}.py`. The
display values are stored with offset `_TEMP_OFFSET=10`, so raw 0..20 on
the wire = display -10..+10.)

### run_progress → L_active mapping

The integration does **not** compute the L_active mapping itself; the
firmware advances the dial by `run_progress` and emits the result via
the per-setting sensors. Externally:

- If `3_level_mode = OFF` → **L1 active for the whole run**.
- If `3_level_mode = ON` and total scheduled run length is `total` minutes:
  - `0 ≤ run_progress < (T1 / total) × 100`     → **L1** (start phase)
  - `(T1/total)×100 ≤ run_progress < ((total − T3)/total) × 100` → **L2** (sleep phase)
  - `((total − T3)/total) × 100 ≤ run_progress ≤ 100` → **L3** (wake phase)

`total` is not directly exposed by the integration — derive it from the
schedule (start time → end time) or from observing run_progress vs
wall-clock at known T1/T3 values. Helper in
`PerfectlySnug/tools/lib_active_setting.py`.

---

## TL;DR

1. **The single biggest data bug**: the firmware's PID control output —
   the actual driver of blower% — is in the HA recorder exclude list.
   No amount of feature engineering on what we have can recover it.
2. **`bedtime_temperature` (L1) is not the active user dial** for most
   rows: 3-level mode is on and the firmware switches to L2/L3 mid-run.
   `run_progress` (also excluded from recorder) is needed to know which
   of L1/L2/L3 is live.
3. **`temperature_setpoint` ≈ max(body)** is true only in the 5–60 %
   blower band; outside it the setpoint slews toward an ambient-anchored
   default, regardless of `running=on`.
4. **`running=off` rows are synthetic zeros**; drop them from training.
5. **`ambient` sensor is biased high**; switch features to the external
   room sensor configured for the integration.
6. **`L1_TO_BLOWER` baseline delta under RC** is mean −21 (median −18,
   std 22), not −45.
