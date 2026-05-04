# PerfectlySnug — Refactored Architecture Plan (2026-05-04)

**Status:** proposal · supersedes the implicit single-class architecture of
`appdaemon/sleep_controller_v5.py` (2660 LOC, 18 chained patch tokens at
`:224`) and the half-built v6 shadow controller (`sleep_controller_v6.py`,
645 LOC). Companion docs:
`docs/findings/2026-05-04_context_audit.md` (ground truth for keep/discard),
`docs/proposals/2026-05-04_state_estimation.md` (L2 internals — separate
file, intentionally out of scope here).

The point of this refactor is **not** new behavior. The point is to draw
sharp interfaces so that the next behavior change touches exactly one
module, not five. v5.2's failures (audit §2) are all interface failures:
`_compute_setting` (`v5:1024–1167`) does data-read, state-estimate,
control, learning, and rate-limiting in one ~140-line function, then
`_setting_for_stage` (`v5:1066–1070`) silently overrides its baselines.

---

## 1. Four layers + Safety

```
                 ┌──────────────────────────────────────────────┐
                 │                  SAFETY                       │
                 │  right_overheat_safety.py (independent app)   │
                 │  + safety_actuator.SafetyActuator (chokepoint)│
                 │  cooling-only clip · CAS lease · dead-man     │
                 │  rate-clamp · rail mutex · master arm         │
                 └────▲────────────────────────────────▲─────────┘
                      │ VETO / kill                    │ heartbeat
   HA states          │                                │
   ─────────►  ┌─────────┐   Snapshot   ┌──────────┐ Action  ┌──────┐
   PG reads ─► │ L1 Data │ ────────────►│ L2 State │────────►│ L3   │
               │ Ingest  │              │ Estimate │ Latent  │ Pol- │
               └─────────┘              └──────────┘ State   │ icy  │
                    │                        │   ▲           └──┬───┘
                    │                        │   │ UserOffsets   │ Action
                    │                        │   │               │
                    ▼                        ▼   │               ▼
               ┌────────────────────────────────────────────────────┐
               │       Postgres  controller_readings  +              │
               │       controller_pressure_movement (logging sink)   │
               └────────────────────────────────────────────────────┘
                                     ▲
                                     │ reads
                              ┌──────┴───────┐
                              │ L4 Learner   │ async, single-writer CAS
                              │ (offline +   │ → writes UserOffsets store
                              │  nightly)    │   (file or input_text)
                              └──────────────┘
```

### L1 — Data Ingestion
- **Inputs:** `self.get_state()` for all entities in `ZONE_ENTITY_IDS`
  (`v6:80–101`, copy verbatim), Aqara room sensor, BedJet, sleep stage,
  occupancy, helpers; PG reads of `controller_pressure_movement` (last 15
  min) and any cross-tick state.
- **Output:** one frozen `Snapshot` dataclass per tick. Typed fields
  (`Optional[float]` for sensors, ints for settings, explicit
  `Optional[bool]` for occupancy — never coerce `None` → `False`).
- **No control logic.** The temperature unit conversion in
  `v6:438–448::_read_temperature` lives here. Body-sensor validity
  *flagging* (`body_skin_f - room_f >= 6.0`,
  `v5:176`) lives here as `Snapshot.body_left_valid: bool` — but the
  *decision* to ignore it lives in L2/L3.
- **Boundary contract:** L1 never calls `call_service`. L1 owns one
  process-wide PG connection + retry. Failure mode: return a `Snapshot`
  with the failed field `None`; downstream layers must tolerate this.

### L2 — State Estimation
- **Input:** `Snapshot` + last N snapshots (rolling buffer in module
  state) + UserOffsets (read-only).
- **Output:** `LatentState(regime: str, regime_reason: str,
  confidence: float, base_setting: int, advice: dict)`.
- **Owns:** the regime classifier (`ml/v6/regime.py:91::classify` is the
  starting point — keep the priority-ordered first-match structure;
  rip out `_normal_cool_base`/`_cycle_index` per audit §3.1), body-sensor
  trust gate, body-trend computation, movement-density consumption.
- **Detailed design out of scope** for this doc — see
  `2026-05-04_state_estimation.md`. The contract here is just the dataclass
  shape and that L2 is **pure** (no I/O, no actuation).

### L3 — Control Policy
- **Input:** `LatentState + UserOffsets + actuator_state` (last write,
  last write time).
- **Output:** `Action(setting: int, regime: str, reason: str,
  confidence: float)` or `Action.HOLD`.
- **Owns** the per-regime mapping `LatentState → Action`, **rate
  limiting**, **bounds clamping**, fail-safe selection. The 60s min-write
  interval and ±2/tick max delta are enforced HERE before handing to
  Safety. (Safety re-enforces them as a backstop, but L3 should not
  produce illegal proposals.)
- **No sensor reads.** No PG. Pure.
- This replaces `v5:_compute_setting` entirely. The regime → base
  mapping table lives here; `BODY_FB_KP_*` constants move here from
  `v5:120–128`.

### L4 — Learning Loop
- **Trigger:** `_on_setting_change` event captured by L1 → enqueued.
  Nightly: PG sweep over closed nights.
- **Input:** `(adjustment_event, recent_state_window: list[LatentState])`
  for online; `controller_readings` join `controller_pressure_movement` for
  nightly.
- **Output:** `UserOffsets` blob (per-regime int delta, with a
  `confidence/n_support` field). Persisted to a single file
  (`ml/state/user_offsets.json`) or `input_text.snug_user_offsets`.
- **Single-writer CAS**: only the nightly batch writes; the online path
  appends to a queue table `pending_overrides`. Reuses the
  `safety_actuator` lease pattern (`safety_actuator.py:200–219`) — same
  helper, different key (`input_text.snug_offsets_owner`). No two
  processes ever write the offsets blob simultaneously.
- **Async**: never blocks L1→L2→L3.
- **Contract:** L4 cannot write the dial. L4 cannot read sensors. It
  reads PG and writes UserOffsets. If L4 is dead, the system runs on
  whatever offsets were last persisted.

### SAFETY (above all four layers)
- `appdaemon/right_overheat_safety.py` — **unchanged**, independent app,
  86°F→`-10` rail, 30-min BedJet suppression, IPC via
  `input_boolean.snug_right_rail_engaged`. Earned its complexity (audit
  Keep §1).
- `appdaemon/safety_actuator.SafetyActuator` — the chokepoint every L3
  Action passes through (`safety_actuator.py:103–196`). Already
  implements: cooling-only clip, master arm, per-zone live arm, CAS
  lease, rail mutex, rate-limit (with regime bypass), dead-man heartbeat.
  **Do not duplicate any of this logic in L3.** L3 proposes; Safety
  disposes. The existing `DummySafetyActuator` (`:275`) is the right
  stub for shadow mode.

---

## 2. File mapping table

| Current location | Future location | Notes |
|---|---|---|
| `appdaemon/sleep_controller_v5.py` (2660 LOC) | **DELETE after Phase D** | Decomposed below. Keep file in repo as `_archive/sleep_controller_v5_2026-05.py` for one quarter. |
| `v5:64–90 CYCLE_SETTINGS` | **DELETE** | Audit §3.1. Time-of-night base is the trap. |
| `v5:147–158 RIGHT_CYCLE_SETTINGS / RIGHT_BODY_FB_*` | **DELETE** | n=6 right overrides; replace with body-targeted L3 rule. |
| `v5:111–128 BODY_FB_*` constants | → `controller/policy.py` (L3 module) | Rename `BODY_FB_KP_COLD` → `POLICY_LEFT_BODY_KP`. Keep the value (1.25) and the LOOCV provenance comment. |
| `v5:176–177 RIGHT_BODY_SENSOR_VALID_DELTA_F` | → `controller/snapshot.py` (L1) | Promote to a per-zone validity flag on `Snapshot`. Apply to **left** zone too (audit Keep §8). |
| `v5:183 PRE_SLEEP_STAGE_VALUES` | → `controller/policy.py` (L3) | One-line tuple. |
| `v5:190–193 INITIAL_BED_*` | → `controller/policy.py` (L3) | The `INITIAL_COOL` regime. Already has a v6 home in `ml/v6/regime.py:67`. |
| `v5:204–209 RIGHT_LIVE_ENABLED + helpers` | → `controller/safety_arm.py` (or stay in `safety_actuator.py`) | Two-key arming pattern (audit Keep §2). Already in `safety_actuator.py:120–125`. Single source of truth. |
| `v5:212, 224 CONTROLLER_VERSION + CONTROLLER_PATCH_LEVEL` | → `controller/__init__.py::VERSION` | Drop the 18-token suffix string. CHANGELOG.md tracks history. |
| `v5:228 L1_TO_BLOWER_PCT` | → `ml/v6/firmware_plant.py` | Already present as the Hammerstein ladder. Live controller stops importing it directly. |
| `v5:392–502 initialize()` | → `controller/app.py::SleepController.initialize` | Trim by ~70%; most of this is helper-state plumbing that vanishes when state lives in dedicated modules. |
| `v5:504–732 _control_loop` (huge) | → `controller/app.py::tick()` (≤60 LOC) | Body becomes: `snap = L1.read(); state = L2.estimate(snap); action = L3.decide(state); safety.write(action)`. |
| `v5:734–992 _right_v52_shadow_tick` | **DELETE** | Replaced by L1+L2+L3 running both zones uniformly. Right is no longer a parallel code path. |
| `v5:994–1022 _shadow_log_decision` | → `controller/logging.py::log_shadow_row` | Format identical to `v6:_log_shadow_row` (`v6:575–639`). One implementation, both modes. |
| `v5:1024–1167 _compute_setting` | **DELETE** (split across L2 + L3) | The single most damaging function in the codebase — does data, state, control, learning, and bounds in one body. |
| `v5:1066–1070 + 1169–1178 _setting_for_stage` | **DELETE** | Audit §2.7 H3. Stage→base table has no provenance and clobbers v5.1 baselines. L2 already consumes `sleep_stage`; no second mapping needed. |
| `v5:1180–1182 _get_cycle_num` | **DELETE** | The defining time-of-night artifact. State-driven L2 uses `mins_since_onset` only as one feature among many. |
| `v5:1186–1257 _on_sleep_mode` | → `controller/lifecycle.py::on_sleep_mode` | Persistent sleep_start, midnight rollover. Pure event handler. |
| `v5:1259–1308 _on_setting_change` (override capture) | → **L4 input** (`controller/learning_queue.py::record_override`) | Splits the PG insert from the in-memory freeze. Freeze state lives in L3's actuator-state. |
| `v5:1309–1437 _on_right_setting_change` | → same `record_override` | Single zone-parameterized handler. |
| `v5:1438–1467 right zone freeze/rate helpers` | → `safety_actuator.py` (already has rate-clamp) | Existing actuator handles this. Delete duplicate logic. |
| `v5:1468–1480 _check_kill_switch` | → `safety_actuator.py` | Master-arm check is already there (`:120`). |
| `v5:1481–1696 occupancy + bed-onset machinery` | → `controller/occupancy.py` (L1 helper) | The whole occupancy state machine, including `_recover_zone_onset_from_presence`. Pure read + cache. |
| `v5:1697–1746 _check_midnight_restart` | → `controller/lifecycle.py` | Calendar rollover; not control logic. |
| `v5:1751–1832 _log_telemetry_only_tick + _morning_data_loss_check` | → `controller/logging.py` | Telemetry sink. |
| `v5:1857–1906 L1↔blower%, room comp, next_colder` | → `ml/v6/firmware_plant.py` (already there) + `controller/policy.py` | Room comp moves into L3 as part of the regime → action mapping. |
| `v5:1907–1962 _ensure_responsive_cooling_off, _ensure_3_level_off` | → `controller/firmware_arm.py` (one-shot init + watchdog) | Pure side-effect that asserts firmware mode flags every tick. Stays a single 30-LOC file. |
| `v5:1963–1992 _ensure_topper_running` | → `controller/firmware_arm.py` | Same. |
| `v5:1993–2110 sensor read helpers` | → `controller/snapshot.py` (L1) | All these become private helpers building one `Snapshot`. |
| `v5:2112–2168 _read_zone_snapshot, _log_passive_zone_snapshot` | → `controller/snapshot.py` + `controller/logging.py` | Split the read from the log. (Also fix audit M11: right zone live writes still don't land as `action='set'`.) |
| `v5:2170–2281 _end_night` | → `controller/lifecycle.py` | Nightly cleanup; delegate "compute night summary" to L4 batch job. |
| `v5:2282–2356 _learn_from_history` | **DELETE** | Audit §2.6, §3.2. Per-cycle EMA on override deltas is the override-bias trap. L4 replaces it with `(LatentState → setting)` regression on full nights, not on the 1% biased subsample. |
| `v5:2358–2389 _load_learned, _save_learned` | → `controller/user_offsets.py` (L4 store) | Renamed; same JSON-on-disk pattern, plus CAS lease. |
| `v5:2390–2502 _log_to_postgres` | → `controller/logging.py::log_decision_row` | Single insert function, called by the controller AND by L4 when replaying. |
| `v5:2503–2600 _log_override` | → `controller/learning_queue.py` | The override-capture write. Atomic with `record_override`. |
| `v5:2601–2627 _get_pg` | → `controller/pg.py` | Process-wide pooled connection. v6 has its own copy at `v6:549–573` — consolidate to one. |
| `v5:2628–2660 _save_state, _load_state, terminate` | → `controller/persist.py` | Cross-restart in-memory state (sleep_start, last_setting, freeze_until). |
| `appdaemon/sleep_controller_v6.py` (645 LOC) | **REPLACE** with `controller/app.py` (≤300 LOC target) | The shadow scaffold has the right shape (`v6:264–419`) but lives next to v5; merge into the new package. |
| `appdaemon/safety_actuator.py` | **KEEP** as `controller/safety_actuator.py` | No code change. The chokepoint design is correct. |
| `appdaemon/right_overheat_safety.py` | **KEEP** unchanged | SAFETY layer; independent app; do not touch. |
| `appdaemon/v6_pressure_logger.py` | **KEEP** unchanged | L1 input feed; live, working, foundational (audit Keep §4). |
| `appdaemon/apps.yaml` | **EDIT** | Replace the `sleep_controller_v5` block with `sleep_controller` (new package); keep `right_overheat_safety` and `v6_pressure_logger`; the commented v6 block goes away. |
| `ml/__init__.py` | KEEP | Empty. |
| `ml/learner.py` (466 LOC) | **DELETE** | Dead code, audit §5 / M6. Never imported by live control. |
| `ml/policy.py` (180 LOC) | **DELETE** | Audit §5. Three-rail hierarchy with its own thresholds (`BODY_OVERHEAT_HARD_F=90`, `policy.py:52`) competing with v5's. Keep the *idea* of stateless rails — implement once, in `controller/safety_actuator.py`. |
| `ml/discomfort_label.py` (383 LOC) | **SIMPLIFY → `ml/discomfort_label.py` (≤120 LOC)** | Keep `compute_candidate_signals` and the `corpus_summary` reporting. Delete `build_label_corpus` until it has a live consumer. Currently pipeline-only (audit §5). |
| `ml/features.py` smart_baseline + sin/cos cycle (`:148`, `:280–320`) | **DELETE** | LightGBM-PRD-era feature engineering for a model that was never trained. |
| `ml/features.py` everything else | **KEEP** | Used by `tools/v6_eval.py` replay (audit Keep §10). |
| `ml/data_io.py` | **KEEP** | PG/HA loaders for offline analysis. |
| `ml/contamination.py` | **KEEP**, but **delete `SQL_VIEW_DDL` at `:135`** | Audit C4: references non-existent column `body_f`. |
| `ml/sleep_curve.py` | **KEEP** | Standalone profile→curve util; not on the live path. |
| `ml/training.py` | **DEFER** | LightGBM training entry. Currently unused; revisit at L4 v2. |
| `ml/schema.py` | **KEEP** | Schema constants for offline tooling. |
| `ml/state/fitted_baselines.json` | **DELETE OR REGENERATE** | Audit C5: stale; only read by the shadow logger; misleads what-if output. |
| `ml/v6/__init__.py` | **KEEP** | |
| `ml/v6/regime.py` | **KEEP, RIP `_normal_cool_base` + `_cycle_index`** (`:271–325`) | Audit §3.1. The first-match priority structure is the L2 backbone. The cycle-baseline trap is not. |
| `ml/v6/firmware_plant.py` | **KEEP** | The honest forward predictor (audit Keep §6). Imported by L2 (divergence sanity) and `tools/v6_eval.py`. |
| `ml/v6/right_comfort_proxy.py` | **KEEP** | Wife's only non-override discomfort metric (audit Keep §5). Used by L4 nightly as the **objective**, not as a feature. |
| `ml/v6/residual_head.py` (448 LOC) | **DEFER and REPLACE** | Audit §5. Keep the *interface* `(features) → (Δ, lcb_meta)`; rewrite as ≤80 LOC `controller/learned_residual.py` once L4 v1 is live and we have an honest `(LatentState → setting)` dataset. The BayesianRidge+sklearn implementation can come back later if a simpler regressor is insufficient. |

New package layout (target):
```
PerfectlySnug/
  appdaemon/
    apps.yaml
    right_overheat_safety.py        # SAFETY (unchanged)
    v6_pressure_logger.py           # L1 feed (unchanged)
    sleep_controller.py             # AppDaemon shim → controller.app:SleepController
  controller/                       # NEW package
    __init__.py                     # VERSION
    app.py                          # AppDaemon entrypoint, ≤300 LOC
    snapshot.py                     # L1
    occupancy.py                    # L1 helper
    pg.py                           # L1 helper (single PG conn)
    state_estimator.py              # L2 (thin wrapper around ml/v6/regime)
    policy.py                       # L3
    safety_actuator.py              # SAFETY chokepoint (moved from appdaemon/)
    safety_arm.py                   # two-key arming helpers
    firmware_arm.py                 # RC-off + 3-level-off watchdogs
    learning_queue.py               # L4 ingress (override capture)
    user_offsets.py                 # L4 store with CAS lease
    learned_residual.py             # L4 model (replaces ml/v6/residual_head.py)
    logging.py                      # PG decision/shadow row writer
    lifecycle.py                    # sleep_mode, midnight rollover, end_night
    persist.py                      # cross-restart state
  ml/                               # offline analysis (unchanged shape)
    v6/regime.py                    # used by controller.state_estimator
    v6/firmware_plant.py            # used by L2 + tools/v6_eval.py
    v6/right_comfort_proxy.py       # used by L4 batch
```

---

## 3. Guardrails (specific values, enforced in `controller/safety_actuator.py` + `controller/policy.py`)

| Guardrail | Value | Source / location | Notes |
|---|---|---|---|
| Setting bounds | `setting ∈ [-10, 0]` | `policy.py:64-65 SETTING_MIN/MAX`, `safety_actuator.py:113 min(target, 0)` | Cooling-only clip is enforced at SAFETY; L3 must never propose `> 0` — refuse at boundary. |
| Max delta per tick | `±2 settings` | `safety_actuator.py:35 DEFAULT_MAX_STEP_PER_TICK` | Already enforced. Bypass list (`_RATE_LIMIT_BYPASS_REGIMES`, `:46`) preserved: `SAFETY_YIELD, INITIAL_COOL, PRE_BED, OVERHEAT_HARD`. |
| Min seconds between writes (L3) | **60s** | NEW in `controller/policy.py` | Current state: left runs every 300s tick with no inter-write floor; right has 1800s `RIGHT_MIN_CHANGE_INTERVAL_SEC` (`v5:208`). Set L3 floor to 60s — well below tick cadence so it never fires in steady state, but stops a listener-driven re-eval (bed onset, BedJet) from cascading writes. Right zone keeps 1800s as a separate L3 rule. |
| Tick cadence | 300s | `v6:61 CYCLE_INTERVAL_SEC`, `safety_actuator.py:40` | Unchanged. |
| Dead-man timer | **720s** (12 min = 2.4× tick) | `safety_actuator.py:40 DEFAULT_DEAD_MAN_SEC` | Heartbeat-based, not last-write-based (`:88-99`). Already correct. On expiry → release lease to v5/dummy and `persistent_notification`. |
| Master arm | `input_boolean.snug_v6_enabled` | `safety_actuator.py:50` | Default off; restart-persistent. |
| Per-zone live arm | `input_boolean.snug_v6_<zone>_live` | `safety_actuator.py:54` | Two-key with master. Default off. |
| CAS lease | `input_text.snug_writer_owner_<zone>` value `"v6"` | `safety_actuator.py:58, :128` | Required for every write. |
| Rail mutex | right zone + `snug_right_rail_engaged="on"` + `target > -10` → BLOCK | `safety_actuator.py:133-135` | Mutual exclusion with `right_overheat_safety.py`. |
| L2 confidence threshold for L3 to act | **`confidence ≥ 0.40`** | NEW in `controller/policy.py` | Below this: HOLD (no write). Confidence is L2's job — see state-estimation doc. |
| Fail-safe action when L2 confidence < threshold | `Action.HOLD` (no write, log only) | NEW | Explicitly NOT "fall back to cycle baseline" — that reintroduces the time-of-night trap. Hold = trust the firmware to stay where it is; the user can override. |
| Fail-safe action when L1 sensor missing | If `body_left_valid=False` AND `body_center` invalid → L2 emits `regime="SENSOR_INVALID"`, L3 → `Action(setting=0, reason="sensor_invalid")` | NEW; subsumes `RIGHT_SENSOR_INVALID_IDLE_SETTING=0` (`v5:177`) | Apply to both zones. Setting 0 = firmware idle, no cooling, no warming. |
| Body-sensor trust gate | `body_left_f - room_f ≥ 6.0°F` for ≥10 min after bed onset | L1 flag, L2 consumer | Generalizes `v5:176 RIGHT_BODY_SENSOR_VALID_DELTA_F`. Audit §2.1 says left also needs this. |
| Per-regime divergence cap | `max_divergence_steps[regime]` (already in `regime.py:67–77`) | L2 advisory; L3 enforces | Existing; keep. |
| Override freeze | 60 min, no all-night floor | `controller/policy.py` actuator-state | Audit Keep §3 + PROGRESS 2026-05-01. Manual overrides feed L4; resume after 60 min. |
| Initial-bed gate | 30 min after bed onset, force `setting = -10` | `controller/policy.py` `INITIAL_COOL` regime | `v5:190–192`. Bypass-rate-limit regime (`safety_actuator.py:46`). |

---

## 4. Migration sequencing

No dates. Each phase ships behind feature flags. Each phase ends with a
go/no-go gate based on PG-evidence, not calendar.

### Phase A — Extract L1 (zero behavior change)
- Create `controller/snapshot.py`, `controller/occupancy.py`,
  `controller/pg.py`. Move sensor-read helpers from
  `v5:1993–2110` and `v6:438–467`.
- v5 controller imports from `controller/` for reads. No control logic
  moves. No actuation change.
- **Gate:** byte-identical PG `controller_readings` rows for 3 nights
  (the row-format hash should match pre-refactor). Counter for
  `Snapshot.body_left_valid=False` lands in PG. Tests: existing v5 tests
  unchanged + a new `test_snapshot.py` exercising every Optional path.

### Phase B — Shadow-mode L2 (logs only)
- Create `controller/state_estimator.py` wrapping
  `ml/v6/regime.py:classify` (with `_normal_cool_base/_cycle_index`
  ripped out per audit §3.1). Add `confidence` field.
- v5 keeps actuating. Each tick, L2 runs and writes `regime`,
  `regime_reason`, `confidence`, `base_setting` to the existing
  `controller_readings` v6 columns (already in PG schema, audit Keep §3).
- **Gate:** 14 nights of shadow logging (matches audit §2.8 / Recommendation
  §11.2 gate) **AND** `confidence ≥ 0.40` on ≥80% of in-bed ticks.
  No second 14-night gate stack — this is **the** gate.

### Phase C — Shadow-mode L3 (logs only)
- Create `controller/policy.py`. v5 still actuates; L3 writes a
  `proposed_target` column to `controller_readings` (new column, NULL
  before this phase). Replay `tools/v6_eval.py --policy v6_synth` against
  Phase B's logged state.
- **Gate:** counterfactual replay shows L3 proposed-target hit-rate
  vs. observed user-overridden setting ≥ v5.2 baseline (1.81 MAE),
  on the same held-out nights. If worse, L3 is wrong — fix L2 first.

### Phase D — Cutover (one zone at a time, two-key armed)
- Wire `controller/app.py` → `safety_actuator.write()`. Existing
  `DummySafetyActuator` flips to live by toggling
  `input_boolean.snug_v6_left_live` (LEFT FIRST). v5 lease releases via
  CAS.
- Run **left-only live** for 7 nights; right stays v5.2-active.
- Right-zone cutover requires: (a) audit M11 fixed (right `action='set'`
  rows landing in PG), (b) 7 left-live nights with no SAFETY veto rate
  > 5%, (c) wife's `right_comfort_proxy` weekly mean not regressed
  vs. last 2 weeks of v5.2.
- **Gate (per zone):** if dead-man fires twice in 14 nights, fall back
  to v5 archive copy and treat as a P0.

### Phase E — Enable L4
- L4 nightly batch job runs against PG closed-night windows; writes
  `UserOffsets` via CAS lease. L3 starts reading them (additive offset,
  bounded ±2).
- Online learning queue (`learning_queue.py`) buffers overrides; nightly
  batch consumes the queue.
- **Gate:** L4 must demonstrate, on shadow replay, an override-rate
  reduction on the held-out 14 nights vs. L3-only. If `n_support < 5` for
  a regime, the offset is forced to 0 (no contribution) — same gate
  pattern as `residual_head` LCB but simpler.

---

## 5. ASCII diagram

(Full diagram at top of §1.) Compact form:

```
HA  →  L1  →  L2  →  L3  →  Safety  →  HA
        ↓     ↓     ↓        ↓
        └─────┴─────┴────────┴──────►  PG (decision rows)
                                          ↑
                                          │
                            L4  ←─────────┘   (reads PG)
                            L4  ──► UserOffsets (CAS write)
                            L3  ──► reads UserOffsets

Independent SAFETY app:
  right_overheat_safety  ──►  HA write (bypasses L1–L4)
                         ──►  IPC: input_boolean.snug_right_rail_engaged
                                    (read by L3 + Safety chokepoint)
```

---

## 6. Anti-goals

This refactor will **NOT**:

- **Rewrite from scratch.** Every file in §2 either moves, gets a thin
  wrapper, or is deleted with an explicit replacement. No greenfield.
- **Introduce a new framework.** AppDaemon stays. No FastAPI, no async
  frameworks, no Ray, no message bus. The "layers" are Python modules
  and dataclasses, not services.
- **Change languages.** Python only. No Rust core, no TS controller.
- **Remove `right_overheat_safety.py`.** It is the SAFETY layer. Any
  proposal to "merge it into the controller for cleanliness" is a
  regression.
- **Reintroduce time-of-night base settings.** No `CYCLE_SETTINGS`,
  `RIGHT_CYCLE_SETTINGS`, `cycle_baseline_left/right`, or
  `_normal_cool_base`. Audit §3.1 is non-negotiable; if L2 needs an
  elapsed-time feature, it consumes it as one input among many — never as
  the base-selector key.
- **Add deep learning.** No neural nets, no LSTMs, no transformers. L4
  is a per-regime additive offset with confidence; learned-residual v2
  (if it ever ships) is a small linear / Bayesian-Ridge model — see
  `learned_residual.py` interface in §2.
- **Re-fit `CYCLE_SETTINGS` on more overrides.** The override-bias trap
  (PROGRESS §6) means more overrides do not solve this; the dataset is
  structurally biased.
- **Build another N-agent fleet for design.** Audit §5 archives the last
  one. One trusted authoring loop per change.
- **Wait for "150+ overrides" before any learning.** The PRD §4.5 gate
  is dead (audit §5). L4 v1 ships at n=53 with `n_support` clamping for
  thin regimes.
- **Move state to a database for "consistency".** Cross-restart state
  stays in-process + small JSON (`controller/persist.py`,
  `controller/user_offsets.py`). PG is the analytics + learning sink, not
  the controller's working memory.
- **Add a UI.** HA helpers (`input_boolean`, `input_text`) are the UI.

---

## 7. Synthesis

The current code's complexity is **organizational, not algorithmic** — a
single `_compute_setting` function (`v5:1024–1167`) does five jobs and
each new failure mode bolted on a sixth. This refactor draws four
interfaces (Snapshot → LatentState → Action → write) so that the
*next* fix touches one module and its tests, not the whole controller.
The `safety_actuator` chokepoint and `right_overheat_safety` rail —
both already correct — survive untouched as the SAFETY layer above all
four. The state-driven control intent (`ml/v6/regime.py` first-match
priority, audit Keep §6 firmware plant, audit Keep §5 comfort proxy) is
preserved; the time-of-night residue (`CYCLE_SETTINGS`,
`_normal_cool_base`, `_setting_for_stage`, EMA-on-overrides learner) is
removed. Phase A through E let us migrate without ever leaving v5.2 as
the actuator until L2/L3 have proven themselves on logged data.
