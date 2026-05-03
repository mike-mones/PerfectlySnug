# Changelog

All notable changes to PerfectlySnug. Terse — for human glance, not
exhaustive history. See `docs/PROGRESS_REPORT.md` for full context.

## [Unreleased]

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
