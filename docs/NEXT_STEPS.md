# PerfectlySnug — NEXT STEPS (post 2026-05-01 evening deploy)

> **READ FIRST every new session:** `docs/PROGRESS_REPORT.md` (especially §11),
> then `docs/2026-05-01_v52_patches.md`, then this file. Then read the user's
> morning report and decide on next action.
>
> Also review (long-term roadmap): `docs/proposals/2026-05-01_recommendation.md`.

---

## Priority 0 — Tomorrow morning (do these before anything else)

### 0.1 Read the user's morning subjective report

What we ship next is gated on this. Possible labels per zone: too hot / too
cold / just right. Free-text observations welcome. **Do not propose changes
before reading the report.**

### 0.2 Run the morning analysis pipeline

```bash
cd ~/Documents/GitHub/HomeAssistant/PerfectlySnug

# 1. Right-zone shadow log digest
.venv/bin/python tools/eval_right_shadow.py

# 2. Pull last night's controller_readings + look for new behaviors
.venv/bin/python <<'PY'
import psycopg2, sys
conn = psycopg2.connect(host='192.168.0.3', dbname='sleepdata',
                         user='sleepsync', password='sleepsync_local')
cur = conn.cursor()

# Overrides + hot_rail fires + 3-level watchdog hits in last 12 h
cur.execute("""
  SELECT ts, zone, action, setting, body_left_f, room_temp_f, notes
  FROM controller_readings
  WHERE ts > now() - interval '12 hours'
    AND (action = 'override' OR notes ILIKE '%hot_rail%' OR notes ILIKE '%3_level%')
  ORDER BY ts
""")
for row in cur.fetchall():
    print(row)
PY

# 3. AppDaemon log scan for this session's markers
ssh root@192.168.0.106 "ha addon logs a0d7b954_appdaemon 2>&1 | grep -E '(hot-rail|3-level|override_floor|MANUAL OVERRIDE)' | tail -40"
```

### 0.3 Decide: keep / tune / revert

Decision matrix:

| Morning report | Action |
|---|---|
| User both: "just right" | Leave running. Gather more data. |
| User: "cold mid-night" | Same complaint as 2026-04-30. The cold-room comp is unchanged tonight; consider Priority 1.A (body-trend-guarded cold-room comp from synth). |
| User: "warm in the morning" | The c6=-6 cycle baseline already exists; investigate if c6 fired correctly. |
| Wife: "too hot" | Check if right hot-rail fired. If yes → working as intended; consider lowering streak threshold. If no → body_left didn't cross 86°F; consider lowering threshold to 84°F (with awareness of body_fb double-counting). |
| Wife: "too cold" | Hot-rail probably over-fired or BedJet -10 ran past her preference window. Consider tightening streak threshold to 3 (≈15 min). |
| Either: "much worse than before" | Revert. See `docs/2026-05-01_v52_patches.md` § Rollback. |

---

## Priority 1 — Patches that should ship this week (deferred from tonight)

These come from the synth fleet's v6 recommendation. Each is a discrete
patch; pick the highest-value one based on tomorrow's morning report and
data, ship it, observe ≥1 night, repeat.

### 1.A Body-trend-guarded cold-room comp (left zone)

**Source:** synth recommendation §6.1 + red-comfort §3.1.
**Why:** v5.2's cold-room comp can over-warm the user when their body is
sweat-cooled (body says "I'm cold" but the cold reading is misleading).
This is the failure mode synth flagged for the 2026-04-30 Case A
(01:37-02:05 cluster, user dragged setting from -10 to -3).
**What:** Gate cold-room comp on `body_trend_15m < +0.20°F/15min`. If
body is rising fast (sweat-cool with imminent recovery), skip the warm
bias.
**Effort:** ~30 min. Need to track body history (3 samples over 15 min).
**Risk:** could under-warm on legitimate cold nights.

### 1.B Mutex between controller hot-rail and `right_overheat_safety`

**Source:** red-safety §1.
**Why:** Both apps write to `number.smart_topper_right_side_bedtime_temperature`
without coordination. Currently OK because the rail's force-write at -10
is monotonically more aggressive than the controller's -1 step bias, but
there's a race window where the controller could write a warmer value
right after the rail engaged.
**What:** New `input_boolean.snug_right_rail_engaged`. `right_overheat_safety`
turns it ON before forcing -10 and OFF after release. Controller's
`_right_v52_shadow_tick` reads it and yields control entirely while ON.
**Effort:** ~1 hour. Modifies `right_overheat_safety.py`,
`sleep_controller_v5.py`, adds an HA helper (requires `ha core restart`
or just adding to UI).
**Risk:** low; new helper defaults to off.

### 1.C BedJet-window override exclusion from learner

**Source:** user request 2026-05-01 ("BedJet shouldn't be taken into
account when determining preferences").
**Why:** If wife overrides during the first 30 min after right-bed onset
(BedJet warm-blanket), her override is influenced by external heat input,
not her thermal preference. Counting it in `_learn_from_history` biases
the learner toward warmer-than-preferred settings.
**What:** In `_learn_from_history`'s SQL query, exclude rows where
`mins_since_onset < 30` on the right zone. Need to add
`mins_since_onset` to `controller_readings` writes (or join to it from
zone-occupancy events).
**Effort:** ~1-2 hours; PG-schema-aware.
**Risk:** low; can ship behind a feature flag.

### 1.D Right-zone live writes → `controller_readings` rows

**Source:** audit backlog M11; recon-data finding.
**Why:** Right-zone live actuations currently log only to
`/config/snug_right_v52_shadow.jsonl`, not PG. This means the cross-night
learner cannot see right-side overrides at all — the learner is left-zone-only.
**What:** In `_right_v52_shadow_tick`, after `actuated=True`, write a
`controller_readings` row tagged `zone='right'` (or use the `notes`
field to disambiguate). Match the schema used by left-zone writes.
**Effort:** ~30 min of plumbing.
**Risk:** schema mismatch could break PG inserts; test in shadow mode first.

### 1.E `tools/v6_eval.py` policy wrapper for current controller

**Source:** val-eval delivered the eval framework but no v6 policy is
wired.
**Why:** Without an executable replay framework, we can't quantify "did
patch X actually beat v5.2 on case Y." The synth's case A/B/C are sitting
unmeasurable.
**What:** Wrap the current `sleep_controller_v5.py` decision logic in a
`Policy` interface for `tools/v6_eval.py`. This unlocks counterfactual
replay on any future patch.
**Effort:** ~2 hours.
**Risk:** none, it's read-only analysis.

---

## Priority 2 — Medium-term (v6 build-out, weeks not days)

From `docs/proposals/2026-05-01_recommendation.md`:

### 2.A Composite right-zone comfort proxy

Synth §5. Combines body distribution percentiles, body_30m_sd
(restlessness), sleep-stage normality (from Apple Health), movement
density (from bed-presence pressure %). Wife's override-absence trap
mitigation. Target metric:
`right_comfort_proxy.minutes_score≥0.5 ≤ 70` (vs v5.2's 115).

### 2.B Movement-density logger

New AppDaemon app `appdaemon/v6_pressure_logger.py` + new PG table
`controller_pressure_movement`. Reads
`sensor.bed_presence_2bcab8_left_pressure` /
`sensor.bed_presence_2bcab8_right_pressure` and computes a per-cycle
movement intensity. Becomes the leading indicator for thermal
discomfort (memory: top quartile predicts 2.4× the override rate).

### 2.C Bounded learned residual head (LEFT zone)

`ml/v6/residual_head.py`. BayesianRidge + tiny GP under LCB. Residual
constrained to ±1 step from the regime classifier output. Trained
nightly on accumulated overrides + comfort proxy. Deployed only after
≥14 nights of shadow-mode logging confirms it does not regress.
Specifically NOT deployed on the right zone until ≥10 right-zone
controlled nights exist.

### 2.D Regime classifier proper

The current patch set keeps v5.2's monolithic decision logic. The synth
recommendation calls for a deterministic regime classifier with
priority-ordered states (`pre_bed`, `initial_cool`, `bedjet_warm`,
`safety_yield`, `override_respect`, `cold_room_comp`, `wake_cool`,
`normal_cool`). This is the structural change that unlocks per-regime
tuning + transparent failure mode debugging.

### 2.E `apps.yaml` wiring + new HA helpers

The synth wrapper requires `input_boolean.snug_v6_enabled`,
`snug_v6_residual_enabled`, `snug_right_rail_engaged`,
`snug_v6_left_live`, `snug_v6_right_live`, plus `input_text` writer-lease
fields. Each addition is one entry in
`ha-config/configuration.yaml` followed by `ha core restart`.

---

## Priority 3 — Audit backlog still open

From `docs/PROGRESS_REPORT.md §10`:

- C3, C4, C5 (PG migration / stale fitted_baselines)
- H1 (rail engagement state not persisted on rail-only mutations)
- H2 (`_set_l1` callback race with override detection)
- H3 (`_setting_for_stage` clobbers v5.2 cycle baselines)
- H5 (`hot_safety` erodes warm overrides)
- H8 (HRV cadence implausibly high — health receiver bug)
- M1-M11 (various, lower priority)

H4 ("cold overrides have no all-night floor") is **partially resolved**
by tonight's deploy (no override has a floor anymore).

---

## Priority 4 — Things to keep an eye on (not bugs yet)

- **The 3-level watchdog might log noisy warnings** if some other
  automation is toggling 3-level on. If `WARNING: 3-level mode ON ...`
  appears every few minutes in the AppDaemon log, find what's flipping
  it on and fix that root cause.
- **Right hot-rail spam.** If body_left stays ≥86°F for hours (very
  hot night), the rail will fire every 5 min until rate-limit blocks
  the write. Each fire logs one line. Acceptable for now; consider a
  cooldown if the log gets noisy.
- **Cross-night learner inheriting tonight's data correctly.**
  CONTROLLER_VERSION is unchanged so `_learn_from_history` should
  ingest tonight's overrides. Verify in tomorrow's analysis:

  ```sql
  SELECT controller_version, COUNT(*)
  FROM controller_readings
  WHERE action = 'override'
    AND ts > now() - interval '24 hours'
  GROUP BY 1;
  ```

  All rows should be `v5_2_rc_off`.

---

## Snapshot of current state for fast resumption

| Thing | Where | Notes |
|---|---|---|
| Live controller | `appdaemon/sleep_controller_v5.py` (deployed to HA at 19:33 ET 2026-05-01) | `v5_2_rc_off` + 4 patches; `CONTROLLER_PATCH_LEVEL` constant tracks them |
| Right-zone safety rail | `appdaemon/right_overheat_safety.py` (unchanged tonight) | engages at 86°F / releases at 82°F / BedJet 30-min suppression |
| Apps wiring | `appdaemon/apps.yaml` (unchanged tonight) | 2 apps registered |
| Eval framework | `tools/v6_eval.py` (built tonight by val-eval agent) | needs v5.2/v6 policy wrappers |
| Long-term v6 design | `docs/proposals/2026-05-01_recommendation.md` (862 lines) | chief-architect synthesis from 9 prior agent docs in same dir |
| Tonight's patch detail | `docs/2026-05-01_v52_patches.md` | line-level diff record |
| Progress log | `docs/PROGRESS_REPORT.md` §11 | this session's entry |
| HA host | `root@192.168.0.106` | AppDaemon configs at `/addon_configs/a0d7b954_appdaemon/apps/` |
| PG | `192.168.0.3:5432/sleepdata` user=sleepsync | `controller_readings`, `nightly_summary`, `sleep_segments` |
| Bedroom room sensor | `sensor.bedroom_temperature_sensor_temperature` (Aqara) | NOT topper ambient (5-10°F high), NOT dehumidifier (stale memory entries reference this — they're outdated) |
