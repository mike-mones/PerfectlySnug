# 2026-05-01 — Responsive Cooling empirical/nonparametric deep dive

## Scope

Right side only. Filtered target population:

- `2026-04-17 11:39:24 UTC` → `2026-04-30 20:35:30 UTC`
- `switch.smart_topper_right_side_responsive_cooling = on`
- `switch.smart_topper_right_side_running = on`
- occupied rows only (`body_max > 75°F`)
- target: `sensor.smart_topper_right_side_blower_output`

The clean 30-second analysis set had **29,793 feature-ready rows** after switch and occupancy filtering.

## Best empirical model

The best purely empirical family was a tree-based lookup/regression surface, not a closed-form rule:

| Model | Time-series CV R² | MAE (blower %) | Notes |
|---|---:|---:|---|
| Gradient Boosting, rich sensor/history features | **0.102–0.134** | **13.04–13.11%** | Best nonparametric approximation on strict TS-CV |
| Random Forest, rich sensor/history features | 0.091 | 13.11% | Similar to GB |
| Linear `ambient_err + body_max_err + ambient integral` | 0.042 | 17.47% | Best simple baseline |
| Linear `ambient_err + body_avg_err + ambient integral` | -0.027 | 17.98% | Does not generalize |
| Polynomial-2 | -0.979 | 21.85% | Overfits/regime leakage |

Earlier random CV looked excellent, but TS-CV collapses to ~0.10 R². That is the key empirical finding: nearby points in shuffled space are easy to interpolate, but future time blocks are not. The firmware behavior is strongly time/regime dependent.

## Lookup-table findings

The 1°F 2D binned heatmaps show the clearest visible pattern:

1. `blower(body_max, ambient)` is the most informative 2D surface.
2. `blower(body_max, setpoint/setting)` is weaker unless transformed into `body_max - setpoint`.
3. `blower(setpoint, ambient)` has broad structure but high within-bin variance.
4. High standard deviation within many 1°F cells means the same instantaneous sensor tuple can map to very different blower values.

That within-cell variance is why a static lookup table is not enough. A practical lookup needs history terms: `body_max[t-1m]`, `body_max[t-5m]`, rolling/integral body error, and rolling/integral ambient error.

## Conditional means

The 1D conditional mean plots support these monotonic tendencies:

- `mean(blower | body_max - setpoint)` rises: warmer body relative to setpoint generally increases blower.
- `mean(blower | ambient - setpoint)` rises more weakly but consistently.
- `mean(blower | body_max)` rises, but the curve is smeared by changing setpoint.
- `mean(blower | setpoint)` alone is not a clean rule; setpoint only matters in relation to body/ambient temperatures.
- Rolling/integral terms are smoother than instantaneous terms, but not enough to define a stable single equation.

Simple in-sample checks on the clean data:

| Hypothesis | In-sample R² |
|---|---:|
| `blower ~ ambient_err` | 0.102 |
| `blower ~ body_max_err` | 0.252 |
| `blower ~ body_avg_err` | 0.202 |
| `blower ~ ambient_err + body_max_err` | **0.271** |
| `blower ~ ambient_err + ambient_int600 + body_max_err` | **0.271** |

The best simple visible rule is approximately:

```text
blower ≈ 24.4 + 1.5*(ambient - setpoint) - 0.8*rolling_10m(ambient - setpoint)
         + 3.5*(body_max - setpoint)
```

But this is only a descriptive trend, not a deployable replacement: it fails TS-CV with MAE ~17.5%.

## k-NN / kernel / lookup interpretation

The nonparametric models tell the same story:

- k-NN is useful for local interpolation inside a night/regime.
- 1°F binned lookup tables expose the surfaces and the high within-cell variance.
- RBF/kernel regression and polynomial smoothing overfit because the train/test time blocks are not identically distributed.
- Tree ensembles are the best empirical approximation because they behave like adaptive multidimensional lookup tables with implicit regime splits.

Recommended empirical approximation if one must emulate RC:

```text
GradientBoostingRegressor over:
  body_max, body_max-setpoint,
  ambient-setpoint,
  body_max lags at 1m and 5m,
  rolling body_max error at 1/3/5/10/30m,
  rolling ambient error at 1/3/5/10/30m,
  setpoint,
  individual body sensor errors.
```

Do **not** include blower lag if the goal is to replace firmware RC. Blower lag improves interpolation but just copies firmware state and cannot cold-start a replacement controller.

## Clean rule?

No exact lookup/rule emerged. The cleanest empirical pattern is:

```text
Responsive Cooling increases blower primarily when max(body sensors) exceeds setpoint,
with ambient air temperature acting as a secondary feedforward/regime signal.
```

However, the same `body_max`, `ambient`, and `setpoint` bins often contain widely different blower outputs. That implies hidden state or omitted inputs: ramp limits, internal hysteresis/deadband, compressor/fan mode, app schedule phase, or firmware state not exposed in HA history.

## Deliverable code

Analysis code is in:

```text
PerfectlySnug/tools/rc_empirical.py
```

It generates:

- 1°F mean/std 2D heatmaps
- 1D conditional mean plots
- lookup, k-NN, RBF/kernel, random forest, and gradient boosting TS-CV results

