#!/usr/bin/env python3
"""
Deep ML analysis of PerfectlySnug Responsive Cooling algorithm.
Predicts blower_output (0-100%) from sensor data.
Uses TimeSeriesSplit CV to avoid temporal leakage.
"""
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone
import warnings
warnings.filterwarnings('ignore')

# ─── 1. LOAD & PARSE DATA ───────────────────────────────────────────────────
print("=" * 70)
print("LOADING DATA...")
with open('/tmp/snug_v2.json') as f:
    data = json.load(f)

entity_map = {}
for group in data:
    if isinstance(group, list) and len(group) > 0:
        eid = group[0].get('entity_id', '')
        records = []
        for item in group:
            records.append({
                'state': item['state'],
                'ts': pd.to_datetime(item['last_changed'])
            })
        entity_map[eid] = pd.DataFrame(records).sort_values('ts').reset_index(drop=True)

# Short names
BLOWER = 'sensor.smart_topper_right_side_blower_output'
SETPOINT = 'sensor.smart_topper_right_side_temperature_setpoint'
AMBIENT = 'sensor.smart_topper_right_side_ambient_temperature'
BODY_C = 'sensor.smart_topper_right_side_body_sensor_center'
BODY_L = 'sensor.smart_topper_right_side_body_sensor_left'
BODY_R = 'sensor.smart_topper_right_side_body_sensor_right'
RC_SWITCH = 'switch.smart_topper_right_side_responsive_cooling'
RUNNING = 'switch.smart_topper_right_side_running'
PRESSURE = 'sensor.bed_presence_2bcab8_right_pressure'

# ─── 2. BUILD UNIFIED TIMELINE ──────────────────────────────────────────────
print("Building unified timeline...")

# Resample all sensors to 30s grid using forward-fill
freq = '30s'
start_ts = pd.Timestamp('2026-04-17 11:39:24', tz='UTC')
end_ts = pd.Timestamp('2026-04-30 20:35:30', tz='UTC')
grid = pd.date_range(start_ts, end_ts, freq=freq)

def to_numeric_series(eid, grid):
    df = entity_map[eid].copy()
    df['val'] = pd.to_numeric(df['state'], errors='coerce')
    df = df.dropna(subset=['val']).set_index('ts').sort_index()
    df = df[~df.index.duplicated(keep='last')]
    # Reindex to grid with ffill
    return df['val'].reindex(grid, method='ffill')

def to_switch_series(eid, grid):
    df = entity_map[eid].copy()
    df['val'] = df['state'].map({'on': 1, 'off': 0}).astype(float)
    df = df.dropna(subset=['val']).set_index('ts').sort_index()
    df = df[~df.index.duplicated(keep='last')]
    return df['val'].reindex(grid, method='ffill')

unified = pd.DataFrame(index=grid)
unified['blower'] = to_numeric_series(BLOWER, grid)
unified['setpoint'] = to_numeric_series(SETPOINT, grid)
unified['ambient'] = to_numeric_series(AMBIENT, grid)
unified['body_c'] = to_numeric_series(BODY_C, grid)
unified['body_l'] = to_numeric_series(BODY_L, grid)
unified['body_r'] = to_numeric_series(BODY_R, grid)
unified['rc_on'] = to_switch_series(RC_SWITCH, grid)
unified['running'] = to_switch_series(RUNNING, grid)

# Pressure - numeric where possible
if PRESSURE in entity_map:
    df_p = entity_map[PRESSURE].copy()
    df_p['val'] = pd.to_numeric(df_p['state'], errors='coerce')
    df_p = df_p.dropna(subset=['val']).set_index('ts').sort_index()
    df_p = df_p[~df_p.index.duplicated(keep='last')]
    unified['pressure'] = df_p['val'].reindex(grid, method='ffill')
else:
    unified['pressure'] = np.nan

# ─── 3. FILTER: RC ON & RUNNING ON ──────────────────────────────────────────
print("Filtering to RC-on & running-on periods...")
mask = (unified['rc_on'] == 1) & (unified['running'] == 1)
df = unified[mask].copy()
df = df.dropna(subset=['blower', 'setpoint', 'body_c'])
print(f"  After filter: {len(df)} rows, time range: {df.index.min()} → {df.index.max()}")

# ─── 4. FEATURE ENGINEERING ─────────────────────────────────────────────────
print("Engineering features...")

# Body stats
df['body_max'] = df[['body_c', 'body_l', 'body_r']].max(axis=1)
df['body_min'] = df[['body_c', 'body_l', 'body_r']].min(axis=1)
df['body_avg'] = df[['body_c', 'body_l', 'body_r']].mean(axis=1)
df['body_spread'] = df['body_max'] - df['body_min']

# Key differences
df['body_max_minus_setpoint'] = df['body_max'] - df['setpoint']
df['body_avg_minus_setpoint'] = df['body_avg'] - df['setpoint']
df['body_c_minus_setpoint'] = df['body_c'] - df['setpoint']
df['body_max_minus_ambient'] = df['body_max'] - df['ambient']
df['ambient_minus_setpoint'] = df['ambient'] - df['setpoint']

# Lags and rolling windows
lag_seconds = [30, 60, 150, 300, 600, 1800]  # 30s steps: 1,2,5,10,20,60
for lag_s in lag_seconds:
    lag_steps = lag_s // 30
    suffix = f'_lag{lag_s}s'
    df[f'body_max{suffix}'] = df['body_max'].shift(lag_steps)
    df[f'blower{suffix}'] = df['blower'].shift(lag_steps)
    df[f'diff_max_sp{suffix}'] = df['body_max_minus_setpoint'].shift(lag_steps)

# Rolling windows
for window_s in [60, 300, 600, 1800, 3600]:
    w = window_s // 30
    suffix = f'_roll{window_s}s'
    df[f'body_max_mean{suffix}'] = df['body_max'].rolling(w, min_periods=1).mean()
    df[f'body_max_std{suffix}'] = df['body_max'].rolling(w, min_periods=1).std().fillna(0)
    df[f'body_max_min{suffix}'] = df['body_max'].rolling(w, min_periods=1).min()
    df[f'body_max_max{suffix}'] = df['body_max'].rolling(w, min_periods=1).max()
    df[f'blower_mean{suffix}'] = df['blower'].rolling(w, min_periods=1).mean()
    df[f'diff_mean{suffix}'] = df['body_max_minus_setpoint'].rolling(w, min_periods=1).mean()

# Rate of change
df['body_max_roc_60s'] = df['body_max'].diff(2)  # 60s rate
df['body_max_roc_300s'] = df['body_max'].diff(10)  # 5min rate
df['blower_roc_60s'] = df['blower'].diff(2)

# Time features
df['hour'] = df.index.hour + df.index.minute / 60.0
df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)

# Pressure (occupancy proxy)
df['pressure_valid'] = df['pressure'].notna().astype(int)

# Drop rows with NaN in critical features (due to lags)
df = df.dropna(subset=['body_max_lag1800s', 'blower_lag1800s'])
print(f"  After feature engineering: {len(df)} rows")

# ─── 5. PREPARE FOR MODELING ────────────────────────────────────────────────
target = 'blower'
exclude_cols = ['blower', 'rc_on', 'running', 'pressure', 'hour']
# Also exclude blower lags to avoid trivial leakage via autocorrelation
# Actually keep them - blower lags are legitimate features (system state)
feature_cols = [c for c in df.columns if c not in exclude_cols]
print(f"  Features: {len(feature_cols)}")

X = df[feature_cols].values
y = df[target].values

# ─── 6. MODEL TRAINING WITH TIMESERIESSPLIT ─────────────────────────────────
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
import lightgbm as lgb
try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    HAS_XGB = False

tscv = TimeSeriesSplit(n_splits=5)

def evaluate_model(model, X, y, name):
    r2s, maes = [], []
    for train_idx, test_idx in tscv.split(X):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
        model.fit(X_train, y_train)
        preds = model.predict(X_test)
        preds = np.clip(preds, 0, 100)
        r2s.append(r2_score(y_test, preds))
        maes.append(mean_absolute_error(y_test, preds))
    print(f"  {name}: R²={np.mean(r2s):.4f}±{np.std(r2s):.4f}, MAE={np.mean(maes):.2f}±{np.std(maes):.2f}")
    return np.mean(r2s), np.mean(maes), r2s, maes

print("\n" + "=" * 70)
print("MODEL EVALUATION (TimeSeriesSplit, 5 folds)")
print("=" * 70)

results = {}

# 6a. LightGBM
lgb_model = lgb.LGBMRegressor(
    n_estimators=500, learning_rate=0.05, max_depth=8,
    num_leaves=63, min_child_samples=20,
    subsample=0.8, colsample_bytree=0.8,
    reg_alpha=0.1, reg_lambda=1.0,
    verbose=-1, n_jobs=-1
)
r2, mae, r2s, maes = evaluate_model(lgb_model, X, y, "LightGBM")
results['LightGBM'] = (r2, mae)

# 6b. HistGradientBoosting
hgb_model = HistGradientBoostingRegressor(
    max_iter=500, learning_rate=0.05, max_depth=8,
    min_samples_leaf=20, l2_regularization=1.0
)
r2, mae, r2s, maes = evaluate_model(hgb_model, X, y, "HistGBT")
results['HistGBT'] = (r2, mae)

# 6c. XGBoost
if HAS_XGB:
    xgb_model = xgb.XGBRegressor(
        n_estimators=500, learning_rate=0.05, max_depth=8,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.1, reg_lambda=1.0, verbosity=0, n_jobs=-1
    )
    r2, mae, r2s, maes = evaluate_model(xgb_model, X, y, "XGBoost")
    results['XGBoost'] = (r2, mae)

# 6d. Ridge (linear baseline)
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
ridge_pipe = Pipeline([('scaler', StandardScaler()), ('ridge', Ridge(alpha=10.0))])
r2, mae, r2s, maes = evaluate_model(ridge_pipe, X, y, "Ridge")
results['Ridge'] = (r2, mae)

# ─── 7. FEATURE IMPORTANCE (BEST MODEL) ─────────────────────────────────────
print("\n" + "=" * 70)
print("FEATURE IMPORTANCE (LightGBM, full data)")
print("=" * 70)

lgb_model.fit(X, y)
importances = lgb_model.feature_importances_
feat_imp = pd.Series(importances, index=feature_cols).sort_values(ascending=False)
print("\nTop 20 features:")
for feat, imp in feat_imp.head(20).items():
    print(f"  {feat}: {imp}")

# ─── 8. PARTIAL DEPENDENCE ANALYSIS ─────────────────────────────────────────
print("\n" + "=" * 70)
print("PARTIAL DEPENDENCE ANALYSIS")
print("=" * 70)

# Key feature: body_max_minus_setpoint
key_feat = 'body_max_minus_setpoint'
if key_feat in feature_cols:
    idx = feature_cols.index(key_feat)
    # Manual PD: vary this feature, fix others at median
    X_median = np.median(X, axis=0).reshape(1, -1).repeat(50, axis=0)
    feat_range = np.linspace(X[:, idx].min(), X[:, idx].max(), 50)
    X_median[:, idx] = feat_range
    pd_preds = lgb_model.predict(X_median)
    print(f"\n  Partial dependence of blower on {key_feat}:")
    for val, pred in zip(feat_range[::10], pd_preds[::10]):
        print(f"    {key_feat}={val:+.1f}°F → blower≈{pred:.1f}%")

# Also check blower lag
key_feat2 = 'blower_lag30s'
if key_feat2 in feature_cols:
    idx2 = feature_cols.index(key_feat2)
    X_median2 = np.median(X, axis=0).reshape(1, -1).repeat(50, axis=0)
    feat_range2 = np.linspace(0, 100, 50)
    X_median2[:, idx2] = feat_range2
    pd_preds2 = lgb_model.predict(X_median2)
    print(f"\n  Partial dependence of blower on {key_feat2}:")
    for val, pred in zip(feat_range2[::10], pd_preds2[::10]):
        print(f"    {key_feat2}={val:.1f}% → blower≈{pred:.1f}%")

# ─── 9. TEST WITHOUT BLOWER LAGS (no autocorrelation leakage) ───────────────
print("\n" + "=" * 70)
print("MODEL WITHOUT BLOWER LAGS (pure causal features)")
print("=" * 70)

causal_cols = [c for c in feature_cols if 'blower' not in c.lower() or 'blower' not in c]
# More careful: remove any feature with 'blower' in name
causal_cols = [c for c in feature_cols if 'blower' not in c]
print(f"  Causal features: {len(causal_cols)}")

X_causal = df[causal_cols].values

lgb_causal = lgb.LGBMRegressor(
    n_estimators=500, learning_rate=0.05, max_depth=8,
    num_leaves=63, min_child_samples=20,
    subsample=0.8, colsample_bytree=0.8, verbose=-1, n_jobs=-1
)
r2_c, mae_c, _, _ = evaluate_model(lgb_causal, X_causal, y, "LightGBM (no blower lags)")
results['LightGBM_causal'] = (r2_c, mae_c)

# Feature importance for causal model
lgb_causal.fit(X_causal, y)
feat_imp_causal = pd.Series(lgb_causal.feature_importances_, index=causal_cols).sort_values(ascending=False)
print("\nTop 15 causal features:")
for feat, imp in feat_imp_causal.head(15).items():
    print(f"  {feat}: {imp}")

# ─── 10. SIMPLE PARAMETRIC FIT ──────────────────────────────────────────────
print("\n" + "=" * 70)
print("PARAMETRIC APPROXIMATION SEARCH")
print("=" * 70)

# Try: blower = clip(a * (body_max - setpoint) + b, 0, 100)
from scipy.optimize import minimize

def parametric_linear(params, X_df):
    a, b = params
    return np.clip(a * X_df['body_max_minus_setpoint'].values + b, 0, 100)

def parametric_loss(params, X_df, y):
    pred = parametric_linear(params, X_df)
    return np.mean((pred - y) ** 2)

res = minimize(parametric_loss, [5.0, 50.0], args=(df, y), method='Nelder-Mead')
a_opt, b_opt = res.x
pred_param = parametric_linear(res.x, df)
r2_param = r2_score(y, pred_param)
mae_param = mean_absolute_error(y, pred_param)
print(f"\n  Linear: blower = clip({a_opt:.3f} * (body_max - setpoint) + {b_opt:.3f}, 0, 100)")
print(f"  R²={r2_param:.4f}, MAE={mae_param:.2f}")

# Try: blower = clip(a * (body_max - setpoint) + b * (body_max - ambient) + c, 0, 100)
def parametric_2feat(params, X_df):
    a, b, c = params
    return np.clip(
        a * X_df['body_max_minus_setpoint'].values + 
        b * X_df['body_max_minus_ambient'].values + c, 0, 100)

def parametric_2feat_loss(params, X_df, y):
    pred = parametric_2feat(params, X_df)
    return np.mean((pred - y) ** 2)

res2 = minimize(parametric_2feat_loss, [5.0, 2.0, 50.0], args=(df, y), method='Nelder-Mead')
a2, b2, c2 = res2.x
pred_param2 = parametric_2feat(res2.x, df)
r2_param2 = r2_score(y, pred_param2)
mae_param2 = mean_absolute_error(y, pred_param2)
print(f"\n  2-feat: blower = clip({a2:.3f}*(body_max-setpoint) + {b2:.3f}*(body_max-ambient) + {c2:.3f}, 0, 100)")
print(f"  R²={r2_param2:.4f}, MAE={mae_param2:.2f}")

# Try piecewise/nonlinear: blower = clip(a * max(0, body_max - setpoint - threshold) + base, 0, 100)
def parametric_piecewise(params, X_df):
    a, threshold, base = params
    diff = X_df['body_max_minus_setpoint'].values
    return np.clip(a * np.maximum(0, diff - threshold) + base, 0, 100)

def parametric_pw_loss(params, X_df, y):
    pred = parametric_piecewise(params, X_df)
    return np.mean((pred - y) ** 2)

res3 = minimize(parametric_pw_loss, [8.0, 2.0, 30.0], args=(df, y), method='Nelder-Mead')
pred_param3 = parametric_piecewise(res3.x, df)
r2_param3 = r2_score(y, pred_param3)
mae_param3 = mean_absolute_error(y, pred_param3)
print(f"\n  Piecewise: blower = clip({res3.x[0]:.3f} * max(0, (body_max-setpoint) - {res3.x[1]:.3f}) + {res3.x[2]:.3f}, 0, 100)")
print(f"  R²={r2_param3:.4f}, MAE={mae_param3:.2f}")

# Try with smoothing/momentum: current blower is a weighted avg of "target" and previous blower
# This is an IIR filter: blower[t] = alpha * target[t] + (1-alpha) * blower[t-1]
# Where target = clip(a * (body_max - setpoint) + b, 0, 100)
# Simulate this forward
def parametric_iir(params, X_df):
    a, b, alpha = params
    alpha = np.clip(alpha, 0.01, 1.0)
    target = np.clip(a * X_df['body_max_minus_setpoint'].values + b, 0, 100)
    blower_sim = np.zeros(len(target))
    blower_sim[0] = target[0]
    for i in range(1, len(target)):
        blower_sim[i] = alpha * target[i] + (1 - alpha) * blower_sim[i - 1]
    return blower_sim

def parametric_iir_loss(params, X_df, y):
    pred = parametric_iir(params, X_df)
    return np.mean((pred - y) ** 2)

res4 = minimize(parametric_iir_loss, [5.0, 50.0, 0.1], args=(df, y), method='Nelder-Mead',
                options={'maxiter': 5000})
pred_param4 = parametric_iir(res4.x, df)
r2_param4 = r2_score(y, pred_param4)
mae_param4 = mean_absolute_error(y, pred_param4)
a4, b4, alpha4 = res4.x
print(f"\n  IIR filter: blower[t] = {np.clip(alpha4,0,1):.4f} * clip({a4:.3f}*(body_max-setpoint)+{b4:.3f}, 0,100) + {1-np.clip(alpha4,0,1):.4f} * blower[t-1]")
print(f"  R²={r2_param4:.4f}, MAE={mae_param4:.2f}")

# ─── 11. RESIDUAL ANALYSIS ──────────────────────────────────────────────────
print("\n" + "=" * 70)
print("RESIDUAL ANALYSIS (best parametric model)")
print("=" * 70)

# Use IIR model residuals
best_param_pred = pred_param4 if r2_param4 > r2_param else pred_param
best_param_name = "IIR" if r2_param4 > r2_param else "Linear"
residuals = y - best_param_pred

print(f"  Using {best_param_name} model residuals")
print(f"  Residual stats: mean={residuals.mean():.2f}, std={residuals.std():.2f}, "
      f"min={residuals.min():.1f}, max={residuals.max():.1f}")

# Correlation of residuals with features
print("\n  Residual correlations with key features:")
for col in ['body_max_minus_setpoint', 'ambient_minus_setpoint', 'body_spread', 
            'body_max_roc_60s', 'body_max_roc_300s', 'hour_sin', 'hour_cos']:
    if col in df.columns:
        corr = np.corrcoef(residuals, df[col].values)[0, 1]
        if not np.isnan(corr):
            print(f"    {col}: r={corr:.3f}")

# ─── 12. SUMMARY ────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("SUMMARY OF ALL RESULTS")
print("=" * 70)
print(f"\n{'Model':<30} {'R²':>8} {'MAE':>8}")
print("-" * 50)
for name, (r2, mae) in sorted(results.items(), key=lambda x: -x[1][0]):
    print(f"  {name:<28} {r2:>8.4f} {mae:>8.2f}")

print(f"\n{'Parametric Approximations':<30} {'R²':>8} {'MAE':>8}")
print("-" * 50)
print(f"  {'Linear (body_max-sp)':<28} {r2_param:>8.4f} {mae_param:>8.2f}")
print(f"  {'2-feature':<28} {r2_param2:>8.4f} {mae_param2:>8.2f}")
print(f"  {'Piecewise':<28} {r2_param3:>8.4f} {mae_param3:>8.2f}")
print(f"  {'IIR filter':<28} {r2_param4:>8.4f} {mae_param4:>8.2f}")

# ─── 13. DISTRIBUTIONAL CHECK ───────────────────────────────────────────────
print("\n" + "=" * 70)
print("DISTRIBUTIONAL SHIFT CHECK (by night)")
print("=" * 70)

df['night'] = (df.index.date).astype(str)
for night, grp in df.groupby('night'):
    if len(grp) > 100:
        pred_grp = lgb_causal.predict(grp[causal_cols].values)
        r2_grp = r2_score(grp['blower'].values, pred_grp)
        mae_grp = mean_absolute_error(grp['blower'].values, pred_grp)
        print(f"  {night}: n={len(grp):>5}, R²={r2_grp:.4f}, MAE={mae_grp:.2f}")

print("\nDone!")
