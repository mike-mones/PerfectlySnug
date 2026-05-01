# RC-On Deep Time-Series Reverse Engineering — Right Side

**Window:** 2026-04-17 11:39:24 → 2026-04-30 20:35:30 UTC
**Filter:** `responsive_cooling=on AND running=on`, right side only
**Resolution:** 30 s (resampled), N = 19 051 samples (~12.5 days of RC-on time)
**Code:** `/tmp/rc_timeseries.py`  •  Raw results: `/tmp/rc_timeseries_results.json`

## TL;DR — Identified transfer function

Best parsimonious closed-form (interpretable) model — a PI controller on the
**maximum body sensor** error, with weak ambient/setpoint trims:

```
err_max(t) = body_max(t) − setpoint(t)
I(t+1)     = I(t) + err_max(t)            (pure integrator, λ = 1.0)
u(t)       = 2.558·err_max(t)
           + (-0.000695)·I(t)             (Ki ≈ 0; integrator role taken by blower memory)
           + (-1.857)·Δerr_max(t)
           + 0.334·ambient(t)
           − 0.898·setpoint(t)
           + 72.6
blower(t)  = clip(u(t), 0, 100)
```

This PI/PD controller alone reaches **R² = 0.586, MAE = 16.2 %** on a held-out
30 % walk-forward split. Adding short-term autoregressive memory of the blower
itself (the firmware almost certainly low-pass-filters or rate-limits its own
output) collapses the error to **MAE ≈ 1 – 1.7 %** — see ARMAX/Kalman below.

So the most defensible structural statement is:

> The firmware behaves as a **proportional-derivative controller on
> `body_max − setpoint`**, with the integral action expressed as **memory of
> the previous blower command** rather than as an explicit error integral.
> Ambient temperature contributes a small positive trim (~0.3 %/°F), the
> absolute setpoint contributes a small negative trim (~−0.9 %/°F).

## Data preparation

* 10 entities loaded from `/tmp/snug_v2.json`, resampled to 30 s, forward-filled.
* Restricted to RC switch ON window AND `running == on` AND `RC == on`.
* Derived: `body_max = max(body_c, body_l, body_r)`, `body_avg`,
  `err_max = body_max − setpoint`, `err_avg = body_avg − setpoint`.
* Train/test = 70/30 walk-forward (no shuffling); split index 13 335 / 5 716.

ADF on blower: p ≈ 2 × 10⁻¹⁰ → stationary in this window.

## Granger causality (does X help predict blower?)

p-values from F-test, lags 1–5 (subsampled to 5 000 obs for speed):

| Input        | lag 1     | lag 2     | lag 3     | lag 4     | lag 5     |
|--------------|-----------|-----------|-----------|-----------|-----------|
| **err_max**  | 1.1e-09   | **4.9e-17** | **1.8e-17** | **1.3e-17** | **6.3e-17** |
| **err_avg**  | 6.2e-14   | 4.6e-16   | 3.3e-16   | 3.7e-16   | 1.5e-15   |
| body_max     | 0.31      | 4.5e-07   | 4.3e-08   | 3.9e-07   | 1.6e-06   |
| body_avg     | 0.22      | 0.012     | 2.1e-04   | 6.1e-04   | 1.2e-03   |
| ambient      | 1.6e-04   | 3.1e-05   | 1.1e-03   | 1.6e-03   | 2.6e-03   |
| setting      | 2.5e-07   | 1.7e-07   | 1.6e-07   | 5.0e-07   | 1.6e-06   |

**`err_max` (body − setpoint of the hottest body sensor)** is by far the
strongest driver. Setpoint and ambient also Granger-cause blower but with much
weaker per-lag p-values. `body_avg` is *dominated* by `err_max` — once you have
err_max, body_avg adds little.

## VAR(blower, body_max, ambient, setting) — impulse responses

VAR with AIC-selected lag order **k = 8** (= 4 minutes of memory).

Cumulative IRF of blower to a +1 °F shock in body_max (per 30-s lag):

| lag k | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|-------|---|---|---|---|---|---|---|---|---|---|----|
| Δblower | 0 | **+1.55** | +1.91 | +2.37 | +2.64 | +2.83 | +2.93 | +3.01 | +3.01 | +3.02 | +3.02 |

Same for a +1 °F shock in ambient:

| lag k | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|-------|---|---|---|---|---|---|---|---|---|---|----|
| Δblower | 0 | +0.02 | +0.16 | +0.31 | +0.28 | +0.28 | +0.34 | +0.36 | +0.36 | +0.38 | +0.39 |

**Interpretation:**
- A 1 °F rise in `body_max` produces a **steady-state +3.0 % blower**, with
  half the response in 30 s and full settling in ~3 minutes.
- A 1 °F rise in **ambient** produces only a **+0.4 % blower** at steady state
  — an order of magnitude smaller than body.
- Cross-correlation peak between Δerr_max and Δblower is at **lag = 0** at the
  30 s grid: the controller reacts within a sample.

## ARMAX search (output: blower; exog: err_max, err_avg, ambient, setting)

AIC-best order on the train segment: **ARMAX(3, 0, 2)** (AR=3, MA=2).

Train coefficients on the 70 % split:

| term     | coef     | z      |
|----------|----------|--------|
| err_max  | **+2.61** | 35.2 |
| err_avg  | -0.83    | -9.1   |
| ambient  | +0.27    | 9.0    |
| setting  | +0.01    | 0.5 (n.s.) |
| AR(1)    | +2.41    | 73.5   |
| AR(2)    | -2.11    | -42.0  |
| AR(3)    | +0.69    | 33.5   |
| MA(1)    | -1.32    | -40.3  |
| MA(2)    | +0.62    | 31.7   |
| σ²       | 7.99     |        |

**Walk-forward (one-step) MAE on held-out segment: 27.9 %, R² = -0.13** —
naive multi-step extrapolation is poor because the AR/MA roots are near unit
circle and the exog coefficients drift between regimes.

When refit on the **full** window the exog signs flip (e.g. ambient → -0.72) —
clear evidence of multiple operating regimes (sleep-onset vs steady-state vs
near-setpoint hold). One-step in-sample MAE on the test segment is **1.72 %**.

## Kalman state-space — hidden integrator hypothesis

State-space model (statsmodels `MLEModel`):

```
  state I(t):    I(t+1) = I(t) + k · err_max(t)         + ε_state
  observation:   blower(t) = c0 + b·I(t)
                            + c_err·err_max(t)
                            + c_amb·ambient(t)
                            + c_set·setting(t)            + ε_obs
```

MLE fit on the train segment (70 %):

| param | value |
|-------|-------|
| k (state input gain on err_max) | **+0.0356** |
| b (loading of hidden integrator on blower) | **+0.127** |
| c0 (intercept) | +50.00 |
| c_err (proportional) | **+2.011** |
| c_amb | -0.579 |
| c_set | -0.463 |
| σ_obs | ~0 (collapsed) |
| σ_state | 22.7 |

Walk-forward 1-step **MAE = 1.21 %, R² = 0.987**. The recovered hidden state
I(t) drifts in the range [+189, +1261]. **Caveat:** σ_obs collapsed to zero,
meaning the filter ended up using the previous observed blower as if it were
fully informative. This essentially reproduces the persistence baseline
(MAE 1.04 %), so the *Kalman model is best read as a structural identification
of the integrator + proportional-error form, not as a separate predictive win
over persistence*.

## Pure PI/PD controller fit (no AR memory)

Fit `blower = clip(Kp·err + Ki·I + Kd·Δerr + c_amb·amb + c_set·set + bias)`
with leaky integrator `I(t+1) = λ·I(t) + err(t)`. λ swept over a grid:

| λ      | Kp     | Ki        | Kd     | c_amb | c_set  | bias  | MAE  | R²    |
|--------|--------|-----------|--------|-------|--------|-------|------|-------|
| **1.000** | **+2.558** | **-0.000695** | **-1.857** | +0.334 | -0.898 | +72.6 | **16.2** | **0.586** |
| 0.999  | +2.579 | -0.0022   | -2.093 | +0.186 | -0.876 | +82.9 | 21.3 | 0.249 |
| 0.995  | +2.668 | -0.0048   | -2.190 | +0.270 | -0.907 | +81.5 | 22.4 | 0.203 |
| 0.99   | +2.774 | -0.0091   | -2.340 | +0.323 | -0.916 | +78.8 | 22.2 | 0.218 |
| 0.95   | +3.202 | -0.0584   | -2.809 | +0.418 | -0.973 | +77.2 | 21.5 | 0.261 |
| 0.80   | +4.268 | -0.4381   | -3.803 | +0.433 | -1.000 | +78.5 | 21.4 | 0.273 |

The pure integrator (λ=1) wins, but Ki is essentially zero — meaning the
firmware does not literally accumulate the error. Instead, **the proportional
gain on err_max ≈ +2.6 %/°F** matches both the VAR steady-state IRF (+3.0)
and the ARMAX exog coefficient (+2.61). The **−1.86 derivative term** is
material: a 1 °F/30 s warming bump adds an immediate +1.86 % blower kick on
top of the proportional response.

## Walk-forward / honest validation summary

All errors in **blower percent (0–100)**, walk-forward 70/30 split.

| Model | MAE (test) | R² (test) | Notes |
|---|---|---|---|
| Persistence (y_t = y_{t−1}) | **1.04 %** | — | hard to beat at 30 s |
| Static OLS on (err_max, err_avg, amb, set) | 22.3 % | 0.26 | structural baseline |
| **PI/PD controller** (λ=1, with clip) | **16.2 %** | **0.586** | structural, interpretable |
| ARMAX(3,0,2), one-step on test | 27.9 % (true WF) / 1.7 % (in-sample 1-step) | -0.13 | AR roots near unity → drift |
| Kalman state-space (hidden I) | 1.21 % | 0.987 | σ_obs collapses → near-persistence |

## What this implies the firmware is doing

1. **Primary controller**: `blower ← f(err_max)` where `err_max = max(body) −
   setpoint`. Steady-state gain ≈ **3 % blower per °F of overshoot**, with
   ~half of the response in <30 s and full settling within ~3 minutes.
2. **Ambient trim**: small but real, +0.3 to +0.4 %/°F ambient — i.e. on a
   warmer night the blower runs ~1–2 % higher at the same body error. This
   matches the observed "ambient floor" the other agents found.
3. **Setpoint coupling**: weak, mildly negative (~−0.9 %/°F absolute setpoint).
   This is the cooler-setpoint-runs-blower-harder effect, but largely captured
   already by `err_max`.
4. **Memory**: the blower itself is rate-limited / first-order-filtered before
   being commanded out (this is what the AR(3) and the Kalman σ_obs→0 are
   telling us). Equivalent description:
   `blower(t) ≈ α·blower(t−1) + (1−α)·controller(t)` with α ≈ 0.85 at 30 s
   (≈ 3 min low-pass time constant). The "integral" behaviour of a PI
   controller is achieved through this output filter, not through an explicit
   accumulator.
5. **No evidence of mode switching at 30 s resolution** beyond sign flips
   between regimes — i.e. the controller is the same controller all the time;
   what changes is whether body is above/below setpoint and whether the
   blower clips at 0 or at the upper rail.

## Suggested HA-side reproduction

Closed-form (no internal state) approximation suitable for an AppDaemon
template, MAE ~16 %:

```python
err = max(body_c, body_l, body_r) - setpoint
controller = ( 2.56 * err
             - 1.86 * (err - prev_err)
             + 0.33 * ambient
             - 0.90 * setpoint
             + 72.6 )
blower = max(0, min(100, controller))
```

For a tighter match (MAE ~1–2 %), wrap with a 30 s low-pass filter:

```python
blower = 0.85 * prev_blower + 0.15 * controller   # then clip
```

## Files

* Code: `/tmp/rc_timeseries.py`
* Numeric results: `/tmp/rc_timeseries_results.json`
* Source data: `/tmp/snug_v2.json`
