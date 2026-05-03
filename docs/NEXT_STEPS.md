# PerfectlySnug — NEXT STEPS (v5.2 `+railHelperIPC` red-team follow-up)

> **READ FIRST every new session:** `docs/PROGRESS_REPORT.md` (especially §13.7),
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



## Tonight's acceptance — redteam follow-up patches

Check these in PG/logs after the next sleep session:

- AppDaemon banner still ends `+learnerStateTag+railHelperIPC` after any reload.
- Any immediate post-`sleep_mode=on` left override has notes containing
  `state=initial_setting`.
- Any right rail force shows helper-on before/with the `-10` setpoint write;
  no right-zone controller actuation occurs while `snug_right_rail_engaged=on`.
- After an HA restart during rail conditions, logs show helper recovery if
  `bedtime_temperature=-10`, `body_left_f >= 86`, and right occupancy is on.
- M11 remains open: confirm whether right-zone live writes produce PG rows;
  do not mark it done without observed right-side `controller_readings` rows.

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

### 1.B Mutex between controller hot-rail and `right_overheat_safety` — DONE

**Done:** `+railHelperIPC` (`48b347e`) added `input_boolean.snug_right_rail_engaged`; `right_overheat_safety` owns the helper and the controller yields right-zone writes while it is on. Cleanup commit `0e4014d` tightened helper-before-setpoint ordering and added HA-restart recovery.
**Verify tonight:** no controller right-zone write should occur while notes/logs indicate the rail helper is on.

### 1.C BedJet-window override exclusion from learner — DONE

**Done:** `+learnerInitialBedExclude` (`45fd9d3`) filters tagged initial-bed, pre-sleep, and BedJet-window overrides out of learner SQL. `+learnerStateTag` (`48b347e`) writes the source state into override notes; cleanup commit `0e4014d` covers the no-control-tick-yet initial-setting edge case.
**Verify tonight:** qualifying override notes should include `state=...`; learner SQL should exclude `initial_bed_cooling`, `pre_sleep`, and `bedjet_window` tags.

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

### 2.A Composite right-zone comfort proxy — SCAFFOLD DONE

`ml/v6/right_comfort_proxy.py` shipped 2026-05-02 night (commit `e6d6a53`,
R1B). Combines body distribution percentiles, body volatility, sleep-stage
normality, movement density. Target metric remains
`right_comfort_proxy.minutes_score≥0.5 ≤ 70`. **Open:** wire the proxy as a
gating metric once Canary-R lands; today it's exercised in tests + shadow
column only.

### 2.B Movement-density logger — DONE

`appdaemon/v6_pressure_logger.py` + table `controller_pressure_movement`
shipped 2026-05-02 night (commits `05ad059` deploy + `ffb2f2b` entity-ID fix).
Live and writing 60s aggregates for both zones. Verified in PG.

### 2.C Bounded learned residual head (LEFT zone) — SCAFFOLD DONE

`ml/v6/residual_head.py` shipped 2026-05-02 night (commit `e6d6a53`).
BayesianRidge + LCB gating implemented; α/λ semantics fixed in
post-audit commit `a7c90b9`. **Still gated off** by
`input_boolean.snug_v6_residual_enabled` (default off). Will not be
enabled until ≥14 nights of clean shadow data. Right-zone residual
remains permanently off until ≥10 right-zone controlled nights exist.
Open follow-up: array-dim validation in `_load_from_path` (R4B H3).

### 2.D Regime classifier proper — DONE

`ml/v6/regime.py` shipped 2026-05-02 night (commit `e6d6a53`) with the
8 priority-ordered regimes from the proposal. `COLD_ROOM_COMP`
`body_trend` guard + 60°F lower bound added in post-audit commit
`a7c90b9`.

### 2.E `apps.yaml` wiring + new HA helpers — PARTIAL

HA helpers all in (`abc1aa2`, R1C): `snug_v6_enabled`, `snug_v6_left_live`,
`snug_v6_right_live`, `snug_v6_residual_enabled`, `snug_v6_shadow_logging`
(default ON), `snug_writer_owner_left/right`, `snug_v6_residual_model_path`.
`apps.yaml` now loads `v6_pressure_logger`; `sleep_controller_v6:` is
present-but-commented-out, awaiting Canary-L per §V6 below.

---

## Priority 3 — Audit backlog still open

From `docs/PROGRESS_REPORT.md §10`:

- C3, C4, C5 (PG migration / stale fitted_baselines)
- H1 (rail engagement state not persisted on rail-only mutations)
- H2 (`_set_l1` callback race with override detection) — DONE by `+leftSelfWrite` (`45fd9d3`)
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

  All rows should keep `controller_version='v5_2_rc_off'`; patch-level validation is via the AppDaemon banner/source constant, not this PG field.

---

## Snapshot of current state for fast resumption

| Thing | Where | Notes |
|---|---|---|
| Live controller | `appdaemon/sleep_controller_v5.py` (latest verified 2026-05-02 evening) | `v5_2_rc_off+noFloor+3levelWatchdog+rightHotRail86+bedOnsetEvent+bodyFbOccGate+hotRailNotes+overheatBypass+rcBothZones+railNotOverride+railRestoreGuard+leftSelfWrite+bodyFbFailClosed+learnerInitialBedExclude+learnerStateTag+railHelperIPC`; `CONTROLLER_VERSION` remains `v5_2_rc_off` for learner continuity |
| Right-zone safety rail | `appdaemon/right_overheat_safety.py` | engages at 86°F / releases at 82°F / BedJet 30-min suppression; coordinates via `input_boolean.snug_right_rail_engaged` |
| Apps wiring | `appdaemon/apps.yaml` (unchanged tonight) | 2 apps registered |
| Eval framework | `tools/v6_eval.py` (built tonight by val-eval agent) | needs v5.2/v6 policy wrappers |
| Long-term v6 design | `docs/proposals/2026-05-01_recommendation.md` (862 lines) | chief-architect synthesis from 9 prior agent docs in same dir |
| Patch history | `docs/2026-05-01_v52_patches.md` + `docs/PROGRESS_REPORT.md` §12/§13/§13.7 | May 1 line-level record plus May 2 superseding patches |
| Progress log | `docs/PROGRESS_REPORT.md` §13.7 | latest red-team follow-up and cleanup pass |
| HA host | `root@192.168.0.106` | AppDaemon configs at `/addon_configs/a0d7b954_appdaemon/apps/` |
| PG | `192.168.0.3:5432/sleepdata` user=sleepsync | `controller_readings`, `nightly_summary`, `sleep_segments` |
| Bedroom room sensor | `sensor.bedroom_temperature_sensor_temperature` (Aqara) | NOT topper ambient (5-10°F high), NOT dehumidifier (stale memory entries reference this — they're outdated) |


---

## V6 — Phased Rollout Plan

> **Read first:** `docs/PROGRESS_REPORT.md §14` (what landed 2026-05-02 night)
> and `docs/proposals/2026-05-01_recommendation.md §11.2` (phased rollout).

### Phase status

| Phase | State | Started | Notes |
|---|---|---|---|
| **Shadow-A** (nights 1-7) | 🟡 IN PROGRESS | 2026-05-02 evening | Pressure logger live; shadow controller scaffold deployed but **not loaded in apps.yaml**. v5.2 still owns both dials. |
| **Canary-L** (left live, residual off) | ⏸ DEFERRED | — | Pre-canary blockers below. |
| **Canary-L + residual** | ⏸ DEFERRED | — | Requires ≥14 nights of clean shadow data. |
| **Shadow-R** | ⏸ DEFERRED | — | Per §11.2, after Canary-L stable. |
| **Canary-R** | ⏸ DEFERRED | — | Requires ≥10 right-zone controlled nights of data. |
| **Steady-state** | ⏸ DEFERRED | — | Per §11.2. |

### Shadow-A acceptance criteria (proposal §11.2)

- ≥6 nights of coverage with `controller_pressure_movement` ≥ ~1300 rows/zone/night.
- Regime distribution sanity check (NORMAL_COOL dominant; INITIAL_COOL only
  during first 30 min; COLD_ROOM_COMP only when room < 70°F).
- No v5.2 regressions in the morning report.

### Pre-Canary-L blockers (must clear before flipping any `snug_v6_*_live`)

- [ ] **WAKE_COOL sustained-duration guard** (R4B H2). Single 5-min "awake"
      stage currently fires the regime; need ≥10-min sustained or
      `body_trend_15m > 0.20°F/15min`.
- [ ] **Production `RollbackGateChecker`** (R4C C1). Test fixture exists; need
      a tool that runs against `v6_nightly_summary` after each shadow night and
      logs PASS/FAIL on each of the 7 §11.3 gates.
- [ ] **Override detection wiring** in `sleep_controller_v6._tick_zone`
      (R4B H1). Currently hardcodes `override_freeze_active=False`, so shadow
      data will never reflect what Canary-L would actually do.
- [ ] **Residual head array-dim validation** in
      `ResidualHead._load_from_path` (R4B H3). Validate
      `len(coefficients) == len(scaler_mean) == len(FEATURE_NAMES)` before
      setting `_loaded=True`.
- [ ] **Backfill ≥1 golden-case fixture** from real PG (R4C C2). Current 4
      cases are spec-synthesized.
- [ ] **Load `sleep_controller_v6`** in `appdaemon/apps.yaml` (currently
      commented out) once the above are in. Even loaded, all live-write
      switches default off — it will run shadow-only until manually armed.

### Open user decisions seeded with defaults (proposal §14)

The overnight build seeded reasonable defaults so work could proceed.
Each can be overridden — change the corresponding constant in
`ml/v6/regime.py::DEFAULT_CONFIG` (or `RegimeConfig`) or the helper
defaults in `ha-config/configuration.yaml`, then redeploy.

| # | Question | Default seeded | Where to change |
|---|---|---|---|
| 1 | Right-zone has 30+ controlled nights of runway? | **YES** — proceed with phased rollout | n/a (decision only) |
| 2 | Cold-room A/B night to disambiguate body_fb vs cold-room comp? | **DEFER** — flagged as user decision | n/a |
| 3 | BedJet entity name | `climate.bedjet_shar` (verified live tonight, currently in `heat`) | `regime.py` BedJet helper / per-app config |
| 4 | `RIGHT_PROACTIVE_HOT_F` (proactive cool threshold) | **84.0 °F** (between v5.2's 86°F rail and proposal's proactive 84°F) | `RegimeConfig` |
| 5 | All-night cold-override floor on right zone? | **NO** — preserve user's 2026-05-01 floor removal | controller logic |
| 6 | Wake_cool warm-bias removed? | **YES** — cool-bias on right when body_hot > 84°F | `RegimeConfig` / `policy.py` |
| 7 | Bayesian Ridge ~1MB model state OK? | **YES** — no objection | n/a (architecture) |
| 8 | 3-level mode forcibly OFF? | **YES** — keeps v5.2's watchdog behavior | `_ensure_3_level_off` (v5.2) and shadow record in v6 |

---
