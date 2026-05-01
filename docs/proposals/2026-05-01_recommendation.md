# v6 PerfectlySnug — Recommended Controller Design (synth)

**Author:** synth · **Date:** 2026-05-01 · **Status:** chief-architect recommendation
**Inputs:** rc_synthesis, PROGRESS_REPORT, ML_PRD, sleep_controller_v5.py,
right_overheat_safety.py, all 7 wave-1 outputs (recon-deployed, recon-data,
opt-mpc, opt-scratch, opt-hybrid, opt-learned, val-eval, v6_eval.py),
and both wave-2 red teams (red-comfort, red-safety).

---

## 1. Executive summary

**Recommended approach:** a **deterministic regime-classifier controller**
(opt-hybrid spine, with the red-comfort fixes applied), wrapped in an
**unconditional safety actuator** (red-safety §"Cross-cutting wrapper"),
with a **bounded learned residual** (opt-learned LCB head) added on the
LEFT zone *only after* 14 nights of shadow-mode logging. The firmware's
two-stage cascade (rc_synthesis) is treated as a known inner plant — used
as a **forward predictor** for divergence guards, never as the planner.
Random-shooting MPC and full-scratch RC-off control are explicitly
**rejected** as primary controllers (red-comfort §6.1, red-safety §283
"most surface area"); their best ideas survive as features.

**Rationale (5 sentences).**
(1) The 30-night / 61-override corpus cannot defend any model bigger than
a 1-parameter wife adapter, so the structural lifting must come from
explicit physics and explicit policy, not capacity. (2) Every red-team
finding converged on a single brittle surface — the right-zone
override-absence trap and the BedJet/cold-room tails — and a transparent
regime classifier is the only architecture in the fleet where each
failure mode maps to one named rule we can disable in isolation. (3)
opt-learned's LCB residual gives us a free upgrade path that *cannot* be
worse than v5.2 by more than ±1 step, which is exactly what the right
zone needs while we wait for data. (4) opt-mpc's plant model is the
right framing for a *predictor* but its action selection is unsafe under
cold-room and silent-overheat extrapolation; we keep the model, drop the
optimizer. (5) Right-zone live writes do not turn on until ≥10 right-zone
controlled nights and a verified rail mutex exist — we cannot fix the
override-absence trap by guessing harder.

**One-line beat-v5.2 metric per zone (must hold on `tools/v6_eval.py`):**

- **LEFT:** Override MAE on cold-room rows (room < 70 °F) drops from
  v5.2's 2.18 to **≤ 1.30** (the cluster-A regime is the headline win),
  and Case A passes (`median setting ≥ -5 over 01:37–02:05`).
- **RIGHT:** `right_comfort_proxy.minutes_score_ge_0_5` drops from
  v5.2's 115 to **≤ 70** (≥ 39 % reduction), with `time_too_hot_min ≤ 320`
  (vs v5.2's 400) and zero new minutes above 86 °F vs v5.2 baseline.

Override MAE on the right zone is **not** a usable metric (n=7, MDE 0.8
steps); the proxy is the gate per val-eval §"Right-zone evaluation".

---

## 2. Architecture

### 2.1 Stack (top-down)

```
┌──────────────────────────────────────────────────────────────────────┐
│  L_active write (number.smart_topper_<side>_{bedtime|sleep|wake}_*)  │
└──────────────────────────────────────────────────────────────────────┘
              ▲    (atomic compare-and-set via input_text writer-owner)
              │
┌─────────────┴─────────────────── safety_actuator ────────────────────┐
│  rate-limit, mutex w/ right_overheat_safety, dead-man, fallback,     │
│  cooling-only clip, override-floor enforcement, BedJet arbitration   │
└──────────────────────────────────────────────────────────────────────┘
              ▲
┌─────────────┴────────── regime classifier (opt-hybrid+) ─────────────┐
│  PRE-BED → INITIAL-COOL → BEDJET-WARM → SAFETY-YIELD → OVERRIDE      │
│  → COLD-ROOM-COMP* → WAKE-COOL → NORMAL-COOL                         │
│  *body-trend-guarded; right-zone wake is COOL not warm-bias          │
└──────────────────────────────────────────────────────────────────────┘
              ▲                                  ▲
              │                                  │
┌─────────────┴────── per-regime rule ─────┐ ┌──┴────── residual head ─────┐
│ base = phase_target + body_fb + room_fb  │ │ (opt-learned LCB, k=1.0)    │
│ proxy_term = movement_density (right)    │ │ Δ ∈ {-1..+1}, gated by      │
│                                          │ │ quorum + n_support + IQL    │
└──────────────────────────────────────────┘ └─────────────────────────────┘
              ▲                                  ▲
┌─────────────┴───── sensor fusion (opt-scratch+) ─────────────────────┐
│  body_skin = b_left (BedJet/post-BedJet 60-min weighted)             │
│  body_hot  = max(b_left, b_center) when post_bedjet_min > 60         │
│  room      = bedroom Aqara EMA                                       │
│  movement_density_15m from raw bed_pressure state-changes            │
│  L_active  via lib_active_setting (3-level mode forced OFF)          │
└──────────────────────────────────────────────────────────────────────┘
              ▲
┌─────────────┴───── firmware Stage-1+2 plant model (opt-mpc, predictor only)─┐
│  Used in two places: (a) divergence-guard sanity (predict expected blower   │
│  given target L_active, alert if delta_real > 25 pts); (b) sleep-stage-     │
│  unaware rollout for BedJet residual decay model.                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 What is borrowed from each proposal

| Proposal | Kept in v6 | Dropped |
|---|---|---|
| **opt-hybrid** | regime classifier as control spine; per-regime authority map; cold-room-comp regime (with red-comfort §3.1 body_trend guard); v5.2 fallback flag | wake-transition warm-bias on right (red-comfort §3.2 — replaced with cool bias); bedjet-warming "no write" (red-comfort §3.3 — replaced with explicit bedtime hold) |
| **opt-learned** | conservative residual head with LCB (k=1.0), n_support cap ladder (≤1, ≤2, ≤3), GP/Ridge quorum, daily nightly refit, silent-tick down-weighting; freeze Δ=0 for 60 min after override | composite reward as primary live signal (kept offline); residual on RIGHT until ≥10 right-controlled nights (red-comfort §5.1) |
| **opt-scratch** | sensor fusion structure; movement_density `proxy_term` (with red-comfort §6.3 body-conditioned sign); 5-phase target table; phase machine with stage+clock fallback | RC-off bypass for primary architecture (red-safety §"opt-scratch"); body_fusion's down-weighted body_center on right (red-comfort §4.1 — replaced with `max(b_L, b_C)` for hot side) |
| **opt-mpc** | firmware Stage-1+2 plant model as **forward predictor** for divergence/sanity; Hammerstein deadband awareness in target snap | random-shooting / CEM optimizer (jitter, red-safety §1.3); hard pin `u_right ≥ -4` during BedJet (red-safety §5); risk-logistic `π_z` (extrapolation, red-comfort §2.2) |

### 2.3 Why a hybrid (not tribal pick)

Each single proposal failed the val-eval claim rule on the existing 61-override
corpus (red-comfort §6.4 numerical scoring: all four MAE-improvement claims
sit ≤ 0.46-step MDE). The composite *raises floor* (LCB cap + classifier
guards) without claiming false ceiling. The recommendation deliberately
**does not promise** statistically significant override-MAE improvement on
the current corpus; it promises (a) a deployable structure that beats v5.2
on the named cases A/B/C and on the right-zone proxy, and (b) a road from
shadow → bounded residual → richer model as data accrues.

---

## 3. Per-zone policies with explicit divergences

| Aspect | LEFT (user) | RIGHT (wife) |
|---|---|---|
| Primary control input | `body_left` (BedJet immune) | `body_left` for cold side; **`max(body_left, body_center_post60)` for hot side** (red-comfort §6.3 #1) |
| RC switch | OFF (enforced every tick) | OFF (enforced; was passive — first night live tonight if shadow OK) |
| 3-level mode | OFF (force in `_on_sleep_mode`); also subscribe to L1/L2/L3 listeners just in case | OFF (force) |
| Phase targets (`base` before terms) | pre-bed -10, settle -10, deep -7, rem -5, wake_cool -6 | pre-bed -10, settle -10, deep -6, rem -5, wake_cool **-6** (NOT +2 bias) |
| Cycle-baseline fallback if phase=NA | `[-10,-10,-7,-5,-5,-6]` (v5.2 left) | `[-8,-7,-6,-5,-5,-5]` (v5.2 right) |
| Body FB cold side | Kp_cold=1.25, target=80, cap +5 | Kp_cold=0.30, target=80, cap +4 |
| Body FB hot side | Kp_hot=0 (rail handles ≥90); but **+1 step proactive** at body_left>84 sustained 2 ticks (red-comfort §4.7) | Kp_hot=0.50 on `max(b_L, b_C_post60)`, cap −4; **proactive cool** at hot ≥84 sustained 10 min (overrides classifier) |
| Room comp | hot 4 pts/°F above 72; cold 4 pts/°F below 72; extra 3 pts below 63; **disabled when room < 65 °F** (red-comfort §6.3 #3, defer to v5.2) | hot 4 pts/°F above 72; **cold 0** (matches deployed); same <65 °F envelope guard |
| Movement-density proxy_term | Kproxy=1.0, capped −1 step, sign-conditioned on `body > 78` (red-comfort §6.3 #5) | Kproxy=2.0, capped −2 steps, sign-conditioned on `body_hot > target` |
| Override floor | warm overrides → all-night floor; cold overrides → 60-min freeze + 60-min cooling preference (no all-night ceiling — red-safety H4 partial fix) | warm override → all-night warm floor; **cold override → all-night cooling ceiling** (`u_right_max_warmer = override_value`) — closes the M11/H4 gap red-safety §7 calls out |
| Kill switch | 3 manual changes / 5 min → manual_hold rest of night (v5.2 behavior) | NEW: 2 manual changes / 10 min → manual_hold (lower threshold because lower override rate) |
| Initial-bed window | 30 min `-10`; **shrink to 15 min if room < 66 °F at onset** (red-comfort §6.3 #6) | 30 min `-10`, BedJet suppression coexists; same cold-room shrink |
| BedJet | n/a | Read `climate.bedjet_shar` state; `bedjet_active = (mode in {heat,turbo}) OR (mins_since_onset ≤ 30 AND no climate state)`; body_center weight=0 for **60 min** post-BedJet-deactivation (red-comfort §6.3 #2) |
| Residual head Δ | LCB cap ±1 (n_support<5), ±2 (<15), ±3 (else); enabled after night 14 | **DISABLED** until ≥10 controlled-zone nights; until then Δ≡0 |
| Authority over right_overheat_safety rail | n/a | Yields unconditionally via `input_boolean.snug_right_rail_engaged` flag (NEW; rail writes it on engage/release) |

---

## 4. Override-bias trap & override-absence trap mitigations

### 4.1 Override-bias trap (left zone)

The 54 left overrides are a 1 % subsample concentrated at v5.2's failures.
Naive fits chase the override mean and oscillate. Adopted mechanisms:

1. **Residual-anchored learning, never level.** The residual head learns
   `Δ` on top of v5.2; the only way Δ ≠ 0 is positive evidence against
   v5.2 (opt-learned §4.2).
2. **Silent positive evidence.** Every non-override tick is a weak positive
   label for the controller's chosen value, weighted `0.25 + 0.5·night_quality`,
   capped at `override_weight / 6` (opt-learned §4.1).
3. **LCB drop.** `Δ_safe = sign(Δ̂) · max(0, |Δ̂| − k·σ)` with `k=1.0`. On
   sparse cells σ is large → Δ collapses to 0 → defers to v5.2 (which is
   often already right outside the cluster-A regime).
4. **Drop initial-bed overrides from training corpus.** Six of 54 fired
   inside the user-mandated 30-min `-10` window — they are policy-noise
   not preference (opt-learned §4.3).
5. **Direction-stratified walk-forward CV.** Warm/cold balanced per fold
   (opt-learned §4.4) so the held-out-cold-night MAE is meaningful.

### 4.2 Override-absence trap (right zone)

8 of 25 right-zone nights have ≥10 min body_left > 86 °F with **zero**
overrides (red-comfort §1). Silence is not consent. Adopted mechanisms:

1. **Composite right-zone comfort proxy** is the gating metric, not
   override MAE (val-eval §"Right-zone evaluation"). Definition in §5.
2. **Hot-side max-channel rule.** Body-FB and proactive-cool both read
   `body_hot = max(body_left, body_center_post60)`, not skin alone
   (red-comfort §4.1, §6.3 #1).
3. **Proactive cool trigger.** If `body_hot ≥ 84 °F for 10 min` AND not
   in BedJet window → step one cooler than current, regardless of
   classifier (independent of override history).
4. **Movement-density escalation.** If `movement_density_15m > p75 of
   her quiet baseline` AND `body_hot > 82 °F` for 10 min → step one
   cooler. Sign of movement_density is body-conditioned (red-comfort
   §6.3 #5 — no cold-stress-cools-harder bug).
5. **Asymmetric override floor.** Cold overrides install an
   **all-night cooling ceiling** on the right side
   (`u_right_warmest_writable = override_value`) — explicit fix for
   recon-deployed §4.3 (no right-zone floor) and red-safety §7. Releases
   only on user warm-override.
6. **Wake-cool not wake-warm on right.** Replaces opt-hybrid's `+2`
   bias with `−1` if `body_hot > 84 °F` else `0` (red-comfort §3.2).
7. **No residual head writes on right** until ≥10 nights of
   controlled right-zone data are in PG (opt-learned §3.3 + red-comfort
   §5.1) — the silent-overheat fix is structural, not learned.
8. **Cross-zone prior** (deferred). When residual *does* turn on, the
   right head is `w_trunk + γ·z_right` where γ is the only right-fit
   parameter (opt-learned §3.2). Until then γ=0.

---

## 5. Composite right-zone comfort proxy — exact definition

Reconciliation of val-eval §"Right-zone comfort proxy", red-comfort §6.3,
opt-learned §3.1, opt-scratch §4.2, recon-data §"Override-absence trap".
**Per right-zone tick** (5-minute cadence; computed trailing-only):

```python
def right_comfort_proxy(row, history_15m, history_30m, climate_bedjet,
                        zone_baseline_movement_p75):
    # Sub-signal 1: body_hot excursion (max-channel; red-comfort §6.3 #1)
    body_hot = row.body_left_f
    if minutes_since_bedjet_inactive(climate_bedjet, row.ts) > 60 and \
            row.body_center_f is not None:
        body_hot = max(row.body_left_f, row.body_center_f)
    body_hot_excess = clip((body_hot - 84.0) / 4.0, 0, 1)         # 0 at 84°F, 1 at ≥88°F

    # Sub-signal 2: body_skin cold excursion (rare for her; kept for symmetry)
    body_cold_excess = clip((73.0 - row.body_left_f) / 5.0, 0, 1)

    body_out_of_range = max(body_hot_excess, body_cold_excess)

    # Sub-signal 3: 30-min thermal volatility on body_left
    body_30m_sd_excess = clip((rolling_sd(history_30m.body_left_f) - 1.2) / 2.0, 0, 1)

    # Sub-signal 4: movement density over last 15 min of raw bed-pressure
    # state-changes (recon-data §4.7 — not 5-min snapshots)
    md = movement_density_15m(history_15m.bed_pressure_right_states)
    movement_excess = clip(md / max(0.05, 2 * zone_baseline_movement_p75), 0, 1)

    # Sub-signal 5: stage-bad
    stage_bad = 1.0 if row.stage in ("awake", "inbed", "unknown", None) else 0.0

    # Sub-signal 6: BedJet residual flag (suppresses comfort weight; not added)
    in_bedjet = climate_bedjet.is_heating(row.ts) or \
                row.mins_since_onset_right < 30
    if in_bedjet:                       # within window we cannot trust hot signal
        body_out_of_range = 0.0
        body_30m_sd_excess *= 0.3

    # Sub-signal 7: rail engagement event (very bad)
    rail_engaged = right_rail_engaged_at(row.ts)

    score = ( 0.30 * body_out_of_range
            + 0.20 * body_30m_sd_excess
            + 0.30 * movement_excess
            + 0.10 * stage_bad
            + 0.10 * (1.0 if rail_engaged else 0.0) )
    return clip(score, 0.0, 1.0)
```

**Reported metrics per night (and rolling 7-night):** `mean`, `p90`,
`minutes_score_ge_0_5` (per val-eval). v5.2 baseline numbers from
`tools/v6_eval.py --policy baseline`:

| metric | v5.2 | v6 target |
|---|---:|---:|
| mean | 0.217 | ≤ 0.180 |
| p90 | 0.337 | ≤ 0.280 |
| minutes_score ≥ 0.5 | 115 | **≤ 70** |
| time_too_hot_min (right) | 400 | ≤ 320 |

(Note: v5.2 baseline as printed by `v6_eval.py` uses a slightly different
proxy — pressure delta /8 — which is a 5-min snapshot. v6 uses raw
state-change movement density per recon-data; the **direction** of the
gate stays the same and we re-baseline on v6's proxy on the first 7
nights of v6 shadow.)

---

## 6. Pseudocode — AppDaemon cycle callback

Full runnable shape; `_safety_actuator` (full body in §7) and
`_classify_regime` are factored out.

```python
# appdaemon/sleep_controller_v6.py
from datetime import datetime, timedelta
from tools.lib_active_setting import active_setting_from_row

CYCLE_INTERVAL_SEC = 300

class SleepControllerV6(hass.Hass):

    # ── Wiring ────────────────────────────────────────────────────────
    def initialize(self):
        self._heartbeat = self.datetime()
        self._regime_history = {"left": deque(maxlen=12), "right": deque(maxlen=12)}
        self._last_write = {"left": None, "right": None}        # {ts, value, dial}
        self._override_state = {"left": OverrideState(), "right": OverrideState()}
        self._residual_left = ResidualHead.load("/config/apps/ml/state/v6_left.pkl") \
                              if path.exists("...v6_left.pkl") else NullResidual()
        self._residual_right = NullResidual()                   # disabled until N_R≥10

        # Force the 3-level dial OFF every night
        self.listen_state(self._on_sleep_mode, E_SLEEP_MODE, new="on")

        # Listen to L1, L2, L3 on both zones (catch L_active overrides too)
        for side in ("left", "right"):
            for kind in ("bedtime", "sleep", "wake"):
                self.listen_state(self._on_setting_change_any_dial,
                                  f"number.smart_topper_{side}_side_{kind}_temperature",
                                  side=side, kind=kind)

        # Subscribe to right_overheat_safety rail-engaged HA flag
        self.listen_state(self._on_rail_state_change,
                          "input_boolean.snug_right_rail_engaged")

        self.run_every(self._tick, "now", CYCLE_INTERVAL_SEC)
        self.run_every(self._dead_man_check, "now", 60)

    # ── Per-tick entry point ──────────────────────────────────────────
    def _tick(self, kwargs):
        try:
            self._heartbeat = self.datetime()
            if not self._is_sleeping():
                return
            ctx = self._read_full_context()
            if ctx is None:                          # snapshot unavailable
                self._fallback_v52(reason="ctx_none")
                return

            for zone in ("left", "right"):
                self._tick_zone(zone, ctx)
            self._heartbeat_persist()
        except Exception as e:
            self.log(f"v6 tick failed: {e!r}", level="ERROR")
            self._fallback_v52(reason=f"exception:{type(e).__name__}")

    def _tick_zone(self, zone, ctx):
        snap = ctx.snap[zone]

        # 0. Read L_active (true active dial, not L1)
        l_active = active_setting_from_row(ctx.ha_row, side=f"{zone}_side",
                                           total_min=ctx.total_min)
        if l_active.dial is None or not l_active.three_level_off:
            self._force_3_level_off(zone)            # idempotent
            self._fallback_v52(zone, reason="3level_on"); return

        # 1. Sensor fusion
        body_skin = snap.body_left_f
        post_bedjet_min = self._post_bedjet_minutes(zone, ctx)
        body_hot = (max(snap.body_left_f, snap.body_center_f)
                    if (zone == "right" and post_bedjet_min > 60
                        and snap.body_center_f is not None)
                    else body_skin)
        room_f = ctx.room_temp_aqara_ema
        movement_15m = self._movement_density_15m(zone)

        # 2. Regime classification (priority order — see §6.1)
        regime = self._classify_regime(zone, snap, ctx, room_f,
                                       body_skin, body_hot, movement_15m)

        # 3. Per-regime base setting
        if regime == "safety_yield":
            return self._safety_yield_log(zone, ctx)
        if regime == "pre_bed" or regime == "initial_cool":
            target = -10
        elif regime == "bedjet_warm":
            target = self._bedjet_hold_setting(zone, ctx)        # not "no write"
        elif regime == "override_respect":
            target = self._override_state[zone].current_floor()
        elif regime == "cold_room_comp":
            target = self._cold_room_setting(zone, ctx, body_skin, room_f)
        elif regime == "wake_cool":
            target = self._wake_cool_setting(zone, ctx, body_hot)
        else:                                                     # normal_cool
            target = self._normal_cool_setting(zone, ctx, body_skin, body_hot,
                                               room_f, movement_15m)

        # 4. Proactive right-zone overheat guard (override-absence trap)
        if zone == "right":
            target = self._right_proactive_cool(target, body_hot, post_bedjet_min,
                                                movement_15m, ctx)

        # 5. Residual head (Δ ∈ {-3..+3}; 0 if disabled or low support)
        head = self._residual_left if zone == "left" else self._residual_right
        delta = head.safe_delta(snap, ctx, regime)
        target = target + delta

        # 6. Cooling-only clip + integer
        target = int(round(max(-10, min(0, target))))

        # 7. Forward-predictor sanity (opt-mpc Stage-1+2 as advisor only)
        v52_advice = self._v52_recommend(zone, snap, ctx)
        if abs(target - v52_advice) > MAX_DIVERGENCE_STEPS[regime]:
            self._log_divergence(zone, target, v52_advice, regime)
            target = self._clamp_toward(target, v52_advice,
                                         MAX_DIVERGENCE_STEPS[regime])

        # 8. Hand to safety actuator (handles rate, mutex, deadband, write)
        self._safety_actuator.write(zone, l_active.dial, target,
                                    regime=regime, residual=delta,
                                    snapshot=snap, ctx=ctx)

    # ── Regime classifier (deterministic, first-match priority) ───────
    def _classify_regime(self, zone, snap, ctx, room_f, body_skin, body_hot, md):
        # 0. Safety override has highest priority for right
        if zone == "right" and self._right_rail_engaged():
            return "safety_yield"

        # 1. Pre-bed
        if ctx.sleep_stage in ("inbed", "awake") and ctx.elapsed_min < 30:
            return "pre_bed"

        # 2. Initial-cool (window scales with room — red-comfort §6.3 #6)
        win = INITIAL_BED_COOLING_MIN
        if room_f is not None and room_f < 66.0:
            win = 15
        if ctx.mins_since_onset[zone] is not None \
                and ctx.mins_since_onset[zone] <= win:
            return "initial_cool"

        # 3. BedJet warm window (right only) — explicit climate state preferred
        if zone == "right" and self._bedjet_active(ctx):
            return "bedjet_warm"

        # 4. Override respect (60 min freeze)
        if self._override_state[zone].in_freeze(ctx.now):
            return "override_respect"

        # 5. Cold-room comp (red-comfort §3.1 — body_trend guard added)
        body_trend_15m = self._body_trend_15m(zone)
        if (room_f is not None and room_f < 69.0 and 65.0 <= room_f
                and body_skin is not None and body_skin < 77.0
                and (body_trend_15m is None or body_trend_15m < 0.20)):
            return "cold_room_comp"

        # 6. Wake-cool (NOT warm bias). Fires on stage AND body_trend (red-comfort §3.4).
        if (ctx.sleep_stage == "awake" and ctx.elapsed_min > 300
                and body_trend_15m is not None and body_trend_15m > 0.20):
            return "wake_cool"
        if ctx.cycle_index >= 5 and body_hot is not None and body_hot > 83.0:
            return "wake_cool"

        # 7. Default
        return "normal_cool"

    # ── Per-regime setting helpers ────────────────────────────────────
    def _cold_room_setting(self, zone, ctx, body_skin, room_f):
        base = self._phase_target(zone, ctx)
        body_fb = self._body_fb_cold(zone, body_skin)
        room_comp = min(5, 1.5 * (69.0 - room_f))
        cap = -3 if zone == "left" else -5
        return max(-10, min(cap, base + body_fb + room_comp))

    def _wake_cool_setting(self, zone, ctx, body_hot):
        base = self._phase_target(zone, ctx)
        cool_bias = -1 if (body_hot is not None and body_hot > 84) else 0
        return max(-10, base + cool_bias)

    def _normal_cool_setting(self, zone, ctx, body_skin, body_hot,
                              room_f, movement_15m):
        base = self._phase_target(zone, ctx)
        body_fb = self._body_fb(zone, body_skin, body_hot)
        room_fb = self._room_comp(zone, room_f)
        proxy = self._movement_proxy_term(zone, movement_15m,
                                           body_skin, body_hot)
        return base + body_fb + room_fb + proxy

    def _right_proactive_cool(self, target, body_hot, post_bedjet_min,
                               movement_15m, ctx):
        if post_bedjet_min < 60:                       # don't trigger near BedJet
            return target
        hot_streak = self._hot_streak_min(zone="right", thresh=84.0)
        if hot_streak >= 10:
            target = min(target, max(-10, target - 1))
        if movement_15m > self._zone_md_p75["right"] * 2 and body_hot > 82:
            target = min(target, max(-10, target - 1))
        return target
```

---

## 7. Required safety wrapper (per red-safety)

Implements every item in red-safety §"Mandatory actuator wrapper".
This is the **only** code that touches HA write services for the
controlled entities.

```python
class SafetyActuator:
    """All v6 writes go through this. Single writer per zone, contract-bound."""

    MAX_STEP_PER_15MIN = 1
    MAX_STEP_PER_30MIN = 2
    MIN_TICK_FOR_REVERSAL = 2
    DEAD_MAN_THRESHOLD_SEC = 600          # 10 min
    BODY_VALID_F = (55.0, 110.0)
    ROOM_VALID_F = (50.0, 90.0)

    def write(self, zone, l_dial, target, *, regime, residual, snapshot, ctx):
        # 1. Cooling-only & type sanity
        if not isinstance(target, int) or target > 0 or target < -10:
            return self._abort(zone, "invalid_target", target)

        # 2. Sensor sanity (red-safety §6)
        if not self._sensors_ok(snapshot, ctx):
            return self._abort(zone, "sensors_invalid", target,
                               fall_back=True)

        # 3. Right-zone mutex with rail
        if zone == "right":
            if self._read_state("input_boolean.snug_right_rail_engaged") == "on":
                return self._log_yield(zone, "rail_engaged")
            # Cooldown after rail release: 10 min (red-safety regression test #2)
            since_release = self._mins_since("right_rail_released")
            if since_release is not None and since_release < 10:
                target = max(-10, min(target, -7))      # caution-cool

        # 4. Acquire writer lease (input_text compare-and-set)
        owner_field = f"input_text.snug_writer_owner_{zone}"
        if not self._cas(owner_field, expect_in=("v6", ""), set_to="v6"):
            return self._log_yield(zone, "lease_busy")

        # 5. Override floor / ceiling (asymmetric per zone; §3 table)
        target = self._override_state[zone].clamp(target)

        # 6. Rate limit & step rate
        last = self._last_write[zone]
        if last:
            since = (ctx.now - last["ts"]).total_seconds() / 60.0
            if regime not in ("safety_force", "initial_cool", "pre_bed"):
                if since < 30 and abs(target - last["value"]) > self.MAX_STEP_PER_30MIN:
                    target = last["value"] + sign(target - last["value"]) \
                             * self.MAX_STEP_PER_30MIN
                if since < 15 and abs(target - last["value"]) > self.MAX_STEP_PER_15MIN:
                    return self._log_block(zone, "rate_limit_15m")

            # Reversal requires 2 consecutive ticks of opposite direction
            if self._reversal(target, last) and not self._reversal_streak(zone, target):
                return self._log_block(zone, "reversal_dwell")

        if target == last["value"] if last else False:
            return self._log_hold(zone)

        # 7. Write to the *active dial* (lib_active_setting), not L1
        entity = f"number.smart_topper_{zone}_side_{l_dial}_temperature"
        self.call_service("number/set_value", entity_id=entity, value=target)
        self._last_write[zone] = {"ts": ctx.now, "value": target, "dial": l_dial}

        # 8. Release lease
        self._cas(owner_field, expect_in=("v6",), set_to="")

        # 9. Log to PG (controller_readings; M11 fix — right writes land in PG)
        self._log_pg(zone=zone, ts=ctx.now, action="set",
                     setting=target, regime=regime, residual=residual,
                     snapshot=snapshot, controller_version="v6_synth")

    # ── Dead-man timer (red-safety §5) ────────────────────────────────
    def _dead_man_check(self, kwargs):
        if (datetime.now() - self._heartbeat).total_seconds() > self.DEAD_MAN_THRESHOLD_SEC \
                and self._read_state(E_SLEEP_MODE) == "on":
            self.log("v6 heartbeat stale > 10 min — restoring v5.2", level="ERROR")
            self._fallback_v52(reason="dead_man")
            self._notify_user("PerfectlySnug v6 dead-man fallback fired")

    # ── Missing data behavior ─────────────────────────────────────────
    def _sensors_ok(self, snap, ctx):
        if snap.body_left_f is None or not (55 <= snap.body_left_f <= 110):
            return False
        # Stuck-sensor: variance ≈ 0 for >30 min vs other channels move
        if self._stuck(snap, channel="body_left", window_min=30):
            return False
        # Cross-channel disagreement post-BedJet
        if (snap.body_center_f is not None and snap.body_left_f is not None
                and snap.body_center_f - snap.body_left_f > 10
                and self._post_bedjet_min > 60):
            return False
        if ctx.room_temp_aqara is None:
            return False                              # no room → no comfort opt
        return True

    # ── Deterministic v5.2 fallback ───────────────────────────────────
    def _fallback_v52(self, reason, zone=None):
        """Verified deterministic code path — re-imports v5.2 module fresh.
        Restores 3-level OFF + RC OFF (left) before yielding control."""
        self.log(f"v6 → v5.2 fallback ({reason}, zone={zone})", level="WARNING")
        self._enforce_firmware_baseline()             # 3-level off, RC off
        for z in [zone] if zone else ("left", "right"):
            v52 = self._v52_recommend(z, ...)
            self._raw_write(z, v52, source="v5_2_fallback")
        self._notify_user(f"v6 fallback ({reason}) — running v5.2")
```

**Required HA helpers (one-time):**

- `input_boolean.snug_v6_enabled` — top kill switch (off → pure v5.2)
- `input_boolean.snug_v6_residual_enabled` — disables learned Δ (=0)
- `input_boolean.snug_right_rail_engaged` — written by `right_overheat_safety` on engage/release (the missing IPC channel red-safety §1 demands)
- `input_text.snug_writer_owner_left` / `_right` — compare-and-set lease
- `input_boolean.snug_v6_left_live` / `_right_live` — per-zone arms

---

## 8. Parameter ranges + initial values + tuning

| Param | Initial | Range | How to tune |
|---|---:|---|---|
| `INITIAL_BED_COOLING_MIN` | 30 | 15–45 | shrink to 15 if room<66; user feedback if morning report says cold-pre-cooled |
| `BODY_FB_KP_COLD_LEFT` | 1.25 | 0.5–2.0 | tune so cold-room-comp + body_fb predicts within ±1 step of overrides on cold-room nights |
| `BODY_FB_KP_HOT_RIGHT` | 0.50 | 0.3–0.8 | tighten if right_proxy.minutes_score≥0.5 stays > 70 after 7 nights |
| `BODY_FB_MAX_DELTA_LEFT` | 5 | 3–6 | reduce to 4 if observed left override count rebounds in c2-c3 |
| `BODY_FB_MAX_DELTA_RIGHT` | 4 | 3–5 | hold; primary lever is hot-side max-channel |
| `ROOM_BLOWER_REFERENCE_F` | 72 | 70–74 | already deployed at 72; revisit after 7 cold-room nights |
| `ROOM_BLOWER_*_COMP_PER_F` | 4 | 2–6 | hold |
| `COLD_ROOM_COMP_CAP_LEFT` | -3 | -2..-5 | lower (more aggressive warm) only after 2 confirmed cold-discomfort nights with no overheat |
| `COLD_ROOM_COMP_CAP_RIGHT` | -5 | -4..-7 | hold; right cold-disc is rare |
| `BEDJET_RESIDUAL_MIN` | 60 | 30–90 | extend if body_C/body_L spread > 4°F at the deadline |
| `RIGHT_PROACTIVE_HOT_F` | 84 | 82–86 | lower only if right_proxy minutes_score≥0.5 > 70 after 14 nights |
| `RIGHT_PROACTIVE_HOT_STREAK_MIN` | 10 | 5–15 | lower with caution; trades latency for over-cooling risk |
| `MOVEMENT_KPROXY_LEFT` | 1.0 | 0.5–2.0 | raise only after movement-density baseline normalized per night |
| `MOVEMENT_KPROXY_RIGHT` | 2.0 | 1.0–3.0 | hold; primary right-side learning signal |
| `RESIDUAL_LCB_K` | 1.0 | 0.5–2.0 | raise to 2.0 if Δ≠0 frequency exceeds 30 % of ticks |
| `RESIDUAL_CAP_LADDER` | (5,1)(15,2)(∞,3) | — | hold |
| `MAX_DIVERGENCE_STEPS` per regime | normal_cool 3, cold_room 4, wake_cool 2, others 1 | 1–5 | tighten on regime if divergence-guard fires > 5 / night |
| `MIN_CHANGE_INTERVAL_SEC` | 1800 (=30 min) | 1200–3600 | hold; matches v5.2 |
| `KILL_SWITCH_RIGHT_CHANGES/WINDOW` | 2 / 600 | — | new for right zone |
| `DEAD_MAN_THRESHOLD_SEC` | 600 | 300–900 | hold |
| `RAIL_RELEASE_COOLDOWN_MIN` | 10 | 5–20 | extend if rail re-engages within 30 min twice |

**Tuning protocol.** All parameter changes go through `tools/v6_eval.py`
walk-forward replay and case-A/B/C check before deploy. No live hot-fix
of parameters during a sleeping night (gated by `_is_sleeping()`).

---

## 9. Counterfactuals on cases A/B/C (numbers from `v6_eval.py` baseline run)

`v5.2 baseline (./v6_eval.py --policy baseline)`: overall MAE 1.770,
left 1.778, right 1.714; right_proxy mean 0.217, p90 0.337,
minutes≥0.5 = 115; **A/B/C all FAIL**.

Predicted v6_synth trajectories (hand-traced — full simulator pending):

### Case A — LEFT 01:37–02:05 cold-room cluster

| t | room | body_L | regime fired | base | body_fb | room_fb | Δ | v6 | v5.2 | user override |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 01:35 | 67.4 | 76.1 | cold_room_comp | -7 | +5 | +2 | 0 | **-3 → cap -3** | -10 | — |
| 01:40 | 67.3 | 75.8 | cold_room_comp | -7 | +5 | +2 | 0 | **-3** | -10 | -7 |
| 01:50 | 67.2 | 75.6 | override_respect | floor=-7 | — | — | 0 | **-7** then warmer floor | -7 | -8 → -6 |
| 02:05 | 67.0 | 75.7 | override_respect | floor=-3 | — | — | 0 | **-3** | -3 (override) | -3 |

Predicted **suppression of 2-3 of the 4-tap override cluster.** Case A
pass criteria (`median_setting ≥ -5 over 01:37–02:05`): **PASS** (median
≈ -3 to -5 depending on override timing).

### Case B — RIGHT 03:25 under-cooled override (-4 → -5)

| t | room | b_L | b_C | post_bedjet | regime | base | body_fb | proxy | v6 | v5.2 |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|
| 03:10 | 68.3 | 73.4 | 76.6 | 245 min | normal_cool | -5 | +1 (cold) | -1 (md elevated) | **-5** | -3 |
| 03:25 | 68.3 | 73.1 | 77.0 | 260 min | normal_cool | -5 | +1 | -2 (md > 2× p75 + body_hot=77<82 → suppress half) | **-5** | -3 |

(Asymmetric cold-cap: right cold-room-comp suppressed by body_trend AND
body_hot < 82, so we don't warm-bias against her; movement-density
proxy handles the rest.) Case B pass (`setting ≤ -5 ≥ 15 min`):
**PASS** — v6 sits at -5 from 03:10. v5.2 was at -3. **MAE 0 vs v5.2's
2** on this override.

### Case C — 04:27 cold + 06:56 warm

| t | regime | base | body_fb | room_fb | Δ | v6 | v5.2 | user wanted |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 04:25 | cold_room_comp (room=68.5, body_L=76, trend≈0) | -5 (rem) | +5 cap | +1 | 0 | **-3 (cap)** | -2 | -2 |
| 06:50 | wake_cool (cycle≥5, body_hot=84) | -6 | 0 (Kp_hot=0 left) | +1 | 0 | **-5** then -6 next tick | -6 | -4 |

Case C pass (`median 04:15–04:40 ≥ -3` AND `median 06:40–07:10 ≤ -4`):
**PASS** on the cold half. Marginal on the warm half: v6 sits at -5/-6
where user wanted -4; same as v5.2 (MAE 1-2). The wake-cool rule
explicitly chose **not** to warm-bias here — the user's "slightly warm"
override is interpreted as cooler-please, which `wake_cool` already
encodes by NOT raising the setting.

**Right comfort proxy projection** (averaged across 8 silent-overheat
nights from red-comfort §1): proactive-cool fires at ~04:30 on each
night, dropping 86°F-minutes by an estimated 35-50 % (from 17 avg to
8-11 avg per night). This is the headline right-zone win. Conservative
target: `minutes_score ≥ 0.5` from 115 → **70**.

---

## 10. Top 3 risks (red teams) + mitigations adopted

| # | Risk (source) | Mitigation in v6_synth |
|---|---|---|
| 1 | **Cold-room warm-bias overshoots when body is sweat-cooled** (red-comfort §3.1) — cluster-A regime can warm during incipient hot-flash | `cold_room_comp` requires `body_trend_15m < +0.20 °F/15min` AND `room ∈ [65, 69)`. Cap at -3 (left). Below 65°F room: defer to v5.2 entirely (envelope guard, red-comfort §6.3 #3) |
| 2 | **Right-zone rail mutex is not implemented; both apps write same entity** (red-safety §1, recon-deployed §7.3) — accidental rail release within seconds | NEW `input_boolean.snug_right_rail_engaged` (rail writes on engage/release); v6 SafetyActuator yields unconditionally if `on`; 10-min cooldown after release; right-zone CAS lease via `input_text.snug_writer_owner_right` |
| 3 | **Override-absence trap on right: silent overheats continue under any "respect history" policy** (red-comfort §1, opt-learned §3.3) — 8/25 nights had >10 min body_L>86 with zero overrides | (a) hot-side max-channel rule for body input on right; (b) proactive-cool at body_hot ≥ 84 sustained 10 min; (c) movement-density gating with body-conditioned sign; (d) all-night cold-override ceiling on right; (e) residual head DISABLED on right until ≥10 nights of own data; (f) composite proxy (§5) is the gating metric, not override MAE |

Honorable mentions actively addressed: BedJet residual outlasts 30 min
(60-min `body_center` exclusion + `climate.bedjet_shar` listening);
sleep-stage feed lag (regime requires stage AND body_trend);
3-level mode silently active (forced OFF + L1/L2/L3 listeners);
`_setting_for_stage` clobber bug (deleted — base = phase target only);
`hot_safety` warm-override erosion H5 (`hot_safety` anchored to
`max(base, override_floor)`).

---

## 11. Deployment plan

### 11.1 Order

LEFT first (54 overrides + RC already off + user is the data source).
RIGHT only after LEFT canary completes AND ≥10 right-zone controlled
nights are in PG (which won't happen without enabling right shadow first
— see below).

### 11.2 Phases (calendar nights)

| Phase | Nights | What runs | Writes? | Gate to advance |
|---|---|---|---|---|
| **Shadow-A** | 1–7 | v5.2 live both zones; v6 computes regime + target every tick, logs to `controller_readings` with `controller_version='v6_shadow'`; reuses existing PG schema | NO (v6 only logs proposals) | (a) ≥6 nights with ≥80% tick coverage; (b) zero divergence-guard fallbacks on left in `normal_cool`; (c) regime distribution sanity (initial_cool ≈ 5%, normal_cool ≥ 60%) |
| **Canary-L** | 8–14 | v6 live LEFT (residual still disabled); RIGHT v5.2 + shadow | YES on left (cold_room_comp, wake_cool, normal_cool only — initial_cool deferred to v5.2 path so we don't reroute the user's mandated -10) | (a) Case A pass on tools/v6_eval.py replay over canary nights; (b) left override count ≤ 5/night; (c) no manual_hold trips; (d) user no-worse subjective report on day 14 |
| **Canary-L+residual** | 15–21 | enable left residual head (Δ ∈ ±1 only) | YES | (a) MAE walk-forward on canary-L nights ≤ 1.50; (b) bootstrap CI for `MAE(v6) - MAE(v5.2) < 0` (one-sided 90%); (c) zero spurious-override predictions |
| **Shadow-R** | 22–28 | start logging v6 right proposals continuously alongside v5.2 (overlapping with canary-L+residual) | NO on right | (a) right shadow regime distribution sanity; (b) `bedjet_active` correctly classified using `climate.bedjet_shar` ≥ 90% of windows; (c) proxy minutes≥0.5 prediction within ±25% of observed |
| **Canary-R** | 29–35 | v6 live RIGHT (residual disabled; proactive-cool ON) | YES on right | (a) right_proxy minutes≥0.5 ≤ 70 (vs v5.2's 115) — **headline gate**; (b) zero rail mutex violations (no v6 write within 60 s of rail engage/release); (c) zero new minutes above 86 °F vs v5.2; (d) ≤ 1 manual cold-override per night |
| **Steady-state** | 36+ | both zones live with residual capped ±1 (right) / ±2 (left); weekly trunk refit; nightly per-zone refit | YES | promote LCB cap as data accrues per ladder |

### 11.3 Rollback criteria (NUMERICAL — not vibes)

Any one of these fires → automatic disable of v6 on that zone (sets
`input_boolean.snug_v6_<zone>_live = off`) + notify:

1. **Override count regression:** > 6 left overrides on any single night
   AFTER night 14, OR > 2 right overrides on any single night AFTER night 29.
2. **Right body_L > 86 °F minutes:** > 30 min on any single night after
   night 29, OR > 20 min average over any 3-night window. (v5.2 baseline:
   8/25 nights had 10–23 min — we must not increase the average.)
3. **Manual_hold trip:** any night where left zone enters manual_hold
   AFTER canary-L day 3 (the kill switch should be silent in steady
   state).
4. **Divergence-guard storm:** > 5 divergence-guard activations per
   night per zone in `normal_cool`.
5. **Dead-man fires twice in 7 nights** → permanent rollback pending
   investigation.
6. **Any positive write attempt** (target > 0 reaches the actuator, even
   blocked) → instant rollback.
7. **right_comfort_proxy.minutes_score≥0.5 worse than v5.2 baseline (115)
   for 3 consecutive nights** after canary-R day 3.
8. **User self-reports a worse night** during canary phases (subjective
   override of metric thresholds).

### 11.4 Logging required from night 1

- Every tick (both zones, regardless of write): `regime`, `target`,
  `v52_advice`, `divergence_steps`, `delta_residual`, `bedjet_active`,
  `body_hot`, `body_skin`, `room_f`, `movement_density_15m`,
  `post_bedjet_min`, `mins_since_onset`, `l_active.dial`,
  `three_level_off`, `right_rail_engaged`, all source sensors raw.
- All blocked actions (rate, freeze, mutex yield, lease busy) get a row
  with `action='blocked'` and reason — close the M11 right-zone PG gap.
- Nightly summary: regime histogram, override count, right_proxy trio,
  rail engagements, fallback events.

---

## 12. Data we still need + how to collect it

| Need | How | When | Blocking |
|---|---|---|---|
| First **full-telemetry night** with PID/run_progress/heater_raw (recorder fix landed today) | passive — already collecting tonight | tonight | Shadow-A analysis on night 2 |
| `climate.bedjet_shar` HA history persistence | add `climate.bedjet_shar` and any `bedjet*` entities to recorder include list (today); add typed columns to PG via new logger in v6 | tonight | bedjet_warm regime accuracy after night 7 |
| Per-minute high-resolution **bed-pressure movement aggregates** to PG | new AppDaemon writer subscribes to `sensor.bed_presence_2bcab8_*_pressure` state-changes, emits `controller_pressure_movement` PG table (zone, ts, abs_delta_sum_60s, max_delta_60s) | this week | movement_density_15m used by proxy_term and right_proxy on night 1 — **has fallback to PG 5-min snapshot if writer offline** |
| Promote `actual_blower_pct` from `notes` to a typed PG column | one-line ALTER + logger change in v5/v6 | this week | non-blocking (already parsed) |
| Apple-stage source dedup + freshness column | new view `sleep_segments_active(ts, source_priority, stage, age_min)` | week 2 | wake_cool reliability |
| ≥ 1 **deliberate cold-room A/B night** (room ≤ 65 °F at onset) with v5.2 only, then v6 the next | thermostat 64 °F overnight; user permission required | week 2-3 | cold_room_comp tuning + envelope guard validation |
| ≥ 3 **right-zone wake-overheat** controlled nights with v6 vs v5.2 paired | natural occurrence — collect 14-night window during canary-R | weeks 4-5 | right Kp_hot, RIGHT_PROACTIVE_HOT_F tuning |
| 10+ right-zone live-controller nights | only available after Canary-R starts | weeks 5-7 | residual head enable on right |
| Cap-vs-`L_active` table from firmware (Stage-1 cap is fit on 6 points) | direct query of `temperature_setpoint` after run_progress recording lands; `tools/firmware_cap_fit.py` (new) | week 2 | divergence-guard sanity tightening |

---

## 13. Honest comparison vs v5.2 on cases A/B/C

| Case | v5.2 actual | v6_synth predicted | Advantage |
|---|---|---|---|
| **A** (LEFT cold-cluster) | held -10, user dragged to -3 over 4 overrides; v6_eval **FAIL** (median setting ≈ -10) | -3 to -5 from 01:37 onward (cold_room_comp, body_trend ≈ 0); predicted suppression of 2-3 of 4 overrides; **PASS** | **MAE 1.0 vs v5.2's 2.0** on the 4-event cluster (50% reduction in this regime) |
| **B** (RIGHT under-cooled) | -3 (cold_room_comp would have warmed it further but right cold-gain=0); user overrode to -5; **FAIL** | -5 from 03:10 onward via movement_density proxy; **PASS** | **MAE 0 vs v5.2's 2** on this single override; one new mechanism (proxy_term) on the zone with no learnable signal |
| **C** (cold mid-night, warm-AM) | 04:27 v5.2 ≈ -2 (close); 06:56 v5.2 ≈ -6 (over-cool by 2); cold half **FAIL**, warm half marginal | 04:25 v6 -3 (cap), 06:50 v6 -5 to -6; **PASS** on cold half, marginal-tied on warm half | **MAE ≈ 1 vs v5.2's 1-2** combined; the cold-half improvement comes from the body-trend-guarded cold_room_comp; the warm-half is a wash (both v5.2 and v6 are ~2 cooler than the user wanted, but neither is positively wrong) |

**What v6_synth does NOT improve:**
- Override MAE on right zone is structurally bounded above the MDE
  (n=7, MDE=0.8). We do not claim improvement; we claim proxy improvement.
- Aggregate left override MAE: we project 1.50–1.55 vs v5.2's 1.78 — that
  is *below* the val-eval MDE of 0.52 on left. We will **not** claim a
  statistically significant override-MAE win on the existing corpus
  (red-comfort §6.4 critique applies to us too); we claim a **regime-MAE
  win on cold-room rows** (n≈6) and a **case-test sweep**. The aggregate
  MAE proof requires post-deploy data.

---

## 14. Open questions for the user (decision-ready)

1. **Right-zone live writes — are you OK with the 30+ night runway?**
   We will not enable RIGHT live writes until canary-L is clean (~21
   nights from start). If you'd prefer faster right-zone improvement,
   we can ship a "rail-only" v6 (right zone gets nothing but the
   improved rail mutex + 60-min BedJet residual + max-channel safety
   tweak) at night 8 with no learning.

2. **Cold-room A/B night** — are you willing to lower the bedroom
   thermostat to 64 °F for one night during week 2, with v5.2 the night
   before and v6 the night of? This is the only way to validate the
   cold-room envelope in time for canary tuning.

3. **BedJet logging** — please confirm the entity name
   (`climate.bedjet_shar` or different) and whether you usually run it
   in `heat` or `turbo` mode, and roughly when. We can detect from
   state but knowing your protocol shrinks the inference window.

4. **Proactive-cool threshold (right) — 84 °F** is the recommendation.
   You characterized the wife as "runs hot" — at what `body_left`
   reading do you (qualitatively) think she is uncomfortable? We will
   anchor `RIGHT_PROACTIVE_HOT_F` to your answer ±2 °F.

5. **All-night right-zone cold-override ceiling** — we propose that any
   right-zone `cooler` override locks the controller from writing
   warmer than that value for the rest of the night (no 60-min
   release). Is that acceptable, or would you rather she be able to
   override warmer later via a new manual change only?

6. **Wake_cool warm-bias removal** — opt-hybrid had `+2/+3` warm bias
   in cycle 5+. We removed it (and made it cool-bias on right when
   body_hot > 84). Any subjective evidence either way from past nights?

7. **Residual head model** — Bayesian Ridge + tiny GP quorum is the
   recommendation (opt-learned §1). Any objection to ~1 MB of model
   state on the AppDaemon container, refit nightly?

8. **3-level mode forcibly OFF** — we will write the switch off at every
   `_on_sleep_mode`. Confirm there is no scenario you want 3-level mode
   ON during sleep (it's been ON historically per recon-deployed §6).

---

## Appendix A — Files to add (if approved)

```
appdaemon/sleep_controller_v6.py           # new (subclasses or wraps v5)
appdaemon/safety_actuator.py               # new (the §7 wrapper)
appdaemon/right_overheat_safety.py         # MOD: write input_boolean.snug_right_rail_engaged
appdaemon/v6_pressure_logger.py            # new (movement_density to PG)
ml/v6/regime.py                            # new (classifier + helpers)
ml/v6/residual_head.py                     # new (BayesianRidge + GP quorum + LCB)
ml/v6/firmware_plant.py                    # new (Stage-1+2 forward predictor for sanity)
ml/v6/right_comfort_proxy.py               # new (§5 implementation; reused by v6_eval)
tools/v6_eval.py                           # MOD: register v6_synth Policy
tools/firmware_cap_fit.py                  # new (Stage-1 cap fit from run_progress)
sql/v6_schema.sql                          # ALTER controller_readings ADD COLUMN regime, residual, divergence_steps; CREATE TABLE controller_pressure_movement
```

HA helpers (input_boolean / input_text):
```
input_boolean.snug_v6_enabled              # default: off (master arm)
input_boolean.snug_v6_left_live            # default: off
input_boolean.snug_v6_right_live           # default: off
input_boolean.snug_v6_residual_enabled     # default: off
input_boolean.snug_right_rail_engaged      # written by right_overheat_safety
input_text.snug_writer_owner_left          # CAS lease
input_text.snug_writer_owner_right         # CAS lease
```

End of recommendation. — synth (v6 fleet, agent 10/10).
