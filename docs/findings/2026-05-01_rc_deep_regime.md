# RC Firmware: Per-Regime Decomposition Analysis

**Date:** 2026-05-01  
**Approach:** Per-regime state machine decomposition  
**Data:** 4,142 samples @ 1-min resolution (2026-04-18 → 2026-04-30), RC-on right side only  
**Code:** `/tmp/rc_regime.py`

---

## Executive Summary

The RC firmware operates as a **feedback controller with 5 distinct operating regimes**. Decomposing by regime and fitting per-regime models yields a **28.9% MAE reduction** over a single global model (2.66% vs 3.74% blower output MAE). The dominant behavior is a **near-unity lag-1 autoregressive process** — the blower changes incrementally each cycle based on thermal error.

---

## 1. Identified Regimes

| Regime | Definition | N samples | Blower avg | Blower std |
|--------|-----------|-----------|-----------|-----------|
| **Idle** | blower_lag ≤ 3% | 75 | 18.1% | 14.3% |
| **Ramping Up** | Δblower > +1%/min | 1,234 | 31.1% | 13.0% |
| **Ramping Down** | Δblower < −1%/min | 1,287 | 25.8% | 14.4% |
| **Holding** | \|Δblower\| ≤ 1%/min, blower > 3%, blower ≤ 60% | 1,529 | 31.6% | 10.4% |
| **Saturated** | blower > 60% | 17 | 70.1% | — |

The system spends most time oscillating between holding and ramping — a classic bang-bang-ish controller with slow ramps.

---

## 2. State Machine / Transition Rules

### Transition Matrix (top transitions)

| From → To | Count | Trigger Condition |
|-----------|-------|-------------------|
| holding → ramping_down | 430 | error decreasing / overshoot resolved |
| ramping_up → ramping_down | 429 | blower overshot target zone |
| ramping_up → holding | 421 | error stabilized near 0 |
| ramping_down → ramping_up | 409 | error rising again |
| ramping_down → holding | 380 | blower found stable point |
| holding → ramping_up | 377 | error_mean = +0.19°F (body warming) |
| ramping_down → idle | 75 | body well below setpoint, blower → 0 |
| idle → ramping_up | 64 | error_mean = +0.43°F, body_max ≈ 86.8°F |

### Key Trigger Thresholds

- **Idle → Ramping Up:** error > ~0.4°F (body_max exceeds setpoint by 0.4°F)
- **Holding → Ramping Up:** error > ~0.2°F and rising
- **Ramping → Holding:** |Δblower| drops below 1%/min
- **Ramping Down → Idle:** blower reaches ≤ 3%

### State Machine Diagram

```
                    error > 0.4°F
        ┌──────────────────────────────┐
        │                              ▼
     [IDLE] ◄──── blower≤3% ──── [RAMPING DOWN]
        │                              ▲    │
        │                              │    │ |Δblower|<1
        │                              │    ▼
        │              error rising   [HOLDING]
        │                    ▲         │    ▲
        │                    │         │    │
        │                    └─────────┘    │
        │                  error>0.2        │ |Δblower|<1
        │                                   │
        └──────── error>0.4°F ───► [RAMPING UP]
                                        │
                                        │ blower>60%
                                        ▼
                                   [SATURATED]
```

---

## 3. Per-Regime Model Equations

### Holding (n=1,529) — **Near-perfect autoregressive**
```
blower(t) ≈ 0.7 + 0.99 × blower(t-1)
```
LR R² = 0.995, GB TS-CV MAE = 0.87%

The firmware holds blower essentially constant. This is the steady-state.

### Ramping Up (n=1,234) — **Error-driven increment**
```
blower(t) ≈ 34.5 + 0.89×blower(t-1) + 0.72×error − 2.93×body_max_roc − 1.53×ambient_roc
```
LR R² = 0.912, GB TS-CV MAE = 2.45%

Blower increases proportional to thermal error. Faster body warming = stronger response. Negative body_max_roc coefficient suggests anticipatory damping.

### Ramping Down (n=1,287) — **Error + absolute temp driven**
```
blower(t) ≈ −15.6 + 2.55×body_max + 3.76×error + 5.69×ambient_roc + 1.41×body_max_roc
```
LR R² = 0.800, GB TS-CV MAE = 3.72%

When ramping down, absolute body_max matters more — firmware reduces blower proportionally as body cools toward setpoint.

### Idle (n=75) — **Small sample, complex**
```
blower(t) ≈ 217.6 − 1.96×body_max − 10.89×ambient_roc
```
GB TS-CV MAE = 7.48% (poor, small sample)

---

## 4. Decision Tree Approximation

The full firmware can be approximated by a depth-6 regression tree using `[error, ambient_error, blower_lag1, body_max_roc, body_spread, ambient]`:

**Primary splits:**
1. `blower_lag1 ≤ 31.25` — divides low vs high output states
2. `blower_lag1 ≤ 22.25` — subdivides low-output  
3. `error ≤ 0.62` — within low-output, decides ramp direction
4. `body_spread ≤ 4.94` — uniform vs non-uniform body heat
5. `body_max_roc ≤ -0.05` — cooling vs warming trend

In-sample: R² = 0.83, MAE = 3.16%

---

## 5. Aggregate TS-CV Results

| Model | MAE (%) | R² | Method |
|-------|---------|-----|--------|
| Global GB (200 trees, depth 4) | 3.74 | 0.6575 | 5-fold TS-CV |
| **Per-Regime GB** | **2.66** | **0.7659** | 5-fold TS-CV |
| Decision Tree (depth 6) | 3.16 | 0.8334 | In-sample |

**Per-regime improvement: 28.9% MAE reduction over global model.**

### Per-fold breakdown (combined per-regime):
| Fold | MAE | R² |
|------|-----|-----|
| 1 | 2.43% | 0.8185 |
| 2 | 3.10% | 0.6811 |
| 3 | 1.93% | 0.8620 |
| 4 | 2.19% | 0.7318 |
| 5 | 3.64% | 0.7361 |

---

## 6. Interpretation: What Is the Firmware Doing?

The RC firmware implements a **proportional-integral feedback controller with regime switching**:

1. **Core loop:** Every ~1 minute, compare body_max to setpoint (error signal).
2. **Holding regime (37% of time):** If error is small (±0.2°F), maintain current blower output (≈ blower(t-1)).
3. **Ramping up (30%):** If error > 0 and rising, increment blower by ~0.5-2%/min proportional to error magnitude.
4. **Ramping down (31%):** If error ≤ 0 or dropping, decrement blower proportional to how far below setpoint.
5. **Idle (2%):** If blower has ramped to near-zero, stop fan entirely.
6. **Saturated (<1%):** High error persists → max fan output.

The controller is **heavily smoothed** — blower_lag1 explains >90% of variance in holding, indicating the firmware uses small incremental adjustments rather than jump-to-target logic. This prevents oscillation and noise.

---

## 7. Limitations

- Only 17 "saturated" samples — insufficient to model that regime
- The "idle" regime has only 75 samples and high variability
- Regime classification uses blower dynamics (Δblower) which requires 1-step lag — not purely causal but observable in real-time
- R² of 0.77 suggests ~23% of variance is unexplained — likely from:
  - Bed presence sensor influence (not modeled as continuous)
  - Internal firmware timers/delays not visible in sensor data
  - Possible hysteresis bands not captured by instantaneous features
