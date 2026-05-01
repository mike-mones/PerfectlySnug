# Right-Side Responsive Cooling — Time-Alignment Audit

**Date:** 2026-05-01  
**Window:** 2026-04-17 11:39:24 UTC → 2026-04-30 20:35:30 UTC  
**Filter:** `switch.smart_topper_right_side_responsive_cooling = on` AND `switch.smart_topper_right_side_running = on`  
**Source:** `/tmp/snug_v2.json` (HA REST `/api/history` with `minimal_response`)  
**Coverage after filter:** 13 nightly intervals, **158.75 h** total

## TL;DR

1. **Recorder value-change events are emitted by a single ~31 s poller in lockstep.** Body, blower, ambient, and setpoint all show median Δt = 31.03 s with identical p10/p25/p75 values. "Lead/lag" inferred from HA event timestamps is therefore mostly a **polling artifact**, not sub-second firmware timing.
2. **No evidence that 30 s forward-fill is pairing a new blower with stale body sensors.** At blower-change events the nearest body/ambient event is effectively simultaneous (typically 0–20 ms earlier, p90 ≈ one 31 s tick when the sensor value did not change).
3. **Resolution does not rescue predictive power.** Time-series CV with all five predictors gives essentially the same best R² at 5/10/30/60 s; the best tested model is a 60 s Ridge/linear model at **R²≈0.277**. Gradient boosting is worse out-of-sample.
4. **Lagged predictors / lead targets make results worse.** Best 30 s-grid result is lag 0 at **R²≈0.276**; 30–300 s lags/target leads monotonically degrade the linear model.
5. **Diagnostic correlations are stronger than TS-CV models but are not stable predictors.** In-sample single-feature sweeps show setpoint R²≈0.40 and ambient R²≈0.17, while body sensors remain ≤0.10.
6. **Blower duty quantization:** observed values are integers 0 then 10–100. **No values 1–9** → firmware enforces a 10 % minimum-on duty when not at zero. 173 zero↔nonzero transitions over 13 nights (≈13 cycles/night).

## Entity inventory (right side)

| Role | Entity |
|---|---|
| Blower output (%) | `sensor.smart_topper_right_side_blower_output` |
| Body sensor left | `sensor.smart_topper_right_side_body_sensor_left` |
| Body sensor center | `sensor.smart_topper_right_side_body_sensor_center` |
| Body sensor right | `sensor.smart_topper_right_side_body_sensor_right` |
| Ambient (onboard) | `sensor.smart_topper_right_side_ambient_temperature` |
| Setpoint (firmware target °F) | `sensor.smart_topper_right_side_temperature_setpoint` |
| RC switch | `switch.smart_topper_right_side_responsive_cooling` |
| Running switch | `switch.smart_topper_right_side_running` |

## Native inter-arrival cadence (RC+running window)

| Entity | n | median Δt | p10 | p25 | p75 | p90 | mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| blower | 8 219 | **31.03 s** | 30.91 | 30.94 | 31.12 | 62.05 | 130.2 |
| body_l | 15 198 | 31.03 s | 30.91 | 30.94 | 31.09 | 61.98 | 71.0 |
| body_c | 15 185 | 31.03 s | 30.91 | 30.94 | 31.09 | 61.99 | 71.1 |
| body_r | 15 093 | 31.03 s | 30.91 | 30.94 | 31.09 | 62.00 | 71.5 |
| ambient | 15 789 | 31.02 s | 30.91 | 30.94 | 31.06 | 61.95 | 68.4 |
| setpoint | 2 679 | 31.02 s | 30.91 | 30.93 | 31.05 | 61.94 | 403.1 |

Bucketed inter-arrivals (s):

| entity | <1 | 1–2 | 2–5 | 5–10 | 10–15 | 15–20 | 20–30 | **30–45** | 45–60 | 60–120 | 120–300 | >300 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| blower | 0 | 2 | 6 | 5 | 4 | 1 | 18 | **6 573** | 9 | 1 392 | 130 | 78 |
| body_l | 0 | 6 | 20 | 18 | 21 | 4 | 33 | **12 585** | 9 | 2 346 | 139 | 16 |
| body_c | 0 | 4 | 16 | 15 | 18 | 3 | 32 | **12 596** | 6 | 2 321 | 161 | 12 |
| body_r | 0 | 6 | 15 | 16 | 19 | 4 | 29 | **12 484** | 7 | 2 327 | 171 | 14 |
| ambient | 0 | 5 | 20 | 20 | 20 | 3 | 30 | **13 525** | 9 | 2 050 | 94 | 12 |
| setpoint | 0 | 0 | 2 | 2 | 5 | 1 | 6 | **2 351** | 1 | 270 | 13 | 27 |

**Interpretation:** Mass at 30–45 s = primary 30 s poll/value-change cycle. Mass at 60–120 s = unchanged samples are not present in this history export (and/or HA recorder records only state changes), so these are not guaranteed raw firmware polls. Blower has ~half the body-channel events because blower is a small integer and changes less often per tick. Setpoint is even sparser (firmware updates target slowly).

## Cross-entity timing alignment

### Native lead/lag (each blower event → nearest preceding predictor event, s)

| predictor | n | median | p25 | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| body_l | 8 219 | **0.00** | 0.00 | 0.01 | 31.03 |
| body_c | 8 219 | **0.01** | 0.00 | 0.01 | 31.03 |
| body_r | 8 219 | **0.01** | 0.00 | 0.01 | 31.03 |
| ambient | 8 219 | **0.01** | 0.01 | 0.01 | 31.00 |
| setpoint | 8 219 | 15 968 | 6 645 | 25 495 | 31 298 |

**Body/ambient/blower fire in the same 30 s tick.** The body/ambient rows are usually milliseconds before blower in the same poll bucket; p90 = 31 s only when that sensor value was unchanged on the current tick. This rules out using HA event timestamps to infer sub-poll causality. Setpoint medians at ~4.4 h because setpoint values stay frozen across thousands of ticks.

### Time-series CV by sampling strategy

Model target is `blower[t]`; predictors are `body_l`, `body_c`, `body_r`, `ambient`, and `setpoint` at the same grid timestamp. Scores are 5-fold `TimeSeriesSplit` R². Linear = RidgeCV with scaling; GB = `HistGradientBoostingRegressor`.

| strategy | n | linear R² | GB R² | best |
|---|---:|---:|---:|---:|
| 5 s forward-fill | 114 297 | **0.276** | 0.050 | 0.276 |
| 10 s forward-fill | 57 150 | **0.276** | 0.046 | 0.276 |
| 30 s forward-fill | 19 051 | **0.276** | 0.060 | 0.276 |
| 60 s forward-fill | 9 525 | **0.277** | 0.161 | **0.277** |
| native blower events + last-known sensors | 8 219 | **−0.738** | −0.996 | −0.738 |

**Conclusion:** finer grids mostly duplicate the same 31 s value-change stream and do not add information. The best tested operational choice is **60 s + linear/Ridge** at R²≈0.277. The important preprocessing detail is not 5 s vs 30 s; it is initializing each nightly interval from the last-known pre-window sensor/setpoint values before forward-fill.

### Predictor lag / target lead sweep (30 s grid, all predictors)

Positive predictor lag means `blower[t] ~ sensors[t-lag]`. Positive target lead means `blower[t+lead] ~ sensors[t]`; with a regular grid those tests are mathematically equivalent after dropping edge rows.

| seconds | predictor-lag best R² | target-lead best R² |
|---:|---:|---:|
| 0 | **0.276** | **0.276** |
| 30 | 0.274 | 0.274 |
| 60 | 0.272 | 0.272 |
| 90 | 0.268 | 0.268 |
| 120 | 0.264 | 0.264 |
| 180 | 0.255 | 0.255 |
| 240 | 0.246 | 0.246 |
| 300 | 0.237 | 0.237 |
| 600 | 0.184 | 0.184 |
| 900 | 0.133 | 0.133 |
| 1 200 | 0.084 | 0.084 |
| 1 800 | −0.009 | −0.009 |

**Conclusion:** there is no useful stale-reading delay. The best lag/lead is zero; moving sensors or targets later only degrades performance.

### Latency: predictor change → next blower change (s)

| predictor | n | median | p25 | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| body_l → blower | 15 103 | 31.0 | 0.0 | 3 317 | 14 845 |
| body_c → blower | 15 079 | 31.0 | 0.0 | 3 286 | 14 402 |
| body_r → blower | 14 985 | 31.0 | 0.0 | 3 192 | 14 679 |

Median 31 s (one tick) again confirms lockstep polling. Long-tail p75/p90 reflect periods where blower stays pinned (often at 100 %) while body keeps drifting.

### Resampled lag-sweep R² (predict blower from predictor at lag, lag ∈ [−300, +300] s)

| freq | predictor | best_lag (s) | best R² | R²@0 | n |
|---|---|---:|---:|---:|---:|
| 5 s | body_l | −125 | 0.093 | 0.090 | 104 703 |
| 5 s | body_c |    0 | 0.034 | 0.034 | 104 793 |
| 5 s | body_r |  +65 | 0.045 | 0.045 | 104 778 |
| 5 s | ambient | **−300** | **0.170** | 0.165 | 104 676 |
| 5 s | setpoint | −10 | **0.400** | 0.399 | 104 768 |
| 10 s | body_l | −120 | 0.093 | 0.090 | 52 353 |
| 10 s | ambient | −300 | 0.170 | 0.165 | 52 340 |
| 10 s | setpoint | −10 | 0.399 | 0.399 | 52 386 |
| 30 s | body_l | −120 | 0.093 | 0.090 | 17 455 |
| 30 s | ambient | −300 | 0.170 | 0.165 | 17 450 |
| 30 s | setpoint |   0 | 0.399 | 0.399 | 17 468 |
| 60 s | body_l | −120 | 0.093 | 0.090 | 8 730 |
| 60 s | ambient | −300 | 0.170 | 0.166 | 8 726 |
| 60 s | setpoint |   0 | 0.400 | 0.400 | 8 734 |

(Negative lag = predictor leads blower; sweep was capped at −300 s so ambient may have a deeper optimum past that bound.)

**Findings:**
- **Setpoint dominates** (R² = 0.40, peaks at lag 0 / −10 s). The firmware's commanded target is the proximate cause of blower duty; body sensors are upstream of setpoint, not blower.
- **Ambient leads blower by ≥ 5 min** with R² ≈ 0.17. Sweep is at boundary, true peak likely deeper. Indicates thermal-mass coupling (warm ambient → sustained higher duty later).
- **Body-left leads blower by ~120 s** but with weak R² (0.09).
- **Body-center is contemporaneous** with blower (best lag 0, R² 0.034) — consistent with `body_c` being the channel firmware hugs.
- **Body-right lags blower by ~60 s** (its peak is at +60 s, R² 0.045) — i.e. body_r reflects what the blower already did.
- **Resampling rate is irrelevant** — R² values are nearly identical across 5/10/30/60 s grids, confirming the underlying signal is a 30 s tick stream and finer interpolation adds no information.

### Native event-stream R²

Blower (each event) ~ last-known predictor:

| predictor | R² | n |
|---|---:|---:|
| body_l  | 0.007 | 8 219 |
| body_c  | 0.098 | 8 219 |
| body_r  | 0.014 | 8 219 |
| ambient | **0.214** | 8 219 |
| setpoint | **0.201** | 8 219 |

Lead form (next blower at body event):

| predictor | R² | n |
|---|---:|---:|
| body_l → next blower | 0.018 | 15 103 |
| body_c → next blower | 0.013 | 15 079 |
| body_r → next blower | 0.006 | 14 985 |

Native event-stream is dominated by samples where blower is pinned at 100 %. In time-series CV with all predictors, the native event model scored R² = −0.738 despite the in-sample single-feature correlations above, so it is not the best predictive representation.

## Quantization & hysteresis

- **Blower distribution:** 174 zero samples, 8 045 nonzero. **Min nonzero = 10**, max = 100. No values 1–9 observed → firmware floors duty at 10 % once on.
- **Transitions:** 172 zero→nonzero, 173 nonzero→zero across 158.75 h ⇒ ≈ 13 on-cycles per night, with off durations short (most zeros are transient).
- **Setpoint quantization:** 658 unique values in 68.198–87.8 °F, irregular spacing (0.072 °F or 0.09 °F deltas) — firmware reports float, but step changes cluster.
- **Sensor ranges (window):**
  - body_l 68.20–90.82 °F, median 77.90 (presence-active distribution)
  - body_c 67.84–89.40 °F, median **85.41** (hottest channel — under torso)
  - body_r 67.37–88.36 °F, median 79.57
  - ambient 67.01–91.94 °F, median 71.69
  - setpoint 68.20–87.80 °F

**No evidence of explicit hysteresis bands** beyond the 10 % minimum-duty floor — firmware appears to PI-modulate continuously while RC is on, with brief off-pulses.

## Reproducible snippet

```python
import json, numpy as np, pandas as pd
from sklearn.base import clone
from sklearn.impute import SimpleImputer
from sklearn.linear_model import RidgeCV
from sklearn.metrics import r2_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
d = json.load(open('/tmp/snug_v2.json'))
streams = {s[0]['entity_id']: s for s in d if s}

def to_df(stream, numeric=True):
    rows = [(r.get('last_changed') or r.get('last_updated'), r['state']) for r in stream]
    df = pd.DataFrame(rows, columns=['t','v'])
    df['t'] = pd.to_datetime(df['t'], utc=True, format='ISO8601')
    if numeric: df['v'] = pd.to_numeric(df['v'], errors='coerce')
    return df.dropna(subset=['t']).sort_values('t').reset_index(drop=True)

t0 = pd.Timestamp('2026-04-17 11:39:24', tz='UTC')
t1 = pd.Timestamp('2026-04-30 20:35:30', tz='UTC')
rc  = to_df(streams['switch.smart_topper_right_side_responsive_cooling'], numeric=False)
run = to_df(streams['switch.smart_topper_right_side_running'], numeric=False)

def state_intervals(df, on='on', tmax=t1):
    out=[]; cur=None
    for _,r in df.iterrows():
        if r['v']==on and cur is None: cur=r['t']
        elif r['v']!=on and cur is not None: out.append((cur,r['t'])); cur=None
    if cur is not None: out.append((cur,tmax))
    return out

def isect(a,b):
    return [(max(s1,s2),min(e1,e2))
            for s1,e1 in a for s2,e2 in b if max(s1,s2) < min(e1,e2)]

both = isect(state_intervals(rc), state_intervals(run))
both = [(max(s,t0), min(e,t1)) for s,e in both if max(s,t0) < min(e,t1)]

ents = {
    'blower':'sensor.smart_topper_right_side_blower_output',
    'body_l':'sensor.smart_topper_right_side_body_sensor_left',
    'body_c':'sensor.smart_topper_right_side_body_sensor_center',
    'body_r':'sensor.smart_topper_right_side_body_sensor_right',
    'ambient':'sensor.smart_topper_right_side_ambient_temperature',
    'setpoint':'sensor.smart_topper_right_side_temperature_setpoint',
}
def in_window(df, ivs):
    m = pd.Series(False, index=df.index)
    for s,e in ivs: m |= (df['t']>=s)&(df['t']<=e)
    return df[m].reset_index(drop=True)

# Keep full streams so each nightly interval can be initialized from the
# last-known value before the RC+running start.
dfs = {k: to_df(streams[v]) for k,v in ents.items()}

def resample(df, freq, ivs):
    parts=[]
    for s,e in ivs:
        sub = df[(df['t']>=s-pd.Timedelta('12h'))&(df['t']<=e)].drop_duplicates('t').set_index('t')['v']
        idx = pd.date_range(s.ceil(freq), e, freq=freq, tz='UTC')
        if len(sub) and len(idx):
            parts.append(sub.reindex(sub.index.union(idx)).ffill().reindex(idx))
    return pd.concat(parts) if parts else pd.Series(dtype=float)

# Best tested operational model: 60 s grid, all predictors, time-series CV.
def design(freq='60s', predictor_lag_s=0):
    y = resample(dfs['blower'], freq, both)
    step = pd.Timedelta(freq).total_seconds()
    X = pd.DataFrame(index=y.index)
    for name in ['body_l','body_c','body_r','ambient','setpoint']:
        x = resample(dfs[name], freq, both).reindex(y.index)
        X[name] = x.shift(int(round(predictor_lag_s / step)))
    data = X.assign(y=y).dropna()
    return data.drop(columns='y'), data['y']

X, y = design('60s', predictor_lag_s=0)
model = make_pipeline(
    SimpleImputer(strategy='median'),
    StandardScaler(),
    RidgeCV(alphas=np.logspace(-4, 4, 20)),
)
scores = []
for train, test in TimeSeriesSplit(n_splits=5).split(X):
    m = clone(model).fit(X.iloc[train], y.iloc[train])
    scores.append(r2_score(y.iloc[test], m.predict(X.iloc[test])))
print(f"60s Ridge TimeSeriesSplit R² = {np.mean(scores):.3f}  folds={scores}")
```

## Implications for controller modeling

- **Do not model blower as a function of body sensors alone** — diagnostic R² ceiling is ~0.10. Setpoint is the strongest single diagnostic correlate, but all-sensor TS-CV is still only modest (R²≈0.28), so setpoint/body inputs are better aimed at understanding firmware intent than predicting exact blower duty.
- **Aggregation grids of 5/10/30/60 s are equally informative.** No need to ingest raw 31 s ticks for response modeling — 60 s grid is the sweet spot among tested grids (matches polling, halves storage vs 30 s).
- **Ambient's univariate diagnostic peak is at the −300 s sweep boundary**, but all-feature TS-CV with long uniform lags got worse. Treat ambient lag as a hypothesis to test in a richer model, not as a proven blower predictor.
- **Body_l leads body_r by ~3 min** in the blower-correlation peak (−120 vs +60 s). If you want a "fastest-responding body channel," prefer **body_l** for early-warning, **body_c** for steady-state contact, **body_r** for confirmation.
- **Quantization floor of 10 %** must be honored in any synthetic blower target.
- **Treat 30–45 s and 60–120 s buckets as the same "tick"** — the 60–120 cluster is just `minimal_response` deduplication, not a slower mode.

## Caveats / what's missing

- **`/tmp/snug_v2.json` extends only to 2026-05-01 19:55 UTC**; the audit window itself ends 2026-04-30 20:35:30 UTC, fully covered.
- **Setpoint stream is sparser** (n=2 679 vs ~15 k for sensors) — lockstep cadence still holds, but firmware emits setpoint changes only when its internal target moves.
- **Lag sweep capped at ±300 s.** Ambient's optimum sits at the −300 s boundary — true ambient → blower lead may be 5–15 min. A follow-up sweep to −1800 s is recommended.
- **No body-presence gating applied.** `sensor.bed_presence_2bcab8_right_pressure` exists in the dump (n=7 730) but starts on 2026-04-21; folding it in could remove empty-bed noise from body sensor R² calculations.
- **Right-side data only**, per task. Left-side (AppDaemon-managed) not audited here.
