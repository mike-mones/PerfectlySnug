# PerfectlySnug — Context Audit (2026-05-04)

**Author:** context-audit pass · **Status:** foundation for stabilization +
intelligence upgrade. Treats existing artifacts as historical, not
ground truth.

Sources read: `CHANGELOG.md`, `docs/PROGRESS_REPORT.md` (esp. §6, §10–§14),
`docs/NEXT_STEPS.md`, `docs/ML_CONTROLLER_PRD.md`, all 9 proposals in
`docs/proposals/2026-05-01_*.md`, all 8 findings in
`docs/findings/2026-05-01_*.md`, `appdaemon/sleep_controller_v5.py` (skim,
2660 LOC), `appdaemon/sleep_controller_v6.py` (645 LOC), `ml/v6/*.py`,
`ml/learner.py`, `ml/discomfort_label.py`, `ml/features.py`, `ml/policy.py`,
`tests/` (24 files).

---

## 1. WHAT WAS TRIED — chronological summary

### v3 (pre-March 2026, deleted in `42482df`)
Hand-coded per-stage settings driven by Apple Health sleep stage labels.
Single global table `{deep:-10, core:-8, rem:-6, awake:-5}` (vestige still in
`sleep_controller_v5.py:1169-1178::_setting_for_stage`). No body feedback,
no override learning. Removed; the stage table survived as a vestigial
override path.

### v4 (March 2026, deleted in `42482df`)
First attempt at "smart baseline" — added a per-cycle `CYCLE_SETTINGS`
schedule plus an EMA-of-override-deltas "learner" (`ml/learner.py`).
Promised: convergence to user preference within ~7 nights. Shipped: an
oscillator. PRD §1 cites the failure: "8.3% autonomous success rate
(1/12 nights)" and "56.5% comfort rate". 45 March one-off tools removed.

### v5 / v5.1 (April 2026)
Re-fit `CYCLE_SETTINGS` from 49 overrides across 30 nights (shrinkage
prior). Added L1→blower% table (`L1_TO_BLOWER_PCT`,
`sleep_controller_v5.py:228`) and forced **Responsive Cooling OFF** so
our setting maps deterministically to a blower band. Promised: −6.3%
in-sample MAE, −26% under-warm bias. Shipped: yes, but counterfactual
replay (PROGRESS §5) showed NEW MAE 2.13 vs v5 1.81 — fit was worse on
held-out nights.

### v5.2 (2026-04-30 → 2026-05-02, evening of `13911cb` and `45fd9d3`)
Closed-loop body-temperature correction layered on top of
`CYCLE_SETTINGS` (`sleep_controller_v5.py:1072-1097`). Below
`BODY_FB_TARGET_F=80°F`, warm the cycle baseline by `1.25 × (80−body_left)`.
Promised: held-out LOOCV MAE 3.116 → 1.633 (−48%). Shipped: yes. Then
discovered every individual sub-bug listed in §2 below, requiring
**16 chained patch tokens** in `CONTROLLER_PATCH_LEVEL`
(`sleep_controller_v5.py:224`). Right-zone twin shipped same evening
with separate constants (`RIGHT_CYCLE_SETTINGS`,
`RIGHT_BODY_FB_*`, lines 147–209) plus `right_overheat_safety.py` rail.

### v5.2 evening + redteam (2026-05-01 → 2026-05-02)
8-agent fleet audit produced 9 proposal docs and 7 P0 patches. Net
delta: bypass freezes for safety paths, RC watchdog on both zones,
rail-vs-controller mutex via `input_boolean.snug_right_rail_engaged`,
self-write race fixes, body_fb fail-closed when occupancy unknown,
learner SQL filters for initial-bed/bedjet/pre-sleep tags. Promised:
no more silent override-on-controller-write, no more empty-bed warm
bias, no more learner contamination. Shipped: all of the above.

### v6 shadow (2026-05-02 night, commits `e6d6a53`, `a7c90b9`)
Full `ml/v6/` scaffold — `regime.py` (8 priority-ordered states),
`firmware_plant.py` (cap-table-anchored predictor, `Stage-1+2`
Hammerstein), `right_comfort_proxy.py` (composite no-override comfort
score), `residual_head.py` (BayesianRidge + LCB + soft-import sklearn).
`appdaemon/sleep_controller_v6.py` is **committed but not loaded**
(`apps.yaml` block commented). `v6_pressure_logger.py` IS live —
writing 60s movement aggregates to PG `controller_pressure_movement`.
Promised: shadow data → 14-night canary → bounded residual on left →
right zone after 10 controlled nights. Shipped: scaffold + plumbing only.
Tests: 138 → 373.

---

## 2. WHAT FAILED & WHY — recurring patterns

### 2.1 Body sensors equal room+3-4°F on an empty bed; controller cannot tell
**Root pattern.** PRD §3 documented this; the controller fails to honor it.

- **2026-05-02 morning (left, `13911cb` motivating bug):** pre-bed tick
  23:32:21 wrote `setting=-4` because `body_left=75°F`, room=71°F,
  cycle-1 baseline `-10` + body_fb `+5` (cap) + room comp `−5` =
  `−4`. User climbed in at 23:36, immediately overrode `-4 → -7 → -10`.
  Cause: `_compute_setting` (`sleep_controller_v5.py:1024-1097`) had
  no occupancy gate and no event-driven re-eval on bed onset. Fix:
  `bodyFbOccGate` + `bedOnsetEvent` patches.

- **2026-05-03 morning (right):** wife cold all night because
  `body_left = 71-77°F` (empty-bed equilibrium not actual skin),
  v5.2 read it as "cold body, warm the dial," firmware ran 100%
  blower regardless. Recognized in module comment
  `sleep_controller_v5.py:166-177`. Fix: `RIGHT_BODY_SENSOR_VALID_DELTA_F=6.0`
  + write `L1=0` when `body − room < 6°F` (line 176-177).

- **`bodyFbFailClosed` patch** treats `bed_occupied=None` as
  unoccupied (correct), but the underlying problem — that the body
  sensor still reports nonsense for ~10–15 minutes after the user
  climbs in while sheets equilibrate — is **not addressed**. The
  body_fb correction will fire as soon as `bed_occupied=True`, on
  data that is still in the empty-bed regime.

### 2.2 "Too cold mid-night" — the same bug, three times
- **2026-04-30 04:27 ET, cycle 5:** user pulled −10→−7. v5.1 baseline
  refit responded by warming c5 from `-6` to `-5`.
- **2026-05-02 (post-v5.2 deploy):** user reported "a bit too cold in
  the middle of the night at some points" (PROGRESS §12). After
  override at 05:32 (`-4→-2`), `noFloor` correctly let the algorithm
  walk back colder. Working as designed, but the **trigger** (over-cool
  in cycles 3–5) was unchanged.
- **Cause:** `CYCLE_SETTINGS` (`sleep_controller_v5.py:64-90`) is
  time-of-night keyed: `cycle_num = floor(elapsed_min/90)+1`.
  Real sleep architecture is non-metronomic; the user's actual deep
  sleep block can end 60 min late, and the `-7`/`-5` baselines fire on
  the schedule, not on what the body is doing. Body feedback partially
  compensates only when body has already cooled to <80°F — by then the
  dial has been at `-7` for 20 minutes.
- **The cold-room comp + body_fb cold correction can both fire
  simultaneously** during a sweat-cool rebound. v6 added
  `body_trend_15m < 0.20` guard to `COLD_ROOM_COMP`
  (`ml/v6/regime.py:203-204`) but **v5.2's `body_fb` has no equivalent
  guard** (`sleep_controller_v5.py:1087-1097`). NEXT_STEPS §1.A still
  open.

### 2.3 "Hot near wake-up" — firmware-bound, not controller-bound
**2026-05-02 morning (left + right both reported "hot in the morning"):**
PROGRESS §12.1 finding 4 — by 07–08 ET LEFT was at `-8/-9` settings
but `actual_blower=50–65%`. Stage-1+2 firmware cascade
(`docs/findings/2026-05-01_rc_synthesis.md`) caps blower regardless of
our setting. v6 `firmware_plant.py:121-151` predicts this but the live
controller cannot escape it. **No amount of cycle-baseline tuning in
c6 will fix this** — the dial is saturated.

### 2.4 Right-zone "silent live" — the controller wasn't actuating
**2026-05-02 morning:** 23 "Right hot-rail fired" log lines but every
shadow row had `actuated:false / actuation_blocked:"ha_flag_off"`.
`input_boolean.snug_right_controller_enabled` was OFF, controller had
been shadow-only for ≥1 night while we believed it was live. PROGRESS §12.1
finding 1. Fix was UI flip + persistence automation, but: **two-key
arming with default-off helpers means a single missed flip = silent
days of "controller off"** with the user expecting otherwise.

### 2.5 Right-zone hot-rail edge cases
- **`hot_safety` erodes warm overrides:** anchored to `current_setting`
  not `max(base, override_floor)` — steps one colder every 5 min until
  the freeze elapses (`sleep_controller_v5.py:1140-1148`). Audit H5,
  open.
- **86°F `RIGHT_HOT_RAIL` is partially redundant with right body_fb
  Kp_hot=0.5:** by `body_left=84°F`, body_fb already proposes `-2`. Rail
  adds `-1`. Then `right_overheat_safety` engages at 86°F and forces
  `-10`. Three-layer escalation that shares no state until
  `+railHelperIPC` (`48b347e`) wired the `snug_right_rail_engaged`
  helper. Pre-IPC, controller mis-classified rail's `-10` write as a
  manual override (fixed in `+railNotOverride`).
- **Rail `_restore_setpoint` could stomp a user change** mid-engagement
  (fixed in `+railRestoreGuard`).

### 2.6 The learner is structurally misled
`_learn_from_history` (`sleep_controller_v5.py:2282-2356`) takes the
last override delta per (night, cycle), exponentially weights with
`LEARNING_DECAY`, clamps to `±LEARNING_MAX_BLOWER_ADJ`, applies as a
blower-percent residual. PROGRESS §6 already documented why this
fails: 47 overrides are a 1% biased subsample of v5's failure
moments; ML on them produces NEW MAE 2.13 vs v5 1.81. Yet the EMA
"learner" remained, then got patched (`learnerInitialBedExclude`,
`learnerStateTag`) to filter contamination — but the structural
override-bias trap is unchanged. **Defaults to "no overrides → no
adjustments → use raw `CYCLE_SETTINGS`,"** which means the cross-night
learning loop has done nothing useful in 30 nights.

### 2.7 `_setting_for_stage` clobbers v5.2 baselines
Audit H3, open. `sleep_controller_v5.py:1066-1070`: when SleepSync feeds
a stage, `base_setting` is **replaced** by the v3-era `{deep:-10,
core:-8, rem:-6, awake:-5, inbed:-9}` table. That overrides the v5.1
fitted `CYCLE_SETTINGS[c4]=-5` with `-10` the moment a "deep" event
arrives. Two unrelated baseline systems still coexist in live control.

### 2.8 "Need more data" paralysis
- PRD §4.5 / §5.2 set "150+ overrides" as the LightGBM gate. Have 53
  (47L+6R) after 30 nights. Implies wait ~5 more months.
- Recommendation §11.2 sets "14 nights of shadow logging" as the
  Canary-L gate. v6 shadow controller is committed but not loaded
  (PROGRESS §14.3). Each gate stack has produced more deferred phases
  than shipped behavior.

---

## 3. ASSUMPTIONS NOW STALE

### 3.1 Time-of-night is the primary control axis
**Encoded:**
- `CYCLE_SETTINGS = {1:-10, 2:-10, 3:-7, 4:-5, 5:-5, 6:-6}`
  (`sleep_controller_v5.py:64-90`)
- `RIGHT_CYCLE_SETTINGS = {1:-8, …, 6:-5}` (`:147-154`)
- `_get_cycle_num` = `floor(elapsed_min / 90) + 1` (`:1180-1182`)
- v6 keeps the trap: `RegimeConfig.cycle_baseline_left/right`
  (`ml/v6/regime.py:80-85`) plus `_normal_cool_base` (`:316-325`)
  and `_cycle_index` (`:271-275`).

**Why stale:** sleep cycles are ~80–110 min and drift across the
night; cycle 1 isn't always deep-dominant; deep blocks can extend or
compress by ±30 min. The PRD §2.1 even says so explicitly. Yet the
controller indexes by elapsed minutes, not by what the body is
actually doing. The repeated "too cold in cycles 3–5" complaints map
exactly to this: `c3=-7` and `c4=-5` fire by clock, not by stage or
body.

### 3.2 The manual adjustment moment = ground truth
**Encoded:**
- `_on_setting_change` writes `controller_value` and `delta = new−old` to
  PG at the override timestamp (`sleep_controller_v5.py:1297-1306`).
- `_learn_from_history` SQL keys on `action='override'` rows and uses
  `setting` at that exact `ts` as the label
  (`sleep_controller_v5.py:2290-2326`).
- ML PRD §4.2 declares "Override events (primary signal): when user
  changes setting, … This is the ground truth label."

**Why stale:** the user reacts to discomfort that has been building
for ~5–15 minutes. They override at the moment they decide to act,
which is correlated with but not equal to the moment their preferred
setting was wrong. Worse, BedJet residual heat, sweat-cool rebound,
sheet-temperature lag, and sleep-stage transitions all confound the
"label." `discomfort_label.py:30-50` already proposes a better signal
(±15-min lead window with movement+HR/HRV consensus) but is
pipeline-only, not consumed by live control or the learner.

### 3.3 Learning needs large data
**Encoded:**
- PRD §4 / §10: "150+ override events" gate for LightGBM.
- Recommendation §11.2: 14 nights of shadow before residual head live;
  10 right-zone controlled nights before any right residual.
- `ml/learner.py:1-25` describes "Bayesian-inspired adaptive regression
  per phase" with confidence-blending — never wired into live control;
  audit M6 calls it dead code that disagrees with everything else.

**Why stale:** with 30 nights of `controller_readings`, full per-poll
sensor + outcome history, the controller is making 5-min decisions on
60s-aggregated movement data and zero learned residuals. The structural
issue is not data quantity; it is the choice to fit `(time, override)`
pairs instead of `(state, outcome)` pairs. A ~1-parameter wife adapter
or a 2-feature regression on `(body_skin, body_trend) → setting` would
have positive support tomorrow.

### 3.4 Setting → °F is linear
**Encoded:** `L1_TO_BLOWER_PCT` (`sleep_controller_v5.py:228`),
`firmware_plant._interpolate` (`ml/v6/firmware_plant.py:196-217`,
piecewise-linear between `(-8,69°F), (0,91.4°F), (5,95.9°F)`).

**Why stale:** Stage-1 setpoint °F is approximately linear in the dial
in the cooling regime (verified empirically). Stage-2 actuation is
not — `rc_synthesis` shows `blower=0 if target<16.4 else max(13.2,
target)`, a hard Hammerstein deadband. Empty-bed step responses
(PROGRESS data audit) confirm: the dial moves the cap, but observed
blower jumps from 0 to ≥13% with no in-between values 1–9. Any
"setting=°F" assumption breaks for control purposes; only the
**setpoint** prediction is linear.

### 3.5 Bed presence is binary
**Encoded:**
- `bed_occupied = self._read(occupied_entity) == "on"`
  (`sleep_controller_v6.py:319`).
- `_on_bed_onset` toggles a single timestamp (`:216-219`).
- Live control reads boolean occupancy only.

**Why stale:** PROGRESS §"Discomfort proxy" already documented that the
pressure stream emits **371 state changes between 22:00–08:00** at
sub-second cadence; switching from 5-min PG snapshots to raw movement
density gave 12% → 28% override-detection recall. The
`v6_pressure_logger` collects this at 60s aggregation into PG
`controller_pressure_movement`. The live controller does not consume
it. The right_comfort_proxy uses it (weight 0.30) but is not wired
to actuation.

---

## 4. KEEP — components worth preserving

| Item | File | Rationale |
|---|---|---|
| Right-zone overheat safety rail | `appdaemon/right_overheat_safety.py` | Pure independent safety layer at 86°F→`-10`, hysteresis to 82°F, BedJet 30-min suppression, helper-IPC coordination. Has earned its complexity. |
| Two-key arming pattern | `sleep_controller_v5.py:204-207` (`RIGHT_LIVE_ENABLED` const + `input_boolean.snug_right_controller_enabled` HA helper) | Operational kill switch, no redeploy. Survived multiple silent-failure incidents — keep on every actuation surface. |
| Postgres `controller_readings` schema | PG `192.168.0.3/sleepdata`; logger at `sleep_controller_v5.py:2390-2530`; v6 ADD COLUMN migration in `sql/v6_schema.sql` | Single source of truth: every 5-min decision + sensor snapshot + override delta. The v6 columns (`regime`, `residual_lcb`, `divergence_steps`, `movement_density_15m`, `actual_blower_pct_typed`) are the right shape; backfill ran clean. |
| Movement-density logger | `appdaemon/v6_pressure_logger.py` (live), PG `controller_pressure_movement` | Already capturing 60s movement aggregates both zones. Foundational for state-driven (§3.5) and discomfort-proxy (§2.6) control. Don't disturb. |
| Right-zone composite comfort proxy definition | `ml/v6/right_comfort_proxy.py:62-130` | The **only** non-override discomfort metric we have for the wife. Six weighted sub-signals (body excursion, volatility, movement, stage, BedJet suppression, rail engagement) — exact PRD numbers, runnable. Use as gating metric, not yet as feature. |
| Firmware plant predictor | `ml/v6/firmware_plant.py:35-218` | Cap-table-anchored interpolator + Hammerstein `predict_blower_pct`. Honest predictor — divergence sanity, no optimization, no actuation. Keep narrow. |
| Aqara room-sensor wiring | `sensor.bedroom_temperature_sensor_temperature` referenced in `sleep_controller_v5.py:_get_room_temp_entity` and `apps.yaml` | The +5–10°F bias on topper-onboard ambient was a 6-month bug. Don't touch. |
| Initial-bed forced-cooling gate via occupancy onset event | `sleep_controller_v5.py:_on_bed_onset` + `INITIAL_BED_COOLING_MIN=30`, `INITIAL_BED_LEFT_SETTING=-10` (lines 190-193) | Event-driven, not poll-driven. Eliminates the 5-min lag at bed entry. User-stated requirement. Keep. |
| `RIGHT_BODY_SENSOR_VALID_DELTA_F` gate | `sleep_controller_v5.py:176-177`, applied around line 808 | Most-recent fix. Generalizable: a body sensor that is `<6°F` above room is reading mattress/sheet, not skin. Should also apply on left zone. |
| Rail-engaged IPC helper | `input_boolean.snug_right_rail_engaged` + `+railHelperIPC` patch | Mutex pattern between safety rail and main controller writes. Pre-IPC, controller mis-classified rail forces as overrides. Pattern is sound; reuse for any future independent safety modules. |
| `v6_eval.py` evaluation harness | `tools/v6_eval.py` | Replay framework with `--policy v5_2_actual / v6_synth / shadow_compare`. Required for any "did patch X actually help" claim. |
| Postgres nightly backup | macmini cron 04:00 → `/home/mike/backups/sleepdata/` | 14-day retention; was added after `H7` audit. Don't rely on PG durability without it. |
| Controller version + patch-level tagging in PG | `CONTROLLER_VERSION="v5_2_rc_off"` + per-row `notes` | Allows cross-night learner to scope on consistent algorithm; allows offline replay to subset by patch. |

---

## 5. DISCARD or SIMPLIFY

| Item | File | Action | Rationale |
|---|---|---|---|
| `CYCLE_SETTINGS` time-keyed table | `sleep_controller_v5.py:64-90` | **DISCARD** | Time-of-night ≠ sleep state; chasing yesterday's overrides per cycle is exactly the override-bias trap PROGRESS §6 documents. Replace with state-driven base from `(stage, body_skin, body_trend, room)`. The hand-tuned non-monotonic `c6=-6 < c5=-5` "intentional dip" is a tell that this table is fitting noise. |
| `RIGHT_CYCLE_SETTINGS` | `sleep_controller_v5.py:147-154` | **DISCARD** | n=6 right-zone overrides — these are guesses. Use a single body-targeted controller for the right zone. |
| v6 `cycle_baseline_left/right` + `_normal_cool_base` | `ml/v6/regime.py:80-85, 316-325` | **DISCARD** | v6 inherited the same trap. NORMAL_COOL should derive base from current state, not cycle index. |
| `_setting_for_stage` table | `sleep_controller_v5.py:1066-1070, 1169-1178` | **DISCARD** | v5-era unfitted; clobbers v5.2 cycle baselines (audit H3). The `{deep:-10, core:-8, rem:-6, awake:-5, inbed:-9}` numbers have no provenance. |
| `_learn_from_history` EMA on per-cycle deltas | `sleep_controller_v5.py:2282-2356` | **DISCARD** | Counterfactual replay said NEW MAE > v5; PROGRESS §6 articulated why. Defaults to `{}` 90% of the time. Replace with offline retrain on `(state → setting)` pairs once an honest dataset is built. |
| `ml/learner.py` | entire file (466 LOC) | **DELETE** | Dead code, audit M6, "disagrees with everything else." Never imported by live control. Contains stale memory comment about `superior_6000s_temperature` (line 44). |
| `ml/state/fitted_baselines.json` | file | **DELETE or REGENERATE** | Audit C5: stale relative to v5.2; only read by shadow logger; produces misleading "what-if" output. |
| `ml/contamination.py SQL_VIEW_DDL` | `ml/contamination.py:135` | **DELETE** | Audit C4: references non-existent column `body_f`; live source of truth is migration 007. |
| `ml/policy.py` Layer 1/2/3 hierarchy | entire file (180 LOC) | **DISCARD** | Never wired to live control. Has its own `BODY_OVERHEAT_HARD_F=90`/`BODY_TOO_COLD_F=76` thresholds independent of the live controller — three competing thresholds across `policy.py`, `sleep_controller_v5.py`, and `right_overheat_safety.py` is two too many. |
| `ml/discomfort_label.py` | file as currently shaped | **SIMPLIFY** | Sound concept (per-minute discomfort proxy with override > proxy > silent priority), but currently pipeline-only and untested in live control. Either consume in the controller as a feature, or delete the per-minute proxy and keep only the override label. |
| `ml/features.py smart_baseline + sin_cycle/cos_cycle` | `ml/features.py:280-320` | **DISCARD** | LightGBM-PRD-era feature engineering for a model that was never trained. `smart_baseline` is yet another competing baseline definition. |
| v6 `residual_head.py` (BayesianRidge + LCB + sklearn soft-import) | `ml/v6/residual_head.py` (448 LOC) | **DEFER or DISCARD** | 448 LOC + sklearn dep + GP quorum + α/λ semantics that already had a 1000× σ bug (R4A C2). Adds substantial maintenance surface for a residual that is gated off and may never demonstrate improvement on n=53 overrides. Keep the **interface** (a residual `(features) → Δ ∈ {-cap..+cap}` predictor) for a much simpler implementation; discard this code. |
| `CONTROLLER_PATCH_LEVEL` 18-token suffix string | `sleep_controller_v5.py:224` | **SIMPLIFY** | The string itself is evidence of over-iteration. Ship a clean version on next major patch (v5.3 or v7.0); keep the patch-token list in CHANGELOG, not in the live banner. |
| 9-proposal × 8-finding agent fleet output | `docs/proposals/2026-05-01_*.md`, `docs/findings/2026-05-01_*.md` | **ARCHIVE** | Keep as historical reference (`_archive/2026-05-01_v6_fleet/`) but do not open another N-agent fleet per session. The signal-to-noise ratio is poor; one trusted authoring loop beats 10 parallel speculative ones. |
| PRD §5.2 day-based confidence schedule (1-3 / 4-7 / 8+) | `docs/ML_CONTROLLER_PRD.md:352-358` | **DELETE from PRD** | Never built. Day-counting confidence has no support — confidence should derive from the data, not the calendar. |
| `RIGHT_HOT_RAIL_F` in controller (line 323) AND right_overheat_safety @ 86°F | `sleep_controller_v5.py:323-325` and `right_overheat_safety.py` | **CONSOLIDATE** | Two threshold systems for the same 86°F event. Keep the rail (independent safety); drop the controller-side `right_hot_streak` bias since body_fb Kp_hot already responds at 84°F+. |
| `_log_passive_zone_snapshot` for left zone | `sleep_controller_v5.py` | **KEEP, but** the right-zone live writes still don't land in `controller_readings` as `action='set'` rows (audit M11) — fix it; this is a 30-min plumbing job blocking right-zone learning. |
| ML PRD as design spec | `docs/ML_CONTROLLER_PRD.md` (574 LOC) | **REWRITE** | Premised on LightGBM + override-as-label; the override-bias trap (PROGRESS §6) and stale assumptions (§3 above) invalidate the foundation. Rewrite as a much shorter (≤150 LOC) state-driven control spec with explicit non-goals. |

---

## 6. Synthesis (one paragraph)

The controller's complexity is not load-bearing — it is the residue of
fixing each new failure with a new patch token rather than retiring the
broken assumptions underneath. The two assumptions doing the most damage
are **"time-of-night is the control axis"** (encoded six places) and
**"bed-presence is binary"** (encoded everywhere). Body sensors that
read room+3°F on an empty mattress have caused user-visible failures on
both zones in the same week despite both `bodyFbOccGate` and
`RIGHT_BODY_SENSOR_VALID_DELTA_F` patches. The next iteration should be
a **rewrite of the base-setting selector** to be state-driven (body,
trend, stage, room, movement) with a thin time-of-night prior — not yet
another patch token on `CYCLE_SETTINGS`. Most ML scaffolding (`learner.py`,
`policy.py`, `discomfort_label.py`, `residual_head.py`) is paying
maintenance cost without paying back; remove it and re-introduce a
single learned residual only after the deterministic state-driven core
is shipped and proven.
