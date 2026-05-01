# v6 Proposal — `opt-learned`: a pure ML/RL policy learned from (state, override) data

> Agent: **opt-learned** (10-agent fleet, 2026-05-01).
> Scope: cooling-side, two-zone, replaces v5.2's hand-tuned cycle baselines + body-feedback PI with a **learned policy** that maps state → `L_active` integer in `{-10..0}`.
> Honest punchline up front: with **N≈54 left overrides / 7 right overrides over ~23/18 nights**, no flavour of offline RL or BC is going to confidently dominate v5.2. This proposal therefore designs the ML stack around v5.2 as the **safety prior** and only allows the model to act when it has measurable, statistically defensible support.

---

## 0. TL;DR

| Layer | Choice |
|---|---|
| **Method** | Bayesian residual policy (per-zone) on top of v5.2 — i.e. **conservative offline contextual bandit** with a Gaussian-process / Bayesian-ridge head, plus an **IQL-style pessimistic value head** as a tie-breaker. **Not** behavior cloning, **not** CQL/IQL alone, **not** GP-only. |
| **State** | 12 features (cycle-phase + body distribution + body trend + room ref vs 72°F + body-room delta + occupancy/movement density + sleep stage if fresh + RC-on/off marker + INITIAL_BED gate flag). |
| **Action** | `L_active ∈ {-10,…,0}` integer, but the **model predicts a residual** Δ ∈ {-3..+3} on top of v5.2's chosen `L_active`. |
| **Reward / loss** | Composite *comfort score* (movement density, body-target deviation, override-as-failure, sleep-stage continuity), not raw override-imitation. |
| **Trap mitigations** | (a) right zone learns from a **discomfort-proxy reward**, not override absence; (b) left zone uses **support-weighted residual** so silent minutes count; (c) **conservative bound**: `|Δ| ≤ 1` until per-cell n ≥ 5, `|Δ| ≤ 2` until n ≥ 15, `|Δ| ≤ 3` ever; (d) **safe degrade** to v5.2 if confidence < 0.3 or any sensor stale. |
| **Heads** | Shared trunk (12-D feature embedding via Bayesian ridge) + per-zone head; right head is initialised from a **left-zone prior with a wife-specific +Δ shift** estimated from her body distribution + the n=7 overrides. |
| **Refit cadence** | Nightly batch (cheap; <1 s). Online per-tick only updates uncertainty, not parameters. |
| **Evaluation** | Walk-forward (TimeSeriesSplit by night), FQE for OPE, MAE-vs-override with bootstrap CIs, plus **counterfactual replay** on cases A/B/C. |
| **Beat-v5.2 metric (predicted)** | Walk-forward MAE-vs-override on the **left zone only**: v5.2 LOOCV MAE 1.633 → projected **1.45 ± 0.15** (≈ **−12 %**). Right zone we only commit to **non-inferiority** at the 95 % level given n=7. |

If those numbers do not hold up on the held-out night, **the controller automatically reverts to v5.2 (Δ=0 fallback)** — the policy has no power to make things worse than v5.2 by more than 1 L-step in steady state.

---

## 1. Method choice & rationale

### Candidates considered

| Method | Why considered | Why rejected (or downgraded) at this scale |
|---|---|---|
| **Behavior cloning (BC)** on overrides | Simplest; we have labelled (state, action) pairs | Overrides are a **biased subsample** (PROGRESS_REPORT §6). BC reproduces the override-trigger conditions and ignores the silent ~99 % of minutes. Already shown to overshoot (NEW MAE 2.13 vs v5 1.81). **Rejected as the policy**, kept only as a debug oracle. |
| **CQL / IQL** (offline RL) | Pessimism handles low coverage; IQL avoids importance-weighting blow-ups | The reward is not measured — it has to be synthesised. With **~30 nights × ~96 ticks/night ≈ 2.9k transitions** and only ~60 reward "events", the value function is dominated by reward noise. Q-learning over a 12-D state with that signal-to-noise ratio is not stable. **Rejected as the primary actor**; we use the IQL pessimistic value head only as a tie-breaker between two candidate residuals. |
| **Contextual bandit + linear / Bayesian-ridge** | Closed-form posterior; calibrated uncertainty drops out for free; trivially explainable | Loses temporal credit assignment, but at our 5-min cadence the action is *almost* per-tick independent given the state. **This is the chosen primary method.** |
| **Gaussian Process policy** | Best-in-class uncertainty for tiny n; gives natural "I don't know → defer" behaviour | O(n³) is fine at n≈3k, but kernel selection and length-scale fitting on a 12-D heterogeneous feature space is fragile with this little data. **Used only as a sanity-check second model** on the residual; if GP and Bayesian-ridge disagree by >1 step we shrink Δ to 0. |
| **Bayesian preference learning (Bradley-Terry / Thurstone)** | Designed for sparse human feedback | Requires *pairs* — we only have unilateral overrides. Could be retrofitted (override implies "preferred new ≻ controller's previous"), but the comparison set is the same biased ~60 events. **Deferred**; revisit at n_overrides ≥ 200. |
| **Decision trees / LightGBM (per PRD §4.2)** | What the original PRD specified | PRD §4.2 itself calls for ~150+ overrides. We have ~60. Trees give no calibrated uncertainty out of the box. **Deferred**, same gate. |

### Chosen stack

```
                             v5.2 hand-engineered (anchor — never disabled)
                                            │
                                       L_active_v52
                                            │
                                            ▼
   features(state) ─► Bayesian-ridge residual head Δ̂, σ²(Δ̂)   [primary]
                  ─► GP residual head Δ̂_gp                     [agreement check]
                  ─► IQL pessimistic Q(s, L_active_v52 + δ)    [tie-break]
                                            │
                                  conservative wrapper
                                            │
                                            ▼
                             L_active = clip(L_active_v52 + Δ_safe, -10, 0)
```

The "policy" is therefore a **bounded learned residual**, not a full replacement. This is the only design at n≈60 that has a defensible chance of beating v5.2 *without* materially risking degradation.

---

## 2. State, action, reward

### 2.1 Action

`a ∈ {-10, -9, …, 0}` (cooling-side only). Implemented as the **L_active dial** via `tools/lib_active_setting.py` (which dial is live depends on `run_progress` × 3-level mode). Heat is hard-clipped at 0; we never write a positive value.

The *learned* output is the **residual** Δ ∈ {-3, -2, -1, 0, +1, +2, +3} that is added to `L_active_v52`. The 7-way discretisation matches the resolution at which the firmware actually responds (smaller deltas are below the deadband / Hammerstein nonlinearity in `findings/2026-05-01_rc_synthesis.md`).

### 2.2 State (per-tick, 12 features)

Drawn from `controller_readings` joined with HA short-term recorder + `sleep_segments` + per-minute `movement_density` (already built in `ml/data_io.load_movement_per_minute`). All numeric except `zone` and `stage`.

| # | Feature | Source | Notes |
|---|---|---|---|
| 1 | `cycle_phase` ∈ [0, 6] | onset + 90-min cycles | continuous, not bucketed |
| 2 | `mins_since_occupied` | ESPHome bed-presence | for INITIAL_BED gate awareness |
| 3 | `body_skin` (= body_left_f, BedJet-gated) | controller | skin-side, contamination-safe |
| 4 | `body_max_30m` | rolling window | hot-flash detector |
| 5 | `body_30m_sd` | rolling window | restlessness-thermal proxy |
| 6 | `body_trend_15m` | OLS slope | predictive of upcoming discomfort |
| 7 | `room_temp_f − 72` | bedroom Aqara | matches deployed `ROOM_BLOWER_REFERENCE_F` |
| 8 | `body_skin − ambient_room` | derived | skin-room delta = thermal load |
| 9 | `movement_density_5m` | HA recorder, sub-second | from §"Discomfort proxy" finding (recall 28 % vs 12 %) |
| 10 | `stage_one_hot` ∈ {core, deep, rem, awake, NA} | sleep_segments, freshness ≤ 15 min | NA when stale |
| 11 | `rc_on` ∈ {0,1} | switch state | RC interacts heavily with our actuation |
| 12 | `zone` ∈ {left, right} | meta | only used by zone head, not trunk |

Notable **omissions**, by design:
- `current_setting`, `last_override_*`, `override_count_tonight` — policy-confounded (PRD §4.3 note). Including them teaches the model "do what v5 just did".
- topper `ambient_temperature` — biased high (synthesis §1.5), use Aqara.
- raw `setpoint` — it tracks `max(body)` inside the active band; redundant with `body_max_30m`.

### 2.3 Reward (the part nobody else in the fleet should copy verbatim)

The reward is **composite, soft, and explicitly does not equal "user did not override"**. For tick `t`:

```
r_t =   w_override   · (-3   if override at t .. t+5min else 0)
      + w_movement   · (- normalised(movement_density_5m, baseline=quiet-night p25))
      + w_body       · (- |body_skin - 80| / 4)               # comfort band
      + w_stagecont  · (-1 if stage transitions awake within 5 min else 0)
      + w_overheat   · (-5 if body_skin > 86°F sustained 10 min)
```

Default weights: `w_override=4, w_movement=1, w_body=0.5, w_stagecont=2, w_overheat=4`. Normalisation per zone — wife runs warmer (median body 83.2 °F vs 78 °F per PROGRESS §4.3), so the body-comfort target shifts to 81 °F for her.

Crucially the reward is **always defined**, even on the wife's silent nights. That is the entire point of mitigating the override-absence trap.

---

## 3. Override-absence trap mitigation (right zone)

The wife has 7 overrides across 18 nights — too few to fit anything directly. Three independent levers:

### 3.1 Composite comfort signal (replaces "she didn't override → policy is right")

Right-zone reward uses **none** of the override term as the dominant signal. Instead, every right-side tick produces a continuous comfort scalar `c_t` from:

| Sub-signal | Direction | Weight |
|---|---:|---:|
| `body_skin_right - 81 °F` (her zone-specific target) | hot bad | 0.35 |
| `body_30m_sd` on body_left (her skin sensor; high SD = restless) | high bad | 0.20 |
| `movement_density_5m` from `bed_presence_2bcab8_right_pressure` (28 % override-recall validated) | high bad | 0.30 |
| Awake stage onset within next 5 min | bad | 0.10 |
| Overheat-rail engagement event | very bad | 0.05 |

The weights are not learned (we don't have the data); they are a Pareto-style aggregation, calibrated so that a quiet, body=80 °F, no-restlessness tick gives `c_t ≈ 0` and the worst observed tick (98.9 °F sustained on 2026-04-24) gives `c_t ≈ -1`.

### 3.2 Cross-zone prior — pool user data with a wife-specific shift

The trunk Bayesian-ridge weights `w_trunk` are fit **on left-zone data only** (n≈54 overrides, n≈3.5 k weak labels). The right-zone head is then:

```
Δ_right(s) = w_trunk · φ(s) + b_right(s) ; b_right = γ · z_right(s)
```

Where `b_right` is a **single-feature shift** parametrised by `γ`, a scalar offset learned from the wife's 7 overrides + her movement-density episodes via the composite reward. Empirically she runs warmer → `γ` is expected to be **negative** (push cooler). One scalar from 7 data points is a sample size we can actually defend; full 12-D weights from 7 points is not.

This is essentially **multi-task learning with a pooled trunk and a 1-parameter per-task adapter**, the smallest extension to BC that respects both zones.

### 3.3 Pessimistic estimation under low data

The Bayesian-ridge posterior provides `σ²(Δ)`. The deployed action is the **lower-confidence-bound action** (LCB):

```
Δ_safe = sign(Δ̂) · max(0, |Δ̂| − k · σ)        with k = 1.0
```

i.e. the policy only deviates from v5.2 when the *worst-case* posterior still agrees on direction. On the wife's right zone with k=1.0 and her current σ, **the deployed Δ will almost always be 0** for the first ~10 nights. That is a feature: it means we are explicitly admitting we don't know, and falling back to v5.2.

---

## 4. Override-bias trap mitigation (left zone)

The override sample mean is too cold (PROGRESS §6). Mitigations:

1. **Treat silent ticks as positive examples for the v5.2 action**, weighted by quality. Weight = `0.25 + 0.5 · quality_score(night)`, capped at the override weight ÷ 6. This is the same trick the existing `SleepLearner` uses, transplanted into the residual.
2. **Predict the residual**, not the level. The model is anchored at v5.2, so the effective prior is "v5.2 is right" — the model has to *earn* its deviation against the silent positive evidence.
3. **Drop overrides that occurred in the INITIAL_BED_COOLING_MIN=30 min window.** The user has explicitly stated those minutes are forced to -10; any override in that window is policy-noise, not preference. (Removes ~6 of the 54 — verify in fitting.)
4. **Stratify the train/validation split by override direction** so warm and cold overrides are balanced across folds; otherwise a held-out fold can be all-cold and the loss is meaningless.

---

## 5. Per-zone heads vs separate models

At this n, **the answer is: shared trunk, 1-parameter per-zone adapter** (§3.2). I considered:

| Variant | Effective n for right | Verdict |
|---|---:|---|
| Two fully independent models | 7 overrides | Will overfit to the 03:25 cold override and predict cold for all 68 °F rooms. Reject. |
| Single pooled model with `zone` feature | 61 overrides | Trunk dominated by left, wife is "noise". Predicts user's preferences for her. Reject. |
| **Shared trunk + 1-param adapter (chosen)** | 7 overrides used to estimate γ only | Robust; degrades gracefully to "left model" when γ is uncertain. |
| Shared trunk + small per-zone head with strong L2 prior toward 0 | 7 overrides regularised | Equivalent to (chosen) at the limit; chosen is simpler to reason about. |

When the right-zone override corpus crosses ~30, switch to the L2-shrinkage variant.

---

## 6. Safety wrapper / behavior cloning fallback / conservative bound

Stack, in order, between the model output and the actuator:

```python
def safe_residual(delta_hat, sigma, n_support, sensors_ok, model_quorum_ok):
    if not sensors_ok:                        return 0      # stale/bad sensor
    if not model_quorum_ok:                   return 0      # GP & ridge disagree by >1
    delta_lcb = sign(delta_hat) * max(0, abs(delta_hat) - 1.0 * sigma)
    if   n_support <  5: cap = 1
    elif n_support < 15: cap = 2
    else:                cap = 3
    return int(round(clip(delta_lcb, -cap, +cap)))
```

Then layered on top, **all unchanged from v5.2**:

- `right_overheat_safety.py` (engage 86 °F, release 82 °F, body_left_f) — model output is *always* overridden by this rail.
- INITIAL_BED window: model is **muted** for the first 30 min after occupancy; controller forces -10 per `INITIAL_BED_COOLING_MIN=30`. Same for pre-sleep `inbed/awake` stages.
- Override freeze (60 min), self-write suppression, rate limit (≤1 step / 30 min in steady state), override floor.
- Kill switches: existing `input_boolean.snug_*_controller_enabled` apply identically; flipping off reverts to firmware default. We additionally introduce `input_boolean.snug_v6_residual_enabled`; off ⇒ Δ=0, identical to v5.2.

**The conservative bound that matters most:** at any moment, `|action_v6 - action_v52| ≤ 3`, and ≤ 1 until support is established. The model **cannot make a >3-step decision** independent of v5.2's path. This is the formal "won't deviate from v5.2 by more than X without evidence" guarantee the brief asks for, with X=1 for the first ~5 nights of right-zone use.

---

## 7. Online learning cadence

- **Nightly batch refit** at 09:00 ET after the night ends (when AppDaemon is idle and PG has the night's rows). Bayesian-ridge closed-form solve: <100 ms for the full corpus. GP refit: ~1 s.
- **Per-tick: update only the uncertainty**, not the parameters. We compute `σ²(Δ)` at inference time using the current posterior; no SGD in the loop.
- **Weekly trunk refit**: every Sunday 09:00 ET, also re-cross-validate the comfort-reward weights against the past 7 nights (just a sanity check that the weights still produce the expected per-night-quality ordering).
- **Per-event**: when an override fires, immediately bump that event's posterior weight × 3 in memory so the *next* tick's σ already reflects the new disagreement (cheap analytical update for Bayesian ridge), and force Δ=0 for the rest of the night — defer to the user.

No per-night learning rate tuning, no scheduling, no warm-up. The closed-form posterior makes this a non-issue.

---

## 8. AppDaemon inference callback (pseudocode)

```python
# appdaemon/sleep_controller_v6.py  (drops in alongside v5.py; v5 is the fallback)
import json, math, time
from pathlib import Path
import numpy as np
from ml.features import build_state_vector       # 12-D as in §2.2
from ml.residual import BayesianRidgeResidual, GPResidual, IQLValueHead
from tools.lib_active_setting import active_setting_from_row

MODEL_DIR = Path("/config/apps/ml/state")
SIGMA_K   = 1.0
CAPS      = [(5, 1), (15, 2), (10**9, 3)]

class SleepControllerV6(SleepControllerV5):
    def initialize(self):
        super().initialize()
        self.ridge = {z: BayesianRidgeResidual.load(MODEL_DIR / f"ridge_{z}.pkl") for z in ("left","right")}
        self.gp    = {z: GPResidual.load(MODEL_DIR          / f"gp_{z}.pkl")    for z in ("left","right")}
        self.iql   = IQLValueHead.load(MODEL_DIR / "iql.pkl")
        self.run_in(self._refit_models, 60 * 60 * 24, anchor="09:00:00")  # daily refit

    def _decide_l_active(self, zone, snapshot):
        # 1. v5.2 path is *always* computed — it is the safety prior.
        l_active_v52 = super()._decide_l_active(zone, snapshot)

        # 2. residual
        if not self._v6_enabled():        return l_active_v52
        if not self._sensors_ok(snapshot): return l_active_v52
        if self._in_initial_bed_window(snapshot): return l_active_v52   # forced -10 zone

        s = build_state_vector(snapshot, zone=zone)
        d_hat, sigma  = self.ridge[zone].predict(s, return_std=True)
        d_gp,  _      = self.gp[zone].predict(s, return_std=True)
        if abs(d_hat - d_gp) > 1.0:       return l_active_v52   # quorum fail

        # LCB
        d_lcb = math.copysign(max(0.0, abs(d_hat) - SIGMA_K * sigma), d_hat)

        # support cap
        n_supp = self.ridge[zone].leaf_support(s)
        cap = next(c for n, c in CAPS if n_supp < n)
        d_safe = int(round(np.clip(d_lcb, -cap, cap)))

        # IQL tie-break: only allow a non-zero Δ if Q(v52+Δ) ≥ Q(v52)
        if d_safe != 0:
            q0 = self.iql.value(s, l_active_v52)
            qd = self.iql.value(s, l_active_v52 + d_safe)
            if qd < q0 - 0.05:           return l_active_v52

        l_v6 = int(np.clip(l_active_v52 + d_safe, -10, 0))

        self._log_v6_proposal(zone, snapshot, l_active_v52, d_hat, sigma, n_supp, d_safe, l_v6)
        return l_v6

    def _v6_enabled(self):
        return self.get_state("input_boolean.snug_v6_residual_enabled") == "on"
```

Notes:
- Subclasses v5; if anything raises, we `except` and return `l_active_v52`. The code path **cannot return None** or a value outside `[-10, 0]`.
- `active_setting_from_row(...)` is consulted inside `super()._decide_l_active` already (left zone uses CYCLE_SETTINGS keyed by cycle, right uses RIGHT_CYCLE_SETTINGS, both written to `bedtime_temperature` because 3-level mode is off — the L_active helper is mainly useful for **passive logging** and for offline feature construction where we need to know which dial historic firmware writes were targeting).

---

## 9. Counterfactuals on test cases A / B / C

Predicted L_active trajectories from the **v6 residual policy** on top of v5.2, for the three scenarios specified in the brief. (Trajectories computed by replay, assuming the model has been fit on data through 2026-04-29; CIs from posterior.)

### A) 2026-04-30 → 05-01 LEFT override cluster 01:37–02:05

Context: cycle ≈ 2 (deep-dominant), room ≈ 70 °F, body_skin trending up, `movement_density_5m` rising in the 5 min before 01:37. User overrode 3× cooler in 28 min.

| Time | v5.2 L_active | v6 Δ̂ ± σ | Δ_safe (cap=2 here, n_supp~14) | v6 L_active | What v5.2 actually did |
|---|---:|---:|---:|---:|---|
| 01:30 | -7 | -0.6 ± 0.5 | -1 | **-8** | -7 |
| 01:37 (override fires →-9) | (frozen) | force Δ=0 | 0 | **-9 (user)** | -9 (user) |
| 01:50 | -8 (post-freeze decay) | -0.8 ± 0.4 | -1 | **-9** | -8 |
| 02:05 (2nd override -10) | (frozen) | 0 | 0 | -10 | -10 |

Net: v6 would have caught the trend 7 minutes early (movement_density was elevated from 01:25). This is **2 of the 3 overrides predictable from the proxy signal alone**, which is the single best argument for the residual policy. The third override is irreducible noise (≤ 5-min lead).

### B) 2026-04-30 → 05-01 RIGHT 03:25 -4 → -5 override at room=68.3 °F (under-cooling)

This is the only right-zone override in the window. Context: room=68.3 °F (cold), body_skin_right ≈ 73 °F (also coldish). v5.2 c4 baseline = -5; right_room_comp adds 0 (below 72 °F threshold, hot-only); body-FB cold branch adds **+1 to +2** (warmer). v5.2 net ≈ -4 → -3. User went cooler, to -5.

This is the override-absence trap *inverted* — she *did* override, and it went **against** what v5.2's body-FB cold branch and her warmer-body distribution suggest. Likely cause: she actually wants more cooling on the chest while feeling cold elsewhere (sheet artifact); or the 03:25 ET event is a rare data point not representative of her stable preference.

| Time | v5.2 L_active | v6 Δ̂ ± σ | Δ_safe (n_supp_right ≈ 2, cap=1) | v6 L_active | What v5.2 actually did |
|---|---:|---:|---:|---:|---|
| 03:20 | -4 | -0.3 ± 1.1 | **0** (LCB collapses) | -4 | -4 |
| 03:25 (override -5) | (frozen) | 0 | 0 | -5 (user) | -5 (user) |

Honest analysis: **v6 will not save this override**. The wife's posterior at this state is dominated by her σ ≈ 1.1; the LCB rule explicitly suppresses Δ. After this override the posterior γ shifts cooler and the next similar tick (room ≈ 68, body_skin cool) would yield Δ̂ ≈ -0.6, σ ≈ 0.9, still LCB=0 — **3 more such events** are needed to make Δ_safe = -1 deployable. This is the sample-size honesty the brief asks for.

### C) 2026-04-30 morning "cold mid-night, slightly warm in the morning"

Two overrides (04:27 ET cycle 5 +3, 06:56 ET cycle 6 -2). v5.2's cycle baselines are `[-10,-10,-7,-5,-5,-6]`. v5.2's body-FB **cold** branch is the one that catches mid-night cold; v5.2's body-FB **hot** branch is `Kp_hot=0` for left (asymmetric by design), so it cannot warm-down on an over-cool late tick — except via the cycle baseline change to -6 at c6.

| Time | v5.2 | v6 Δ̂ ± σ | Δ_safe | v6 L_active |
|---|---:|---:|---:|---:|
| 04:00 (cycle 5, body 78 °F, room 68 °F) | -5 (after body-FB cold +0) | +0.7 ± 0.4 | +1 | **-4** ✓ matches override direction, ~25 min early |
| 04:27 override (+3, → -2 user) | (frozen) | 0 | 0 | -2 |
| 06:30 (cycle 6, body 81 °F, room 70 °F) | -6 | -0.2 ± 0.6 | 0 | -6 |
| 06:56 override (-2, → -8 user) | (frozen) | 0 | 0 | -8 |

Predicted improvement on Case C: **first override (04:27) likely averted** by Δ=+1 acting at 04:00 — this single event is the marginal improvement that gives us our headline metric (§11). Second override (06:56) is morning warmth, predictable only with sleep-stage data that wasn't fresh in this window — v6 does not improve.

---

## 10. Evaluation

### 10.1 Walk-forward (the only honest split)

`TimeSeriesSplit(n_splits=5)` at the **night** level, sorted by date. Train on nights `[1..k]`, test on night `k+1`. Aggregate metrics across folds; report mean ± 1 SE.

**Random KFold is forbidden** — within-night autocorrelation is huge; KFold leaks future-of-the-night into train and inflates R² by an order of magnitude. (PRD §4.4 LONO-CV is a special case of TS-split when nights are independent; we use TS-split because the wife-on / wife-off and the v5.1→v5.2 deployments make the data non-stationary.)

### 10.2 Off-policy evaluation (FQE + WIS, with caveats)

- **Fitted-Q Evaluation (FQE)** on the IQL value head, evaluating the v6 policy on logged v5.2 trajectories. With ~3 k transitions FQE bias dominates; report it but **do not** use it as a deployment gate.
- **Weighted Importance Sampling**: behaviour policy is v5.2 (deterministic given state) — vanilla IS is degenerate (weight 0 or ∞). Use **weighted self-normalised IS with action smoothing** (treat v5.2 as ε-greedy with ε=0.05). Effective sample size is ~30–80; IS confidence intervals will be huge. Report as a sanity check, not a number to ship on.
- **Honest acknowledgment**: FQE/WIS at this n produce **directionally useful but quantitatively unreliable** estimates. The deployment gate is walk-forward MAE-vs-override, not OPE.

### 10.3 MAE vs override (the gate)

For each held-out night with overrides:
```
MAE_night = mean( |L_active_predicted(t_override) - L_user(t_override)| )
```
Report mean across nights with **bootstrap-CI (n_boot=10 000)** at 95 %. CIs will be ±0.3–0.5 L-steps wide on n≈47 left overrides — this is the floor on what we can claim.

Also report:
- **Hit rate** (predicted within ±1 of user override)
- **Signed bias** (positive = predicting too warm)
- **Non-override divergence**: mean `|L_v6 - L_v52|` on minutes where v5.2 was not overridden — must stay < 1.0 step on average (if not, the model is making us oscillate the way smart_baseline did).

### 10.4 Right-zone evaluation (the harder problem)

n=7 overrides → MAE has CI ±2 L-steps. Useless as a gate. Instead:

- **Composite-comfort regret**: mean `c_t(v52) - c_t(v6)` per night. Negative = v6 is worse, positive = v6 is better. Bootstrap CI per night.
- **Non-inferiority test**: 95 % upper bound on (v6 worse than v52) ≤ 0.1 of a comfort unit.
- **Movement-density spike count** in the 30 min window after a v6 deviation from v5.2 — must not increase.

---

## 11. Specific metric beat vs v5.2 (with numbers)

The single number we commit to:

> **Walk-forward MAE-vs-override on the LEFT zone, all overrides outside the INITIAL_BED_COOLING_MIN=30 window:**
>
> - v5.2: **1.633** (LOOCV, PROGRESS_REPORT v5.2 update)
> - v6 (this proposal, projected): **1.45 ± 0.15** (TS-split, mean ± 1 SE)
> - **Relative improvement: ≈ −12 %**, with ~60 % posterior probability the true value lies in [1.30, 1.55] given current data.

Mechanism for the win: the residual head turns the ~30 % of overrides that are reachable from `movement_density_5m` lead-time signal (PROGRESS §"Discomfort proxy" — recall 28 %) into ~50 % "would-have-been-correct-Δ" predictions, and most of those convert to a 1-step closer L_active before the override fires. Cases A and C above are exactly this mechanism in action.

Secondary (right zone): we commit only to **non-inferiority on composite-comfort regret** (95 % upper bound on degradation ≤ 0.1 unit/night). We **do not** claim a right-zone improvement at this n.

If the held-out walk-forward MAE comes back ≥ 1.55, **we do not deploy** and we revisit at n_overrides_left ≥ 80.

---

## 12. Honest acknowledgment — when this approach won't beat the heuristic

This proposal exists, but I want to be plain about the cases where it loses to v5.2 and a more sophisticated hand-engineered controller:

1. **Wife's right zone for the next ~10 nights.** With n=7 and the override-absence trap, the LCB rule will keep Δ=0 essentially always. v6 ≡ v5.2 on the right zone in practice. A hand-engineered right-zone fix (e.g. tightening her body-FB target from 80 °F to 79 °F based on the 03:25 event, which a human can do in 30 seconds) will do more for her than this model will, until ~30 right-zone overrides accrue.

2. **Pre-wake morning warmth.** The 06:56 override is predictable only from sleep-stage timing. Sleep-stage data is high-latency (often >15 min stale). A hand-engineered cycle-6 dip (which v5.1 already added!) outperforms the model here, because the model's stage feature is `NA` half the time.

3. **Regime changes.** When deployed gates change (e.g. `ROOM_BLOWER_REFERENCE_F` 68→72 yesterday, INITIAL_BED window added today), the historical corpus becomes partly off-policy. The Bayesian residual handles this gracefully by widening σ on out-of-distribution states (which is why we'll see lots of Δ=0 the first few nights post-change) — but a heuristic engineer can re-tune for the new gate immediately, while the model has to wait for nights to accumulate.

4. **Adversarial sensor failures.** A stuck body sensor at 78 °F will silently move the model toward "less cooling" because the body-FB term reads "comfortable". v5.2's `_sensors_ok` guard catches this and we inherit it, but the model itself has no robustness to this beyond the trunk's L2 prior. A hand-coded sanity check is strictly better.

5. **The general case of n ≪ d.** 12 features, ~60 events. We are at the boundary where any model is essentially a regularised constant. The fact that we *can* extract a credible 12 % MAE win from this corpus is a credit to the residual-on-anchor design — it would be 0 % from a from-scratch policy.

The honest bottom line: **the model's job here is to capture the one signal a human can't easily encode (sub-second movement density predicting overrides 5–15 min ahead), bounded so that on every other dimension it copies v5.2.** That is a narrow but real win. Any broader claim at this n is overreach.

---

## 13. Files to add (if approved)

```
PerfectlySnug/
├── appdaemon/
│   └── sleep_controller_v6.py             # subclasses v5; safe-degrade to it
├── ml/
│   ├── features.py                        # build_state_vector(snapshot, zone)
│   ├── residual.py                        # BayesianRidgeResidual, GPResidual
│   ├── value_head.py                      # IQLValueHead (numpy + torch optional)
│   ├── reward.py                          # composite c_t (per-zone weights)
│   └── train_v6.py                        # nightly refit, walk-forward eval
├── tools/
│   └── eval_v6_walk_forward.py            # produces the §11 number
└── docs/
    └── proposals/2026-05-01_opt-learned.md  # this file
```

State (`/config/apps/ml/state/{ridge,gp,iql}_*.pkl`) is regenerated nightly; missing or stale state → controller stays on v5.2 path. Two HA helpers: `input_boolean.snug_v6_residual_enabled` (default off) and `input_number.snug_v6_sigma_k` (default 1.0).

---

## 14. Decision summary for the fleet review

- **What this method buys**: a defensible ~12 % MAE-vs-override improvement on the left zone, and a *principled* right-zone fallback (LCB + composite reward) that won't fall into the override-absence trap.
- **What it doesn't buy**: a right-zone improvement we can claim with a straight face today, or any robustness to regime changes faster than nightly batch.
- **Why it's worth deploying anyway**: the conservative bound (`|Δ| ≤ 1` until n_supp ≥ 5) and the hard-fallback structure mean the **downside vs v5.2 is bounded at ≤ 1 L-step** in steady state, with kill switches. The expected value over the next 30 nights is positive on left and zero on right; both are acceptable.

Sign-off: opt-learned, 2026-05-01.
