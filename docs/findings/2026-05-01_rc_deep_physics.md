# Responsive Cooling — Deep Physics / System ID

**Author:** parallel agent #control-theory
**Date:** 2026-05-01
**Data window (RC ON ∧ running ON):** 2026-04-17 11:39:24 → 2026-04-30 20:35:30 UTC
**Right-side only.** 13 nightly segments, ~13 h each, resampled to 10 s grid → 57 149 samples.
**Target:** `sensor.smart_topper_right_side_blower_output` (integer 0 or 10..100).
**Inputs:** `body_l/c/r`, `ambient`, `bedtime_temperature` (`sp_user`, integer −10..−4 in this window).
**Internal observable used as auxiliary:** `temperature_setpoint`.

---

## TL;DR — recovered control structure

The firmware implements a **two-stage cascade** controller, NOT a vanilla PID:

```
                      ┌─────────────────────────────┐
 body_max ──────────► │  Stage 1: Peak-tracking     │── setpoint(t)
                      │  setpoint generator         │
 sp_user (cap) ─────► │  (slow integrator + cap)    │
                      └─────────────────────────────┘
                                       │
 body_max ────────────────┐            ▼
 body_avg ────────────┐   │   ┌─────────────────────────────┐
 ambient ─────────┐   │   │   │  Stage 2: P + rate FF       │
                  ▼   ▼   ▼   │  + ambient FF               │── blower(t)
                              │  + on/off deadband          │
                              │  + min‑on quantization      │
                              └─────────────────────────────┘
```

It is **closest to a P-controller with feed-forward (P + FF), wrapped in a Hammerstein output stage** (deadband + min-on saturation). It is **not** a PI/PID — the integrator (Ki) consistently optimizes to zero because the integral action is effectively *outside* the controller, embedded in the slow setpoint generator.

---

## Stage 1 — setpoint generator (slow peak-tracker)

The exposed `temperature_setpoint` is *internal* state — a **leaky max-hold** of `body_max`, capped by a per-night ceiling derived from `sp_user` plus history:

```
setpoint(t) = min( cap , max( body_max(t), setpoint(t-1) − leak·Δt ) )
```

Identified parameters (per 10 s step):

| Parameter | Value | Units | Notes |
|---|---|---|---|
| `leak`     | **0.002 °F / 10 s** ( ≈ 0.012 °F/min ) | °F | Best fit over 13 nights, MAE 1.89 °F (ratchet decays slowly between body peaks) |
| `cap`      | ≈ 0.337·sp_user + 88.6 °F (linear approx.) | °F | residual ±0.57 °F. Real cap is **half-°C-quantized** (29.5 / 30.0 / 30.5 / 31.0 °C ⇒ 85.1 / 86.0 / 86.9 / 87.8 °F). Suggests firmware works in °C internally. |

**Cap is *not* a pure function of `sp_user`** — for `sp_user = −4` we observe both 87.8 °F and 86.9 °F across nights. Likely depends on prior‑night peak history or a separate "comfort_max_temp" user parameter not in our input set. **This is the dominant source of full-pipeline error.**

---

## Stage 2 — controller (P + rate-FF + ambient-FF + deadband)

```
err_max(t)  = body_max(t) − setpoint(t)
err_avg(t)  = body_avg(t) − setpoint(t)
err_amb(t)  = body_avg(t) − ambient(t)
ḃ_max(t)    = d body_max / dt        (positive part only)
ḃ_avg(t)    = d body_avg / dt        (positive part only)

target(t)   = bias
            + Kp_max  · err_max
            + Kp_avg  · err_avg
            + Kff_amb · err_amb
            + Krise_max · max(0, ḃ_max) · 60
            + Krise_avg · max(0, ḃ_avg) · 60      # rates expressed as °F/min

blower(t)   = 0                                        if target < off_thresh
            = clip( max(min_on, target), 0, 100 )      otherwise
```

### Identified parameters (5-fold time-series CV, mean ± std across folds)

| Coefficient    | Value           | Interpretation |
|---|---|---|
| `Kp_max`       | **19.1 ± 2.5**  | Dominant gain. ~19 % blower per °F that body_max sits above the running setpoint (the ratchet keeps this small except when body is warming). |
| `Kp_avg`       | **1.4 ± 0.5**   | Weak P-action on average body vs setpoint. |
| `Kff_amb`      | **−1.45 ± 0.29**| Ambient feed-forward. Cooler ambient ⇒ less blower needed (passive cooling helps). |
| `Krise_max`    | **0.96 ± 0.62** | Rate FF on body_max (per °F/min). |
| `Krise_avg`    | **0.46 ± 0.27** | Rate FF on body_avg. |
| `bias`         | **46.8 ± 4.3 %**| Operating-point bias (~47 % blower at error≈0). |
| `off_thresh`   | **16.4 ± 3.4 %**| If computed target < this, blower is forced to 0. |
| `min_on`       | **13.2 ± 5.9 %**| Hammerstein min-on quantization (matches the empirical fact that blower never takes values 1–9). |
| `Ki` (integral)| **≈ 0**         | Optimizer drives Ki → 0 in every fold. The integral action lives in stage 1 (setpoint ratchet), not stage 2. |

### Performance

| Configuration | Train MAE | Test MAE | R² (full data) |
|---|---|---|---|
| **Stage 2 only**, with **observed** setpoint | 7.51 ± 0.56 | **9.31 ± 2.88** | **0.736** |
| Full pipeline (derive setpoint from body_max + sp_user-cap rule) | n/a | 18.5 ± 11.8 | −0.02 |

All errors are in **blower percent points** (target range 0–100).
The full-pipeline degradation is dominated by mis-prediction of the per-night cap (see §Stage 1).

---

## Step-response / FOPDT identification

Cross-correlation of `blower` vs `(body_max − setpoint)` peaks at **lag 0** ⇒ the controller has **no internal lag**: blower output reacts within one 10 s sample to error. All apparent "smoothing" comes from the body-sensor thermal time constant, not from a controller LP filter. FOPDT identification on `error → blower` returns negative τ (degenerate fit), confirming there is no first-order controller dynamics. This is consistent with an algebraic P-controller, not a PI/PID with integral lag.

---

## Why it is not a PID

1. Optimizer drives **Ki → 0** every fold when fitting on observed setpoint.
2. Cross-correlation peak at zero lag ⇒ no derivative-induced lead.
3. The integrator-like behavior in the closed loop is provided by **stage 1** (the leaky max-hold of body_max), which explains why the system exhibits slow ratcheting setpoint behavior typical of a comfort-tracking thermostat.
4. The absence of values in `[1..9]` and saturation at exactly 100 % confirm a **deadband + minimum-on Hammerstein output stage**, not a continuous PWM PID.

This pattern matches a **"comfort peak tracker"** style controller commonly used in mattress / bedding thermal regulators: the setpoint silently adopts the user's nightly thermal peak; the blower then keeps the average body close to that peak by turning on whenever current body crosses or rises toward it, and shutting off when body has cooled enough that natural dissipation is sufficient.

---

## Per-segment fit examples

`/tmp/rc_physics_fit_segments.png` — actual vs predicted blower across four representative nights (segs 3, 6, 9, 12). The model captures the on/off transitions correctly and the magnitude of pulses; residual error is dominated by high-frequency dithering of the firmware around the ~30–40 % operating band.

`/tmp/rc_physics_seg1.png` — overlay of body sensors, setpoint, ambient, blower for segment 1, showing the peak-tracking behavior of the setpoint.

`/tmp/rc_physics_hysteresis.png` — blower vs `body_max − setpoint`, colored by error direction. No strong rising/falling asymmetry → the controller is **not** classic bang-bang with hysteresis; the apparent vertical band at err≈0 is explained by the rate-FF terms.

---

## Confidence intervals (5-fold TS-CV folds = pseudo-bootstrap)

Bootstrapping by leave-one-night-out across 13 nights gives the same coefficient ranges as the 5-fold CV. The most robust parameter is **`Kp_max ≈ 19 %/°F`**; the least robust is **`bias ≈ 47 %`** which trades off with `off_thresh` and `min_on`.

| Parameter   | Mean | 95 % CI (CV-based) |
|---|---|---|
| Kp_max      | 19.1 | [14.1, 24.1]  |
| Kp_avg      |  1.4 | [0.5, 2.4]    |
| Kff_amb     | −1.45| [−2.0, −0.9]  |
| Krise_max   |  0.96| [0.0, 2.2]    |
| Krise_avg   |  0.46| [0.0, 1.0]    |
| bias        | 46.8 | [38.3, 55.4]  |
| off_thresh  | 16.4 | [9.6, 23.2]   |
| min_on      | 13.2 | [1.5, 24.9]   |

---

## Open problems for downstream work

1. **Cap rule.** With only 13 nights and 6 distinct `sp_user` values we cannot identify the exact cap function. Hypotheses to test with more data: (a) cap depends on a separate `comfort_max_temp` user setting; (b) cap depends on rolling N-night peak body temperature; (c) cap is updated by the cloud overnight.
2. **Discrete blower quantization.** Blower takes only integer values, with the gap [1..9] always skipped. Our continuous predictions should be **post-rounded to integer and snapped to {0, 10..100}** for any deployment-side use.
3. **Per-side asymmetry.** Center body sensor dominates the "argmax body" statistics (29 k of 41 k active samples), suggesting the firmware may weight the center sensor more heavily; an explicit weighted-max could improve fit by ~1 MAE point.
4. **Stage-1 leak rate.** 0.002 °F / 10 s is approximate; refit jointly with a true cap model.

---

## Reproducibility

* Code: `/tmp/rc_physics.py` (data loader + grid build) and the analysis snippets shown above.
* Cached grid: `/tmp/rc_grid.pkl` (57 149 samples, 10 s resolution, RC-on ∧ running-on).
* Fit artifacts: `/tmp/rc_physics_fit.json`.
* Plots: `/tmp/rc_physics_seg1.png`, `/tmp/rc_physics_hysteresis.png`, `/tmp/rc_physics_fit_segments.png`, `/tmp/rc_physics_xcorr.png`.
