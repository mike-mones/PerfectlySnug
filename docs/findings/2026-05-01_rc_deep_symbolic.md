# 2026-05-01 — RC deep symbolic regression

Scope: right-side Responsive Cooling only, `responsive_cooling=on` and `running=on`, 2026-04-17 11:39:24Z → 2026-04-30 20:35:30Z. Target is `sensor.smart_topper_right_side_blower_output` in blower percent.

Rows after 30-second alignment and filtering: **19,051**. Active modulation rows (5–60% blower): **12,071**.

## Pareto-front candidate equations

| rank | equation | params | TS-CV R² | TS-CV MAE | active MAE |
|---:|---|---:|---:|---:|---:|
| 1 | `clip(-1.02*(setpoint-ambient)-6.21*setting+8.23)` | 3 | 0.390 | 16.78 | 8.49 |
| 2 | `clip(-0.991*(setpoint-ambient)+33.6)` | 2 | 0.242 | 19.04 | 11.94 |
| 3 | `clip(-0.729*(body_max-ambient)-6.65*setting+6.31)` | 3 | 0.054 | 20.20 | 12.22 |
| 4 | `clip(-0.831*(body_max_30m-ambient_30m)-7.13*setting-29.1)` | 3 | -0.052 | 21.73 | 13.00 |
| 5 | `clip(3.05*body_max-6.7*setting-274)` | 3 | -0.302 | 22.80 | 23.26 |

## Best parametric equation

Best full-window equation: `clip(-1.02*(setpoint-ambient)-6.21*setting+8.23)`

- TS-CV R²: **0.390**
- TS-CV MAE: **16.78 blower points**

Best equation fit only on active modulation rows:

`clip(-0.88*body_max+0.624*ambient-5.65*setting+25.1)`

- Active-regime TS-CV R²: **-0.070**
- Active-regime TS-CV MAE: **8.30 blower points**

## Setpoint/body-max regime check

| Blower bin | n | mean setpoint-body_max | MAE setpoint-body_max |
|---|---:|---:|---:|
| 0-5% | 5,357 | 8.09 | 8.10 |
| 5-15% | 1,106 | -0.04 | 0.18 |
| 15-30% | 4,170 | -0.13 | 0.21 |
| 30-60% | 6,795 | -0.15 | 0.18 |
| 60-100% | 1,623 | -4.70 | 6.42 |

## Does this look like the true firmware equation?

No. The best compact formulas recover only coarse behavior. Their time-series CV error remains large, and the fitted coefficients are not stable enough to identify a simple firmware law from observed features alone. The active-regime fit is better but still too noisy to claim exact recovery. This supports the prior finding that the firmware depends on unobserved internal state (occupancy/mode/integral/time-since-entry), while exposed `setpoint` is mostly a process value near `max(body_*)` during active modulation rather than a user target.

## Reproducibility

Code: `PerfectlySnug/tools/rc_symbolic.py`.

I did not write to `/tmp`; this environment forbids `/tmp` file operations. The script caches data at `PerfectlySnug/ml/state/rc_symbolic_cache.csv` when writable.
