# PerfectlySnug ML Sleep Controller — Technical Design Document

## 1. Problem Statement

The current sleep controller (v5) uses a crude weighted moving average of override deltas as its "learning" mechanism. After 14 nights of operation, it achieves only **8.3% autonomous success rate** (1/12 nights without manual intervention) and a **56.5% comfort rate** (readings within ±1 L1 step of user preference). The system oscillates rather than converges because it cannot model multi-variable relationships.

**Goal:** Replace the heuristic learning system with a true ML model that predicts the optimal topper setting given all available sensor inputs, learns from manual overrides as labeled training data, and self-improves nightly without manual hyperparameter tuning.

**Target (initial, weeks 1-3):** >70% comfort rate and <2 overrides/night average.
**Target (stretch, weeks 4+):** >85% comfort rate and <1 override/night as the model accumulates more data.

---

## 2. Sleep Science Foundation

### 2.1 The 90-Minute Sleep Cycle

Human sleep architecture follows a predictable ~90-minute ultradian rhythm:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Cycle 1      Cycle 2      Cycle 3      Cycle 4      Cycle 5           │
│ ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐           │
│ │ Light  │  │ Light  │  │ Light  │  │ Light  │  │ Light  │           │
│ │ Deep   │  │ Deep   │  │ (less) │  │ (min)  │  │  REM   │           │
│ │ REM    │  │ REM    │  │ REM    │  │ REM    │  │ (long) │           │
│ └────────┘  └────────┘  └────────┘  └────────┘  └────────┘           │
│                                                                         │
│  Deep-dominant ─────────────────────────────── REM-dominant            │
│  (First half, ~4 hours)                        (Second half)            │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key patterns across the night:**
- **Cycles 1-2 (0-3h):** Deep (N3) sleep dominates. Core body temperature at nadir. Thermoregulation is ACTIVE — the body can still sweat/vasoconstrict.
- **Cycles 3-4 (3-6h):** REM increases progressively. Deep sleep decreases. Thermoregulation is IMPAIRED during REM — the body behaves more poikilothermically (like a reptile).
- **Cycles 5+ (6h+):** REM dominates. Body temperature begins rising toward wake. Minimal deep sleep.

### 2.2 Thermoregulation by Sleep Stage

| Stage | Thermoregulation | Temperature Preference | Implication |
|-------|-----------------|----------------------|-------------|
| **Wake/Light (N1/N2)** | Fully active | Comfort-neutral | Follow user preference |
| **Deep (N3/SWS)** | Active, body at temp nadir | Cooler helps maintain deep sleep | Aggressive cooling is safe and beneficial |
| **REM** | Impaired/absent | Overcooling causes arousal | Must reduce cooling to prevent wake-ups |
| **Brief Awakening** | Active (briefly) | Comfort-critical | Stabilize; this is when user notices discomfort |

**Critical insight from Eight Sleep research (ref: Eight Sleep Autopilot docs):** "Cooler offsets promote deep sleep; warmer offsets promote REM." The optimal controller doesn't maintain a single temperature — it tracks a *curve* that matches the body's thermoregulatory state.

### 2.3 The Ideal Temperature Curve (Conceptual)

```
Cooling  ─10 ──────╮
Intensity      │    ╰──╮
               │       ╰──╮
          ─7 ──│          ╰──────╮
               │                  ╰──╮
          ─4 ──│                     ╰────────────
               │
          ─1 ──│──────────────────────────────────
               └──────────────────────────────────── Time
               0    90   180   270   360   450  min
               C1   C2    C3    C4    C5    C6
```

This curve must be **modulated** by:
1. **Room temperature** — cold room means less artificial cooling needed
2. **Body temperature** — if body is already cool (from cold room), reduce cooling
3. **Sleep stage** (from Apple Watch) — detected REM → ease off; detected Deep → maintain/increase
4. **Individual physiology** — learned from override history; some people run hotter
5. **Bed presence/movement** — restlessness may indicate discomfort

### 2.4 Reference: Eight Sleep's Approach (from PDF)

Eight Sleep's Autopilot uses:
1. **Multi-phase baseline schedule:** Precondition → Bedtime (15 min post sleep-onset) → Early/Deep (~4h) → Late/REM (until wake) → Pre-wake ramp
2. **Bounded real-time offsets:** Stage-based corrections around the baseline (cooler for deep, warmer for REM), applied per-minute based on sleep staging
3. **Adaptive magnitude:** Offset size increases when prior-night metrics show deficits (deep <15%, REM <20%)
4. **Multi-day calibration:** ~7-day initial period with user feedback loop
5. **Environmental compensation:** Room temperature and seasonal adjustments

**Key architectural insight:** Autopilot uses "offsets from user comfort, not wholesale overrides." The user's preference is the anchor; the ML system nudges within bounds.

---

## 3. Available Data Sources

### 3.1 PerfectlySnug Topper Sensors (30-second polling)

| Sensor | Entity | Description |
|--------|--------|-------------|
| Body Center | `sensor.smart_topper_left_side_body_sensor_center` | Core torso surface temp (°F) |
| Body Left | `sensor.smart_topper_left_side_body_sensor_left` | Left shoulder area (°F) |
| Body Right | `sensor.smart_topper_left_side_body_sensor_right` | Right shoulder area (°F) |
| Ambient | `sensor.smart_topper_left_side_ambient_temperature` | Air temp at topper surface (°F) |
| Setpoint | `sensor.smart_topper_left_side_temperature_setpoint` | Firmware target temp (°F) |
| Blower Output | `sensor.smart_topper_left_side_blower_output` | Actual blower % (when available) |

### 3.2 Apple Watch Health Data (via Health Receiver → PostgreSQL)

| Metric | Frequency | Available Since |
|--------|-----------|----------------|
| Heart Rate | ~3,750 readings/21 days | 2026-04-08 |
| Heart Rate Variability | ~3,640 readings/21 days | 2026-04-08 |
| Respiratory Rate | ~876 readings/21 days | 2026-04-08 |
| Sleeping Wrist Temperature | ~866 readings/21 days | 2026-04-08 |
| **Sleep Stages** (core/deep/rem/awake) | Per-segment, ~140 segments/20 nights | 2026-04-09 |

### 3.3 Bed Presence Sensor (ESP32-based, continuous)

| Sensor | Description |
|--------|-------------|
| Left/Right Pressure | Raw pressure value (movement correlate) |
| Left/Right Calibrated Pressure | Normalized 0-100% |
| Occupied Left/Right/Either/Both | Binary occupancy |
| Trigger Pressure | Threshold for occupancy detection |

### 3.4 Room Environment

| Sensor | Entity | Description |
|--------|--------|-------------|
| Bedroom Temperature | `sensor.bedroom_temperature_sensor_temperature` | Aqara room thermometer (°F) |

### 3.5 PostgreSQL Database (sleepdata on 192.168.0.3)

| Table | Records | Description |
|-------|---------|-------------|
| `controller_readings` | 2,023 (v5) | Every control decision + sensor snapshot |
| `health_metrics` | ~12,000+ | Apple Watch HR, HRV, RR, wrist temp |
| `sleep_segments` | 140 | Apple Watch sleep stages per night |
| `nightly_summary` | Per-night aggregates | |
| `daily_patterns` | | |
| `state_changes` | | |

---

## 4. ML Algorithm: LightGBM Gradient Boosted Trees

### 4.1 Why LightGBM?

| Requirement | LightGBM Fit |
|-------------|-------------|
| Small dataset (14 nights, ~2000 rows) | Excellent — trees handle small data with minimal overfitting via regularization |
| Non-linear relationships | Natural — decision trees partition feature space into regions |
| Mixed feature types | Native — handles continuous + categorical without encoding |
| Interpretable | Built-in feature importance; SHAP values available |
| Fast inference | Microseconds per prediction — runs in AppDaemon loop |
| Incremental learning | Retrain nightly on full dataset (milliseconds for this data size) |
| No hyperparameter sensitivity | Reasonable defaults work; no "decay rate" or "max adjustment" to tune |

### 4.2 Problem Formulation

**Bounded offset regression:** Given sensor context at time T, predict a **delta (offset) from a safe cycle baseline** that represents the user's comfort preference. This mirrors Eight Sleep's approach of "offsets from user comfort, not wholesale overrides."

```
final_setting = cycle_baseline[cycle_num] + model.predict(features)
```

The model predicts a bounded offset in [-5, +5] L1 steps, which is added to the hand-authored baseline schedule. This keeps the system anchored to known-good behavior while the ML learns personalized adjustments.

**Training labels:** Derived from override events with context-aware windowing:
- **Override events (primary signal):** When user changes setting, compute `delta = user_setting - cycle_baseline`. This is the ground truth label. **Weight: 3x.**
- **Post-override stabilization window (5-15 min after override):** The override value persists as label for a SHORT window only (not the entire night), capturing that the user is comfortable at that setting in that immediate context. **Weight: 2x.**
- **Pre-override discomfort window (5-10 min before override):** The setting that was active BEFORE the override is labeled as "wrong" — used as a negative signal. **Weight: 1x.**
- **No-override periods:** Treated as weakly labeled. Current `delta_from_baseline` is the label, but with **weight: 0.5x** (the user might be asleep/tolerating, not necessarily comfortable).
- **Label decay:** No-override labels beyond 30 minutes from the nearest override are down-weighted further (weight: 0.25x) to prevent the model from simply learning to replicate the previous controller.

### 4.3 Feature Engineering

#### Time Features
| Feature | Derivation | Rationale |
|---------|-----------|-----------|
| `elapsed_min` | Minutes since sleep onset | Primary cycle proxy |
| `cycle_num` | `floor(elapsed_min / 90) + 1` | Discrete cycle position |
| `cycle_phase` | `(elapsed_min % 90) / 90` | Position within current cycle (0.0–1.0) |
| `sin_cycle` | `sin(2π × elapsed_min / 90)` | Smooth cyclical encoding |
| `cos_cycle` | `cos(2π × elapsed_min / 90)` | Smooth cyclical encoding |
| `night_progress` | `elapsed_min / typical_night_duration` | 0.0–1.0 overall night position |

#### Body Temperature Features
| Feature | Derivation | Rationale |
|---------|-----------|-----------|
| `body_avg` | Mean of center/left/right sensors | Primary comfort signal |
| `body_center` | Center sensor raw | Core torso heat |
| `body_trend_5m` | Slope of body_avg over last 5 min | Rising = getting hotter |
| `body_trend_15m` | Slope over 15 min | Longer-term trend |
| `body_delta_from_entry` | Current body_avg - body_avg at sleep start | How much has body warmed |
| `body_max_last_30m` | Max body_avg in last 30 min | Recent peak (hot flash detection) |

#### Room/Environment Features
| Feature | Derivation | Rationale |
|---------|-----------|-----------|
| `room_temp` | Aqara sensor reading | Ambient cooling contribution |
| `room_trend_30m` | Room temp slope over 30 min | Room cooling/warming rate |
| `room_delta_from_start` | Room now vs room at sleep start | How much has room cooled overnight |
| `ambient_temp` | Topper surface ambient sensor | Microclimate near bed |

#### Current State Features
| Feature | Derivation | Rationale |
|---------|-----------|-----------|
| `setting_duration_min` | Minutes at current setting (capped at 60) | Thermal equilibrium indicator |
| `mean_setting_30m` | Average L1 over last 30 min | Recent thermal history |
| `recent_blower_avg` | Average blower % over last 15 min | Actuator state (thermal inertia) |

**Note:** Excluded from features: `current_setting`, `override_count_tonight`, `last_override_elapsed`. These are policy-confounded features that would cause the model to learn "repeat what v5 did" rather than learning actual comfort preferences.

#### Sleep Stage Features (from Apple Watch)
| Feature | Derivation | Rationale |
|---------|-----------|-----------|
| `sleep_stage` | Categorical: core/deep/rem/awake | Direct stage signal |
| `stage_duration_min` | How long in current stage | Stage depth |
| `deep_pct_so_far` | % of night in deep sleep so far | Deficit tracking |
| `rem_pct_so_far` | % of night in REM so far | Deficit tracking |

#### Bed Presence Features
| Feature | Derivation | Rationale |
|---------|-----------|-----------|
| `pressure_left` | Calibrated pressure | Movement/restlessness proxy |
| `pressure_variability_5m` | Std dev of pressure over 5 min | Tossing/turning indicator |
| `occupied_both` | Boolean: both sides occupied | Partner presence affects microclimate |

### 4.4 Validation Strategy

**Critical: Grouped leave-one-night-out cross-validation (LONO-CV).**

Because readings within a single night are temporally correlated, we MUST validate at the night level:

```
For each night N in dataset:
    train_set = all nights EXCEPT N
    test_set  = night N only
    model = train(train_set)
    predictions = model.predict(test_set)
    evaluate comfort_rate, override_prediction, RMSE on test_set
    
Report: mean ± std of metrics across held-out nights
```

**Deployment gate:** Model must beat BOTH of these on held-out nights:
1. v5 baseline (56.5% comfort rate)
2. A "dumb" baseline-only model (no ML, just cycle baselines + simple room compensation)

If the ML model doesn't beat both on held-out data, it is NOT ready to deploy.

**Rolling-origin validation (for production monitoring):**
After deployment, track the model's predictions BEFORE applying them vs what the user actually preferred. This gives an ongoing measure of model quality without needing A/B testing.

### 4.5 Training Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│                    NIGHTLY RETRAINING                            │
│                                                                 │
│  1. After sleep session ends (morning):                        │
│     ┌─────────┐    ┌──────────────┐    ┌─────────────────┐    │
│     │ Pull all │───▶│ Join sensor  │───▶│ Generate labels │    │
│     │ nights   │    │ readings +   │    │ from overrides  │    │
│     │ from PG  │    │ health data  │    │ + no-override   │    │
│     └─────────┘    └──────────────┘    └────────┬────────┘    │
│                                                   │             │
│  2. Feature engineering:                          ▼             │
│     ┌───────────────────────────────────────────────┐          │
│     │ Compute all features from raw sensor data     │          │
│     │ (time, body, room, stage, presence)            │          │
│     └───────────────────────┬───────────────────────┘          │
│                              │                                  │
│  3. Train LightGBM:         ▼                                  │
│     ┌───────────────────────────────────────────────┐          │
│     │ model = lgb.train(                            │          │
│     │   params={objective: 'regression',            │          │
│     │           num_leaves: 31,                     │          │
│     │           learning_rate: 0.05,                │          │
│     │           min_data_in_leaf: 5},               │          │
│     │   train_set=all_labeled_data,                 │          │
│     │   num_boost_round=100)                        │          │
│     └───────────────────────┬───────────────────────┘          │
│                              │                                  │
│  4. Save model:              ▼                                  │
│     ┌───────────────────────────────────────────────┐          │
│     │ model.save_model('/config/apps/snug_model.txt')│          │
│     └───────────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────────────┘
```

### 4.5 Inference Pipeline (Every 5 minutes during sleep)

```
┌──────────────────────────────────────────────────────────────────┐
│                    REAL-TIME INFERENCE                            │
│                                                                  │
│  Every 5 minutes:                                                │
│  ┌──────────┐   ┌────────────┐   ┌──────────┐   ┌───────────┐ │
│  │ Read all │──▶│ Compute    │──▶│ model    │──▶│ Compose   │ │
│  │ sensors  │   │ features   │   │.predict()│   │ + safety  │ │
│  └──────────┘   └────────────┘   └──────────┘   └─────┬─────┘ │
│                                       │                 │       │
│                              predicted offset           ▼       │
│                              (e.g., +2 steps)                   │
│                                                                  │
│  Composition:                                                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 1. base = cycle_baseline[cycle_num]         (e.g., -8)   │   │
│  │ 2. offset = model.predict(features)         (e.g., +2)   │   │
│  │ 3. offset *= confidence                     (e.g., ×0.8) │   │
│  │ 4. raw = base + offset                      (e.g., -6)   │   │
│  │ 5. Rate limit: -2/+1 max per 30 min (asymmetric)        │   │
│  │ 6. Override floor: never warmer than user's last         │   │
│  │ 7. Clamp to [-10, 0]                                     │   │
│  │ 8. Stale/bad sensor → fallback to base                   │   │
│  │ 9. Hot safety: body>85°F (2+ sensors) → force -10       │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### 4.6 Override Handling (Training Signal)

When the user manually changes the setting:

1. **Immediate:** Respect the override. Freeze controller for 60 minutes.
2. **Floor:** Override value becomes the minimum cooling for the rest of the night.
3. **Training signal:** Log the override as a labeled data point:
   - Features = sensor state at time of override
   - Label = user's chosen setting
   - Weight = 3x (overrides are 3x more informative than no-override readings)
4. **No filtering needed:** The ML model naturally handles conflicting signals (user sometimes wants warmer, sometimes cooler in same cycle) because it has access to the *full feature context* that explains *why* the preference differed.

### 4.7 Cold-Start Strategy

With 14 nights of existing data (~2000 labeled readings), we are NOT in a cold-start situation. The model has sufficient training data from day 1. However, for robustness:

1. **Days 1-3:** Model predictions are blended 50/50 with the v5 cycle baselines (safety net)
2. **Days 4-7:** Model confidence increases; blend shifts to 75% model / 25% baseline
3. **Days 8+:** Full model control (100% model prediction)
4. **Fallback:** If model file is missing/corrupt, revert to v5 cycle baselines

---

## 5. Model Confidence & Uncertainty

### 5.1 Support-Based Confidence

Rather than a fixed day-based blend schedule, confidence is determined by **how well the current context is represented in training data:**

- **Leaf sample count:** LightGBM provides the number of training samples in each prediction's leaf node. Low leaf count (< 3 samples) = low confidence.
- **Held-out residual magnitude:** Track prediction errors on held-out nights during LONO-CV. For feature regions with historically high error, reduce confidence.
- **Quantile spread:** Train two additional models (quantile=0.1 and quantile=0.9). When `model_high - model_low > 3 L1 steps`: uncertainty is high.

**Action when confidence is low:**
- Blend prediction with cycle baseline: `final_offset = confidence × model_offset + (1 - confidence) × 0`
- Where confidence ∈ [0.3, 1.0] based on leaf count: `confidence = min(1.0, leaf_count / 10)`

### 5.2 Cold-Start Blending

- **Days 1-3:** `confidence_cap = 0.5` (model can nudge at most ±2.5 L1 from baseline)
- **Days 4-7:** `confidence_cap = 0.75`
- **Days 8+:** `confidence_cap = 1.0` (full model authority within safety bounds)

This is a FLOOR on uncertainty, not a replacement for support-based confidence. The model can be more uncertain than the cap suggests, but never more confident.

### 5.3 Feature Importance Monitoring

After each nightly retrain, log top-5 feature importances. If feature rankings shift dramatically night-over-night, flag for review. Expected stable ranking:
1. `elapsed_min` / `cycle_num` (time-of-night dominates)
2. `room_temp` (environment)
3. `body_avg` (direct comfort signal)
4. `sleep_stage` (when available)
5. `body_trend` (predictive of upcoming discomfort)

---

## 6. Architecture & Deployment

### 6.1 System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Home Assistant (192.168.0.106)                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  AppDaemon                                                    │    │
│  │  ┌──────────────────────┐   ┌────────────────────────────┐  │    │
│  │  │  sleep_controller_v6 │   │  ml_trainer (daily cron)    │  │    │
│  │  │  • reads sensors     │   │  • pulls data from PG       │  │    │
│  │  │  • loads model       │   │  • trains LightGBM          │  │    │
│  │  │  • runs inference    │   │  • saves model file         │  │    │
│  │  │  • writes L1 setting │   │  • logs metrics             │  │    │
│  │  │  • logs to PG        │   └────────────────────────────┘  │    │
│  │  └──────────────────────┘                                     │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────┐  ┌────────────────┐  ┌──────────────────┐     │
│  │ PerfectlySnug   │  │ Apple Watch    │  │ Bed Presence     │     │
│  │ Topper (WS)     │  │ → Health Rcvr  │  │ Sensor (ESPHome) │     │
│  └─────────────────┘  └────────────────┘  └──────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘
         │                        │                    │
         ▼                        ▼                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PostgreSQL (192.168.0.3 — Mac Mini)                                 │
│  • controller_readings (sensor snapshots + decisions)                │
│  • health_metrics (HR, HRV, RR, wrist temp)                         │
│  • sleep_segments (Apple Watch sleep stages)                         │
│  • model_metrics (training logs, feature importances)                │
└─────────────────────────────────────────────────────────────────────┘
```

### 6.2 File Layout

```
PerfectlySnug/
├── appdaemon/
│   ├── sleep_controller_v6.py      # New ML-powered controller
│   ├── ml_trainer.py               # Nightly retraining app
│   └── apps.yaml                   # AppDaemon config
├── ml/
│   ├── features.py                 # Feature engineering functions
│   ├── training.py                 # Training pipeline
│   ├── inference.py                # Prediction + safety layer
│   └── evaluation.py              # Backtesting & metrics
├── tools/
│   ├── backtest_v5.py             # Existing v5 backtester
│   └── backtest_v6_ml.py         # New ML backtester
└── docs/
    └── ML_CONTROLLER_PRD.md       # This document
```

### 6.3 Dependencies

```
lightgbm>=4.0
numpy
pandas (for feature engineering)
psycopg2-binary (for PostgreSQL access)
scikit-learn (for train/test split, metrics)
```

---

## 7. Safety Constraints

| Constraint | Implementation |
|-----------|---------------|
| **Never override user** | Manual changes always take priority; 60-min freeze |
| **Rate-limiting (asymmetric)** | Cooling: max -2 L1 steps per 30 min (faster response to heat). Warming: max +1 L1 step per 30 min (slower, conservative). At bedtime (first 15 min): no rate limit (allow fast convergence to predicted setting) |
| **Override floor** | After manual change, controller cannot set warmer than user's override |
| **Hot safety** | If body_avg > 85°F for 2+ consecutive readings AND room_temp > 68°F, force max cooling. Require multi-sensor agreement (at least 2 of 3 body sensors above threshold) |
| **Stale data fallback** | If any primary sensor (body, room) is >10 minutes stale or returns out-of-range values, fall back to cycle baseline. If Apple Watch stage data is stale (>15 min), exclude stage features but continue with other inputs |
| **Sensor sanity** | Reject body readings outside 55-110°F, room readings outside 50-90°F. If sensors disagree by >10°F, use median and flag |
| **Bounded output** | Offset predictions clamped to [-5, +5]; final setting clamped to [-10, 0] |
| **Graceful degradation** | If model file missing/corrupt → fall back to v5 cycle baselines |
| **Kill switch** | 3 manual changes in 5 minutes → full manual mode for the night |
| **Local-only** | No cloud dependency; all inference runs on HA server |

---

## 8. Success Metrics & Evaluation

### 8.1 Primary Metrics

| Metric | Current (v5) | Target (wk 1-3) | Target (wk 4+) |
|--------|-------------|-----------------|----------------|
| Comfort rate (readings within ±1 of preference) | 56.5% | >70% | >85% |
| Override-free nights | 8.3% (1/12) | >25% | >50% |
| Average overrides per night | 2.8 | <2.0 | <1.0 |
| Mean directional bias | -0.53 (too warm) | ±0.2 | ±0.1 |
| Time to first override | 218 min | >300 min | >360 min (or none) |

### 8.2 Monitoring

After deployment, track nightly:
- Override count and direction
- Model prediction vs actual setting (RMSE)
- Feature importance stability
- Comfort rate per night
- Body temperature stability (std dev)

### 8.3 Rollback Criteria

Revert to v5 if within first 7 nights:
- Average overrides > 4/night (worse than v5)
- Any safety event (body temp > 95°F unaddressed)
- Model produces clearly wrong predictions (e.g., max cooling when room is 65°F)

---

## 9. Implementation Phases

### Phase 1: ML Training Pipeline
- Build feature engineering module
- Train initial model on 14 nights of v5 data
- Backtest against historical data (beat v5's 56.5% comfort)
- Validate predictions look sane

### Phase 2: Controller v6 (Inference)
- New AppDaemon app that loads model and runs predictions
- Safety layer (clamps, rate-limits, override handling)
- Cold-start blending with v5 baselines
- Logging to PostgreSQL (same schema + model-specific fields)

### Phase 3: Nightly Retraining
- Daily cron app that retrains after sleep session ends
- Feature importance logging
- Model versioning (keep last 7 models)

### Phase 4: Wife's Side Integration
- Apply same architecture to right side (192.168.0.211)
- Separate model per side (different thermal profiles)
- No override history for wife → use husband's model as warm-start, then adapt from her sensor patterns alone (use body temperature targets derived from her defaults)

---

## 10. Key Design Decisions

### Why not a neural network?
- 2000 training samples is too few for deep learning to outperform gradient boosting on tabular data
- Trees are more interpretable (feature importances are trivial to extract)
- Inference time is negligible for both, but tree training is faster with small data

### Why not online learning (updating per-reading)?
- Risk of catastrophic forgetting from a single bad night
- Nightly batch retraining is more stable and allows full-dataset context
- With 14+ nights of data, batch training takes <1 second anyway

### Why not reinforcement learning?
- RL requires many episodes (nights) to converge — months to years for sleep
- We already have labeled data (overrides = ground truth labels)
- Supervised learning converges much faster when you have labels

### Why predict L1 directly instead of a "comfort score"?
- L1 is what we actuate — direct mapping avoids a secondary translation layer
- Labels are naturally in L1 space (user overrides to a specific L1 value)
- The model can learn non-linear mappings from features → L1 internally

### How does this handle the "warmer vs cooler" contradiction?
- The current system oscillates because it stores a scalar per cycle with no context
- ML sees the FULL context: when room=69°F + body=80°F + cycle=4, the user wants -4. When room=73°F + body=80°F + cycle=4, the user wants -7. These are different regions in feature space — the tree naturally partitions them.

---

## 11. Example: How the Model Would Handle Known Failure Cases

### Case 1: Cold room + late night (user's "waking up cold" pattern)

**Current v5 behavior:** Cycle baseline = -7, room comp reduces by ~4% (one step warmer) = -6. Still too cold. User overrides to -4.

**ML prediction:** Model has seen multiple instances where `room_temp < 70 AND elapsed_min > 270 AND body_avg > 79` → user chose -4. It directly predicts -4.

### Case 2: Warm room + bedtime (user's "not cool enough" pattern)

**Current v5 behavior:** Cycle baseline = -10, but learned_adj = +30 (BUG: wrong direction), so effective = -2. User overrides to -8.

**ML prediction:** Model has seen that `elapsed_min < 90 AND room_temp > 72` → user always wants -8 to -10. Predicts -9.

### Case 3: Mid-night REM (need less cooling during REM)

**Current v5 behavior:** No stage awareness. Follows time-based cycle regardless of actual sleep stage.

**ML prediction:** With Apple Watch `sleep_stage=rem` feature, model learns that REM readings are associated with warmer preferences. Predicts 1-2 steps warmer than it would for the same time with `sleep_stage=deep`.

---

## 12. References

1. Eight Sleep Autopilot documentation — stage-based temperature offsets, minute-by-minute adjustment, adaptive magnitude based on prior-night deficits
2. Eight Sleep temperature controls blog — multi-phase schedule (Bedtime → Deep/Early → REM/Late → Pre-wake)
3. Moyen et al. (2024), PMC — Pod water temperature range ~13-43°C mapped to -10 to +10 dial
4. Kräuchi (2007), PMC 3427038 — Sleep thermoregulation, bed microclimate, stage-dependent mechanisms
5. Eight Sleep deep learning sleep algorithm blog — per-minute staging, 78% accuracy vs PSG
6. How Eight Sleep Implements Dynamic Bed-Temperature Control.pdf — comprehensive analysis of Eight Sleep's control architecture, patent evidence, and practical implications for air-based topper implementation

---

*Document version: 1.0 — 2026-04-29*
*Author: Copilot ML Controller Design Agent*
*Status: Ready for implementation*
