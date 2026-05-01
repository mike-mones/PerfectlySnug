# 2026-05-01 — recon-data: data map for v6 PerfectlySnug controller

Agent: **recon-data**  
Scope: data availability, reliability, join keys, gaps, and information content for a v6 controller.  
Postgres inspected: `192.168.0.3:5432/sleepdata` as `sleepsync`.  
Required reading incorporated: RC synthesis, progress report, ML PRD, `sleep_controller_v5.py`; also data-audit timing/labels and RC tools.

## Executive takeaways

1. **Best current supervised label is still manual override**, but it is sparse and biased: `controller_readings` now has **54 left overrides / 21 nights** and **7 right overrides / 17 nights**. That is enough for diagnostics and guarded heuristics, not enough for a general ML residual model.
2. **Best row grain for controller modeling is the 5-minute `controller_readings` tick** because it is the only table with override labels, zone, elapsed minutes, current/effective setting, body sensors, room, setpoint, blower parsed from notes, and bed-presence snapshot.
3. **Native topper telemetry is ~31 s**, but historical override-labeled data is only 5 min. HA `ha_stats.short_term` has 5-min aggregates for 2026-04-17→04-29; long-term hourly goes back to 2026-03-04 for topper telemetry. The 31 s raw stream is not in PG except via prior JSON audits.
4. **The right-zone override-absence trap is real.** Right side has only 7 overrides, all in cycles 1–3, while the zone has high body-center/skin heat and known overheat stretches. Right-side comfort must use indirect proxies.
5. **Most informative override-soon proxy signals are weak.** In a 30-min pre-override proxy, top mutual information values are small: left `room_temp_f` MI≈0.079 nats, parsed `actual_blower_pct`≈0.044, `body_right_f`≈0.044; right `cycle_calc`≈0.019, `room_temp_f`≈0.018, `elapsed_min`≈0.017. Treat these as ranking hints, not predictive proof.
6. **Forbidden leaks:** never train on future override windows as features; do not use `action`, `override_delta`, `notes` text except to parse contemporaneous blower; do not use `effective/setting` as comfort features without modeling policy confounding; do not use Apple sleep totals for same-night real-time stage decisions.

---

## Queries run / reproducibility

Representative SQL/Python operations used:

```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema='public'
  AND table_name IN ('controller_readings','nightly_summary','sleep_segments');

SELECT zone, action, count(*)
FROM controller_readings
GROUP BY zone, action;

SELECT statistic_id, source, count(*), min(ts), max(ts)
FROM ha_stats
GROUP BY statistic_id, source;

SELECT metric_name, units, source, count(*), min(ts), max(ts)
FROM health_metrics
GROUP BY metric_name, units, source;
```

Information-content analysis used `.venv/bin/python` with `pandas` + `sklearn.feature_selection.mutual_info_classif` over `controller_readings`, deriving:

- `night = local date if hour>=18 else date-1`
- `cycle_calc = floor(elapsed_min/90)+1`
- `actual_blower_pct` from `notes ~ /actual_blower=(\d+)/`
- `override_soon_30m = 1` for non-override rows whose next same-zone override is within 0–30 minutes.

---

## Current table inventory

### `controller_readings` — gold labeled table

Schema columns inspected: `id, ts, zone, phase, elapsed_min, body_right_f, body_center_f, body_left_f, body_avg_f, ambient_f, room_temp_f, setting, effective, baseline, learned_adj, action, override_delta, controller_version, notes, setpoint_f, bed_* pressure/occupancy columns`.

| zone | rows | nights | first | last | overrides | median tick | rows/night |
|---|---:|---:|---|---|---:|---:|---:|
| left | 2,729 | 21 | 2026-04-06 19:10 UTC | 2026-05-01 12:43 UTC | 54 | 300.0 s | 130.0 |
| right | 2,365 | 17 | 2026-04-15 01:01 UTC | 2026-05-01 12:43 UTC | 7 | 300.0 s | 139.1 |

Action counts:

| zone | cooldown | deadband | freeze_hold | hold | hot_safety | manual_hold | override | passive | preference | rate_hold | set |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| left | 10 | 99 | 334 | 1,723 | 25 | 85 | 54 | 0 | 18 | 316 | 65 |
| right | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 2,358 | 0 | 0 | 0 |

**Reliability:** excellent for 5-min controller-loop context; reliable timestamps; but not native 31 s firmware telemetry. `actual_blower_pct` is not a first-class column; it must be parsed from `notes`, so missingness depends on logger path.

### `ha_stats` — HA recorder statistics backfill

Relevant coverage:

| signal family | source | rows | range |
|---|---|---:|---|
| topper body/ambient/blower/setpoint, both zones | long_term hourly | 1,341/entity | 2026-03-04→2026-04-29 |
| topper body/ambient/blower/setpoint, both zones | short_term 5-min | 3,578/entity | 2026-04-17→2026-04-29 |
| bed pressure L/R | short_term 5-min | 2,517/entity | 2026-04-20→2026-04-29 |
| bedroom Aqara temperature | short_term 5-min | 3,578 | 2026-04-17→2026-04-29 |
| PID P/I/control output | long_term hourly only | 799/entity | 2026-03-04→2026-04-06 |

**Reliability:** useful for unlabeled background distributions and validation. Not enough for override-labeled training; manual vs controller writes are not distinguishable. PID recorder exclusion means historical PID/run_progress is absent for the v5/v5.2 window; first full-telemetry night is tonight after the recorder fix.

### `sleep_segments`, `nightly_summary`, `health_metrics`

- `nightly_summary`: 25 rows, 2026-04-06→2026-04-30, 22 rows with sleep totals.
- `sleep_segments`: multiple sources (`sleepsync`, `sleepsync_phone`, `sleepsync_watch`), duplicated aggregate stage rows by night. Watch coverage: 18 nights for awake/core/deep, 18 for REM, 4 generic asleep rows.
- `health_metrics`: heart rate 4,253 rows, HRV 4,091, respiratory 988, wrist temp 983 from 2026-04-08→2026-05-01; units are often blank. HRV cadence is suspiciously high and already flagged in progress-report backlog.

**Reliability:** good for post-hoc nightly/sleep architecture analysis; poor as a real-time join unless source priority and lag are handled. Apple Health is lagged and sometimes missing; sleep-stage rows appear aggregate-by-stage for a night, not always true interval stage transitions.

### `state_changes`

Only relevant current coverage found for `sensor.superior_6000s_temperature`: 592 rows, 2026-04-05→04-08. No `bedjet`, `run_progress`, or PID rows found in `state_changes`.

---

## Signal-by-signal map

### Topper body sensors: left/center/right, per zone

| item | value |
|---|---|
| HA entities | `sensor.smart_topper_<zone>_body_sensor_left/center/right` |
| PG columns | `body_left_f`, `body_center_f`, `body_right_f`, `body_avg_f`; zone in `controller_readings.zone` |
| Units | °F in PG/HA display; integration native temps are °C converted by HA |
| Native sample rate | ~31 s value-change/poll stream from audit; PG controller sample is 5 min |
| Latency | topper/body/blower/ambient are lockstep at a poll tick; no useful sub-tick causal ordering |
| Missing rate in `controller_readings` | left: 0.0–0.2%; right: 0.0% |
| Known artifacts | center often sheet/torso/microclimate dominated; inner/outer contamination across zones; body sensors read above true skin; empty-bed body is ~67–70°F |

Distribution excluding override rows:

| zone | body_left avg/p50/p95 | body_center avg/p50/p95 | note |
|---|---|---|---|
| left/user | 78.9 / 78.7 / 84.6°F | 82.9 / 83.1 / 89.6°F | cooler-pref, v5.2 live |
| right/wife | 79.1 / 79.0 / 86.2°F | 84.2 / 86.0 / 94.7°F | runs hot; center heavily warm-sheet/blanket affected |

**Information content:** body channels are weak but real. For left override-soon, `body_right_f`, `body_avg_f`, and `body_max_f` rank after room/blower; positive windows were actually cooler than quiet rows, showing override direction confounding. For right, body channels have small MI and negative correlation with override-soon because the few right overrides are early-cycle and not representative of later overheat.

**Recommended use:** use `body_left_f` as skin-contact control input for both zones; keep `body_center_f`/`body_max_f` for safety/overheat diagnostics and warm-sheet detection, not as sole comfort targets.

### Topper onboard ambient

| item | value |
|---|---|
| Entity | `sensor.smart_topper_<zone>_ambient_temperature` |
| PG column | `ambient_f` |
| Units | °F in PG/HA display |
| Sample rate | native ~31 s; PG 5 min; HA stats hourly/5-min |
| Missing rate | left 0.2%, right 0.0% in `controller_readings` |
| Artifact | biased high vs true room; includes bed/topper microclimate |

Measured mean onboard-minus-room bias in `controller_readings`: **left +4.9°F**, **right +1.5°F**. Prior RC audit found 5–10°F high in some windows. Do not use as room temperature; it may be useful as a microclimate/blanket heat feature.

### True room temperature — Aqara bedroom sensor

| item | value |
|---|---|
| Entity | `sensor.bedroom_temperature_sensor_temperature` |
| PG | `controller_readings.room_temp_f`; `ha_stats` short/long term |
| Units | °F |
| Sample rate | PG 5 min; HA stats short-term 5 min; physical sensor slower/change-driven |
| Missing rate | 0.0% in `controller_readings` |
| Reliability | best current room reference; configured in `apps.yaml` and v5 |

Distribution: mean room around **71.0°F left / 71.2°F right** in logged rows. Left override-soon MI is highest for room temperature, but absolute MI is still modest.

### Room reference — dehumidifier `sensor.superior_6000s_temperature`

| item | value |
|---|---|
| Entity | `sensor.superior_6000s_temperature` |
| PG coverage | only `state_changes`, 592 rows from 2026-04-05→04-08 |
| Units | °F |
| Reliability | historical reference only; not current in v5 deployment |

Use only for old v3/v4 analysis. Current v5/v6 should prefer `sensor.bedroom_temperature_sensor_temperature` unless a fresh backfill proves superior coverage/alignment.

### Bed presence / pressure, per side

| item | value |
|---|---|
| Entities | `sensor.bed_presence_2bcab8_left_pressure`, `sensor.bed_presence_2bcab8_right_pressure`, binary occupancy sensors, calibration numbers |
| PG columns | raw pressure, calibrated pressure, unoccupied/occupied/trigger thresholds, `bed_occupied_*` booleans |
| Units | raw pressure %, calibrated 0–100% |
| Native sample rate | audit/progress: ~6 s or faster state changes; PG stores one 5-min snapshot |
| Missing rate in `controller_readings` | left rows: 44.2%; right rows: 37.0% (sensor added midstream) |
| Known artifact | PG 5-min snapshots miss most movement; use HA history/API or future high-res table for movement density |

Progress report measured 371 state changes between 22:00–08:00 and showed movement-density recall on overrides **28% (11/41)** vs **12% (5/41)** for 5-min pressure snapshots. This is the strongest path for right-zone indirect comfort labels.

### Apple Health sleep stages / metrics

| item | value |
|---|---|
| Tables | `sleep_segments`, `health_metrics`, `nightly_summary` |
| Sources | SleepSync watch/phone/server |
| Units | stages categorical; HR bpm; RR likely breaths/min; HRV units blank/suspect |
| Sample rate | sleep stages per segment or aggregate rows; HR/HRV irregular; RR/wrist temp ~nightly batch/episodic |
| Latency | lagged; often lands after the fact, not guaranteed real-time |
| Gaps | duplicates by source; aggregate rows with same start/end by stage; sometimes missing stage granularity |

**Join caution:** do not simply interval-join all segment rows: phone/watch rows duplicate the same night and stage aggregate rows may cover the whole sleep interval. Use source priority (`sleepsync_watch` > phone > generic), deduplicate by `(night_date, source, stage, start_ts, end_ts)`, and distinguish aggregate totals from true point-in-time stages.

### Manual overrides

| item | value |
|---|---|
| Table | `controller_readings` |
| Key fields | `action='override'`, `override_delta`, `setting` (new value), `effective` (controller/old value), `zone`, `elapsed_min`, `phase`, `notes` |
| Latency | immediate AppDaemon state listener, but right-side controller self-writes can be a gotcha |
| Missing | primary signal present for 54 left + 7 right events |

Override summary:

| zone | direction | n | mean delta | median elapsed | median body_left | median body_center | median room |
|---|---|---:|---:|---:|---:|---:|---:|
| left | cooler | 22 | -2.91 | 106.7m | 77.9°F | 78.7°F | 71.9°F |
| left | warmer | 32 | +2.03 | 269.0m | 77.0°F | 81.7°F | 69.2°F |
| right | cooler | 5 | -1.80 | 121.5m | 75.4°F | 80.1°F | 71.4°F |
| right | warmer | 2 | +1.50 | 100.1m | 79.0°F | 81.1°F | 70.5°F |

### BedJet / `climate.bedjet_shar`

No PG rows found for BedJet. Current code models the BedJet indirectly as a **right-zone occupancy-onset warm-blanket window**:

- `RIGHT_BEDJET_WINDOW_MIN = 30`
- right overheat safety suppresses rail during first 30 min after right-bed occupancy
- body sensors can inflate to 90–99°F during this window

**Required for v6:** add explicit HA state logging for `climate.bedjet_shar` and its mode/target/fan/temp if available. Until then, derive only a coarse proxy from right occupancy onset + first 30 minutes.

### PID telemetry / run_progress / L2/L3 / L_active

| signal | current state |
|---|---|
| PID P/I/control output | historical `ha_stats` only to 2026-04-06; excluded from recorder during relevant RC audit window; recorder fix landed today |
| run_progress | excluded historically; first full telemetry night is tonight |
| L2/L3 dials | live HA entities exist; not in `controller_readings` schema except nightly summary L2/L3 |
| L_active | must be derived from run_progress + T1/T3 + total scheduled duration + L1/L2/L3 + 3-level mode |

**Gotcha:** `bedtime_temperature`/L1 is not the active dial for most 3-level-mode rows. Use `tools/lib_active_setting.py`; do not use L1 as `L_active` once run_progress is available.

---

## Join keys and gotchas

### Primary keys / grains

| Source | Grain | Join key | Notes |
|---|---|---|---|
| `controller_readings` | one 5-min AppDaemon tick per zone + separate override rows | `zone`, `ts`; derived `night`; `elapsed_min` | best modeling grain; right passive rows are logged alongside left ticks |
| `ha_stats.short_term` | 5-min HA statistic row per entity | `statistic_id`, `ts` | aggregate mean/min/max; not raw state |
| `ha_stats.long_term` | hourly HA statistic row per entity | `statistic_id`, `ts` | background distributions only |
| `sleep_segments` | segment or aggregate stage row | interval join: `reading.ts BETWEEN start_ts AND end_ts`, plus `night_date` | must dedupe source and identify aggregate rows |
| `health_metrics` | irregular event samples | nearest/prior sample within tolerance by metric | lagged; not always real-time |
| Bed pressure high-res | HA history state changes, not PG except snapshots | time-window aggregation before 5-min tick | must compute rolling movement density before joining |

### Time alignment

- Topper body/ambient/blower/setpoint update in one coordinator poll around **31 s**. Audit showed no evidence that finer than 30–60 s resampling improves predictive power.
- `controller_readings` rows for left and right are near-simultaneous but not identical timestamps; join cross-zone by nearest tick within ~2 seconds or by floor/round to 5-minute bucket.
- Override rows are event rows, not regular ticks. For pre-override features, use the last non-override tick strictly before the override or a rolling window ending before override time.
- Occupancy boundaries matter: first 30 minutes after occupancy onset is an intentional max/pre-cool gate and right BedJet warm-blanket window. Do not treat body heat in that window as discomfort without BedJet state.

### Per-cycle vs per-event

- `cycle_calc=floor(elapsed_min/90)+1` is the current practical cycle proxy.
- Overrides concentrate by cycle and direction, but are biased; a cycle with no overrides is not proof of comfort.
- Right overrides are only cycles 1–3; there is no right override evidence for late-night overheat, despite known high body temperatures.

---

## Information content and redundancy

### Override-soon proxy

Definition: for each non-override row, label 1 if the next same-zone override occurs within 0–30 minutes.

| zone | rows | positives | actual overrides |
|---|---:|---:|---:|
| left | 2,675 | 197 | 54 |
| right | 2,358 | 32 | 7 |

Top mutual-information features (nats):

| zone | feature | MI | corr | proxy mean | quiet mean | interpretation |
|---|---|---:|---:|---:|---:|---|
| left | `room_temp_f` | 0.079 | -0.043 | 70.55 | 71.07 | room has signal, but sign mixes warm/cool overrides |
| left | `actual_blower_pct` | 0.044 | +0.114 | 71.74 | 59.94 | actuator state is informative; parsed from notes only |
| left | `body_right_f` | 0.044 | -0.120 | 80.88 | 82.77 | body-side signal confounded by direction/time |
| left | `body_avg_f` | 0.031 | -0.106 | 80.32 | 81.86 | weak discomfort proxy |
| left | `setting/effective` | 0.019–0.021 | negative | ~-7.0 | ~-6.3 | policy-confounded; not pure comfort |
| right | `cycle_calc` | 0.019 | -0.124 | 2.00 | 4.41 | right labels are early-cycle only |
| right | `room_temp_f` | 0.018 | +0.023 | 71.64 | 71.15 | very weak |
| right | `elapsed_min` | 0.017 | -0.126 | 129m | 352m | label sparsity artifact |
| right | `setpoint_f` | 0.014 | -0.129 | 78.81 | 84.09 | early-cycle/RC artifact |
| right | body channels | 0.009–0.013 | negative | lower before overrides | higher quiet | override-absence trap |

**Conclusion:** no single current scalar is a strong override predictor. Use features for constrained policy logic and anomaly/proxy labels, not a high-capacity model.

### Redundancy

High absolute correlations in `controller_readings`:

- Left: `body_center_f`, `body_right_f`, `body_avg_f`, `body_max_f`, and `setpoint_f` are highly redundant (|r|≈0.76–0.95). `setpoint_f` tracks body in active band and should not be treated as independent comfort evidence.
- Right: `body_center_f`, `body_right_f`, `body_avg_f`, `body_max_f` are highly redundant (|r|≈0.82–0.98). `body_left_f` is less redundant and more skin-contact relevant.
- Pressure left/right calibrated snapshots are not listed as high-redundancy because 5-min snapshots are too coarse; high-res movement density must be separately engineered.

---

## Per-zone differences

### Left / user / cool-pref

- Live v5.2 RC-off controller with body feedback.
- More labels: 54 overrides over 21 nights.
- Override directions: 22 cooler, 32 warmer. Warmer overrides dominate later cycles; cooler overrides dominate early cycle 1.
- Body-left p50≈78.7°F, p95≈84.6°F; user reportedly runs hot around body_left≈84°F.
- Left model can use overrides as sparse supervised labels, with heavy caution around policy confounding.

### Right / wife / runs hot

- Right is now v5.2 live per progress report, but historically was passive/default; `controller_readings` right rows are mostly `action='passive'`.
- Only 7 right overrides over 17 nights; 5 cooler, 2 warmer, all cycles 1–3.
- Body-left p95≈86.2°F and body-center p95≈94.7°F. Center is warm-sheet/blanket dominated; use body-left for skin-contact control and center/body_max for hazard detection.
- BedJet warm-blanket window in first 30 min can intentionally heat sensors. Treat early right-zone body spikes as contaminated unless explicit BedJet state says otherwise.

---

## The right-zone override-absence trap

The right side can be uncomfortable without generating manual overrides. Reasons: spouse may not wake or may not adjust; right zone had no historical controller; BedJet and blankets contaminate body sensors; override sample is only 7 events.

Recommended indirect comfort proxies:

1. **High-resolution movement/restlessness from bed pressure**
   - Build per-minute and rolling 5/15-min features from raw HA state changes: movement count, absolute pressure delta sum, max delta, pressure std, side-specific occupancy transitions.
   - Existing evidence: movement-density proxy caught 28% of left overrides vs 12% for 5-min pressure snapshots. Apply to `sensor.bed_presence_2bcab8_right_pressure`.

2. **Post-BedJet thermal profile percentiles**
   - Ignore or flag first 30 min after right occupancy onset and any explicit `climate.bedjet_shar` heat-on interval.
   - After suppression window, monitor `body_left_f` percentile excursions relative to her own baseline: e.g., p90/p95 exceedance minutes, slope >0.5°F/5min, sustained body_left≥86°F, body_center-body_left spread.
   - This catches hot discomfort even with no override.

3. **Sleep-stage / awakening timing and postural disruption**
   - Join deduped Apple stage/night aggregates to identify awakenings or REM-rich late windows, but do not use aggregate future totals as real-time features.
   - Proxy labels: pressure movement bursts or occupancy breaks during/after high body-left percentiles; morning reports; awake segments following heat buildup.

Optional fourth: **setting-response residual** — if body_left remains high or rising for >20–30 min after a colder setting / high blower-proxy, label as insufficient cooling support, not comfort success.

---

## Honest sample-size budget

Current `controller_readings` budget:

| zone | nights | rows | overrides | overrides/night |
|---|---:|---:|---:|---:|
| left | 21 | 2,729 | 54 | 2.6 |
| right | 17 | 2,365 | 7 | 0.4 |
| total | ~21 unique | 5,094 | 61 | — |

Per-cycle overrides:

| cycle | left | right |
|---:|---:|---:|
| 1 | 11 | 2 |
| 2 | 12 | 4 |
| 3 | 10 | 1 |
| 4 | 5 | 0 |
| 5 | 7 | 0 |
| 6 | 3 | 0 |
| 7 | 2 | 0 |
| 8+ | 4 | 0 |

This is below the PRD's ~150+ override target for a LightGBM residual layer. The right zone is especially underdetermined: no labeled late-cycle right examples.

---

## Recommended downstream feature set

### Safe primary real-time features

- `zone`
- `elapsed_min`, `cycle_calc`, `night_progress` if expected wake known
- `body_left_f` skin-contact channel
- `body_center_f`, `body_right_f`, `body_spread_f`, `body_max_f` as diagnostics/safety, not sole control input
- `room_temp_f` from `sensor.bedroom_temperature_sensor_temperature`
- `ambient_f - room_temp_f` as microclimate/blanket feature, with onboard ambient not used as room reference
- `actual_blower_pct` or RC-off blower proxy if contemporaneous and first-class logged
- bed occupancy booleans and calibrated pressure, plus high-res movement-density aggregates once available
- `mins_since_occupancy_onset`, `in_initial_bed_cooling`, `in_bedjet_window`
- `L_active` once run_progress/L2/L3 are recorded
- PID P/I/control output and `run_progress` after tonight's first full telemetry night; initially use for system identification, not supervised comfort labels

### Features to use with caution

- `setpoint_f`: firmware tracking value, not user target; only meaningful by regime. Include regime flags (`deadband`, `active`, `saturation`) if used.
- `setting`, `effective`, `baseline`, `learned_adj`: strongly policy-confounded. Useful for counterfactual replay and actuator state, dangerous as comfort predictors.
- Apple Health stages: lagged and duplicate-prone. Use only after source-priority dedupe and no future leakage.
- `actual_blower_pct` parsed from `notes`: informative but fragile. Promote to a real PG column if v6 depends on it.

### Forbidden leaks / modeling rules

1. Do not use `action`, `override_delta`, `manual_hold`, `freeze_hold`, or future override timing as model features.
2. Do not use post-override rows as if they were independent comfort labels without downweighting and time-window limits.
3. Do not use same-night total sleep, total REM/deep minutes, or morning summary fields for real-time inference.
4. Do not train on `running=off` blower=0 rows; they are integration-injected synthetic zeros.
5. Do not treat L1/`bedtime_temperature` as active setting when 3-level mode is on; use `L_active`.
6. Do not use topper onboard ambient as room temperature.
7. Do not evaluate on random row splits; use leave-one-night-out or rolling-origin validation.

---

## Data gaps to close next

1. Add explicit PG columns or side table for PID P/I/control output, `run_progress`, L1/L2/L3, T1/T3, 3-level mode, and derived `L_active`.
2. Log BedJet state (`climate.bedjet_shar`) with timestamps and mode/temperature/fan attributes.
3. Store high-resolution bed-pressure movement aggregates in PG by minute; avoid relying on 5-min snapshots.
4. Promote `actual_blower_pct` from `notes` text into a typed column.
5. Deduplicate and normalize Apple Health sleep-stage rows; document source priority and whether rows are aggregate or point intervals.
6. Keep collecting at least 10 right-zone live-controller nights before fitting right-side parameters; use proxies meanwhile.
