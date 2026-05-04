# Changelog

All notable changes to PerfectlySnug. Terse — for human glance, not
exhaustive history. See `docs/PROGRESS_REPORT.md` for full context.

## [Unreleased]

### Added — 2026-05-04 (P2: evaluation framework)

First patch of the 2026-05-04 stabilization+intelligence rollout (see
`docs/proposals/2026-05-04_rollout.md`). Pure additive, AppDaemon hot path
untouched.

- `sql/v6_eval_metrics.sql` — additive ALTER on `v6_nightly_summary` adds
  19 metric columns (discomfort proxy, stability, responsiveness, comfort
  outcomes, audit trail) + 2 indexes. Idempotent.
  - Rollback: `psql -f sql/v6_eval_metrics_rollback.sql`.
- `tools/eval_nightly.py` — end-of-night batch. Writes one row per
  `(night, zone)` to `v6_nightly_summary`. CLI: `--night`, `--zone`,
  `--rebuild`, `--backfill --from --to`, `--include-manual`. Skips
  `manual_mode` nights by default.
  - Diverged from doc: real `controller_readings.action` values are
    `set` / `override` / `hot_safety` / `passive` / `hold` / `freeze_hold`
    / `rate_hold` / `manual_hold` / `telemetry_only`. Controller-write set
    is `{set}`; `hot_safety` is tracked separately. Doc has been clarified.
- `tools/eval_compare.py` — A/B harness. Per-metric medians, paired
  bootstrap 95% CI (10k iters), permutation p (10k iters). Returns exit
  0/1/2 = ACCEPT/HOLD/REVERT per `evaluation.md §5`.
- `tests/test_eval_nightly.py`, `tests/test_eval_compare.py` — 19 new
  tests; full suite 400 passing.
- Backfilled 28 historical nights × 2 zones → 56 rows minus skips. Top
  finding: v5.2 left zone is in target band only 11–40% of the night,
  with 200–460 minutes "too warm" — the audit's "too hot near wake-up"
  is pervasive, not localized.

REVERT one-liner:
```bash
PGPASSWORD=sleepsync_local psql -h 192.168.0.3 -U sleepsync -d sleepdata \
  -f sql/v6_eval_metrics_rollback.sql && \
git revert <SHA>
```

### Added — 2026-05-04 (design deliverables)

Fleet-mode stabilization+intelligence audit + 7 design proposals. No
production code touched.

- `docs/findings/2026-05-04_context_audit.md` — what was tried, what
  failed, what to keep/discard. Top 5 findings: (1) time-of-night is the
  wrong axis; (2) body sensors read room+3°F on empty bed for 10–15 min
  post-entry; (3) the learner is structurally misled and defaults to
  `{}` 90% of the time; (4) 18-token patch chain in
  `CONTROLLER_PATCH_LEVEL` reflects patch-on-patch complexity; (5) the
  movement signal is logged to PG but live control still reads the
  boolean `bed_occupied`.
- `docs/proposals/2026-05-04_architecture.md` — 4-layer modular
  architecture (data ingestion, state estimation, control policy,
  learning loop) under a non-bypassable safety layer. Every v5/v6 file
  mapped to a destination.
- `docs/proposals/2026-05-04_state_estimation.md` — 7-state rule-based
  estimator (`OFF_BED`, `AWAKE_IN_BED`, `SETTLING`, `STABLE_SLEEP`,
  `RESTLESS`, `WAKE_TRANSITION`, `DISTURBANCE`) + `stability_confidence`.
  Time-of-night banned from primary thresholds.
- `docs/proposals/2026-05-04_control_policy.md` — pure
  `policy(state, offsets, prev_setting, prev_state) → Action` with
  `WAKE_TRANSITION` floor of −3 (kills the wake-up overheat).
- `docs/proposals/2026-05-04_learning.md` — 16-float per-(user, state)
  offset table; exponential 10-min credit kernel; population prior so
  Day-1 is non-zero; `MIN_EVENTS_TO_UPDATE=1`.
- `docs/proposals/2026-05-04_evaluation.md` — the metric stack and
  ACCEPT / HOLD / REVERT thresholds (this commit's doc).
- `docs/proposals/2026-05-04_features.md` — 49 raw signals → 66
  features; per-user z-score for movement; ring-buffer architecture.
- `docs/proposals/2026-05-04_rollout.md` — patches P1–P11, ordered,
  reversible, atomic. P2 ships first (this changelog entry).

### Added — 2026-05-02 night (v6 shadow scaffold)

- v6 ML controller scaffold (`ml/v6/`):
  - `regime.py` — 8-state priority-ordered classifier
  - `firmware_plant.py` — cap-table-anchored setpoint predictor
  - `right_comfort_proxy.py` — composite no-override comfort signal
  - `residual_head.py` — bounded BayesianRidge LCB residual
  - `policy.py` — `compute_v6_plan` + `V6SynthPolicy` adapter
- AppDaemon apps:
  - `v6_pressure_logger.py` — **LIVE**, writes 60s movement aggregates to
    `controller_pressure_movement` for both zones
  - `safety_actuator.py` — §7 chain: cool-only clip, master arm, per-zone
    live, CAS lease, rail mutex, rate-clamp, dead-man + heartbeat,
    `number/set_value` write
  - `sleep_controller_v6.py` — shadow-only; **committed but not loaded**
- HA helpers (in HomeAssistant repo, commit `abc1aa2`):
  - `input_boolean.snug_v6_enabled / _left_live / _right_live /
    _residual_enabled / _shadow_logging` (last is `initial: on`, others off)
  - `input_text.snug_writer_owner_left / _right` (CAS lease, init "v5")
  - `input_text.snug_v6_residual_model_path`
  - Persistence automation: re-enable `snug_v6_shadow_logging` after HA start
- PG schema (`sql/v6_schema.sql`, idempotent):
  - 15 new columns on `controller_readings`
  - new tables `controller_pressure_movement`, `v6_nightly_summary`
  - trigger `extract_actual_blower_pct` auto-populates
    `actual_blower_pct_typed` from `notes`
  - backfill of 3248 historical rows
- Tools: `tools/firmware_cap_fit.py`, `tools/v6_eval.py` extended with
  `--policy v5_2_actual / v6_synth / shadow_compare`
- Tests: 138 → 373 passing (+235 over the night)

### Fixed — 2026-05-02 night (post-audit, commit `a7c90b9`)

- Cap table loader format mismatch — `FirmwarePlant` now reads the
  `{"table": [...]}` format produced by `firmware_cap_fit`
- Bayesian Ridge α/λ swap in LCB σ formula (sklearn semantics:
  `alpha_` = noise precision, `lambda_` = weight precision)
- Dead-man timer = tick interval — bumped default to 720s, added
  `heartbeat()` API called every tick, lease re-acquirable after fallback
- Rate limiter blocks instead of clamps — now clamps toward target;
  bypassed entirely for `INITIAL_COOL`, `SAFETY_YIELD`, `PRE_BED`,
  `OVERHEAT_HARD`
- `COLD_ROOM_COMP` missing `body_trend_15m` guard — now blocked when trend
  ≥ 0.20°F/15min; added 60°F room lower bound
- `residual_lcb` meta key mismatch — shadow row column will now populate

### Earlier 2026-05-02 (afternoon → evening, pre-v6)

- `13911cb` v5.2: bed-onset event listener, body_fb occupancy gate,
  hot-rail PG notes
- `45fd9d3` v5.2: 7 P0 safety + correctness fixes from deep audit
- `48b347e` v5.2 P0 redteam fixes: learner state tag + rail helper IPC
- `0e4014d` v5.2: 3 redteam NIT fixes (initial_setting tag,
  helper-before-write, restart recovery)
- `42482df` cleanup: remove old v3/v4 controllers + 45 March one-off
  tools + tracked generated JSON
- `b29ef3f` cleanup: unused constants + stale imports
- `adb930d` docs: refresh PROGRESS_REPORT/NEXT_STEPS for redteam follow-up
- HomeAssistant: `9cbedb2` add `input_boolean.snug_right_rail_engaged`,
  `7d08805` remove 8 dead automations + 2 AC scripts,
  `0e22d3a` gitignore workspace bloat

## [v5.2 evening — 2026-05-01]

- `+noFloor`: cold overrides no longer install an all-night floor
- `+3levelWatchdog`: watchdog forces 3-level mode off
- `+rightHotRail86`: right-zone overheat rail engages at 86°F (was 88°F)
- See `docs/2026-05-01_v52_patches.md` for the full patch set
