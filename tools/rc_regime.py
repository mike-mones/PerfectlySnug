"""
PerfectlySnug RC Firmware: Per-Regime Decomposition Analysis
============================================================
"""

import json
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor, export_text
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# 1. LOAD AND PREPARE DATA
# =============================================================================

print("Loading data...")
with open('/tmp/snug_v2.json') as f:
    raw = json.load(f)

def parse_series(records):
    rows = []
    for r in records:
        try:
            val = float(r['state'])
            ts = pd.to_datetime(r['last_changed'])
            rows.append((ts, val))
        except (ValueError, TypeError):
            continue
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=['time', 'value']).set_index('time').sort_index()
    return df['value']

entity_map = {}
for series in raw:
    if len(series) > 0:
        eid = series[0]['entity_id']
        entity_map[eid] = parse_series(series)

blower = entity_map['sensor.smart_topper_right_side_blower_output']
ambient = entity_map['sensor.smart_topper_right_side_ambient_temperature']
body_center = entity_map['sensor.smart_topper_right_side_body_sensor_center']
body_left = entity_map['sensor.smart_topper_right_side_body_sensor_left']
body_right = entity_map['sensor.smart_topper_right_side_body_sensor_right']
setpoint = entity_map['sensor.smart_topper_right_side_temperature_setpoint']

freq = '1min'
df = pd.DataFrame({
    'blower': blower.resample(freq).mean(),
    'ambient': ambient.resample(freq).mean(),
    'body_center': body_center.resample(freq).mean(),
    'body_left': body_left.resample(freq).mean(),
    'body_right': body_right.resample(freq).mean(),
    'setpoint': setpoint.resample(freq).ffill(),
}).dropna()

rc_start = pd.Timestamp('2026-04-17 11:39:24', tz='UTC')
rc_end = pd.Timestamp('2026-04-30 20:35:30', tz='UTC')
df = df[(df.index >= rc_start) & (df.index <= rc_end)]
print(f"Dataset: {len(df)} rows, {df.index[0]} to {df.index[-1]}")

# =============================================================================
# 2. FEATURE ENGINEERING
# =============================================================================

df['body_max'] = df[['body_center', 'body_left', 'body_right']].max(axis=1)
df['body_mean'] = df[['body_center', 'body_left', 'body_right']].mean(axis=1)
df['body_min'] = df[['body_center', 'body_left', 'body_right']].min(axis=1)
df['error'] = df['body_max'] - df['setpoint']
df['ambient_error'] = df['ambient'] - df['setpoint']
df['body_spread'] = df['body_max'] - df['body_min']

# Rate of change
df['body_max_roc'] = df['body_max'].diff(5) / 5.0
df['ambient_roc'] = df['ambient'].diff(5) / 5.0
df['blower_lag1'] = df['blower'].shift(1)
df['blower_lag5'] = df['blower'].shift(5)
df['error_lag5'] = df['error'].shift(5)
df['blower_diff'] = df['blower'].diff(1)

df = df.dropna()
print(f"After features: {len(df)} rows")

# =============================================================================
# 3. REGIME IDENTIFICATION (Multiple methods)
# =============================================================================

print("\n=== REGIME IDENTIFICATION ===")

# Method A: Blower-output bins (for analysis, not for prediction)
bins = [0, 5, 15, 35, 65, 100]
labels = ['idle', 'low', 'moderate', 'high', 'saturated']
df['regime_blower'] = pd.cut(df['blower'], bins=bins, labels=labels, include_lowest=True)
print("\nBlower-bin regime distribution:")
print(df['regime_blower'].value_counts())

# Method B: Thermal-state regime (deterministic from sensors - usable for prediction)
# Use error AND blower_lag as proxy for "what state is the system currently in"
# The firmware uses feedback control, so blower_lag is observable state
def thermal_regime(row):
    error = row['error']
    blower_prev = row['blower_lag1']
    
    if blower_prev <= 5:
        return 'idle'
    elif blower_prev <= 5 and error > 1.0:
        return 'trigger'  # about to ramp up
    elif error > 3.0:
        return 'hot_active'
    elif error > 1.0:
        return 'warm_active'
    elif error > -0.5:
        return 'comfort_maintain'
    else:
        return 'cold_rundown'

# Better approach: use blower dynamics to identify regimes
# Regimes from blower behavior patterns
df['blower_accel'] = df['blower_diff'].diff(1)

def dynamic_regime(row):
    """Regime from observable system state (blower_lag + error + dynamics)"""
    bl = row['blower_lag1']
    err = row['error']
    diff = row['blower_diff'] if not np.isnan(row['blower_diff']) else 0
    
    if bl <= 3:
        return 'idle'
    elif diff > 1.0:
        return 'ramping_up'
    elif diff < -1.0:
        return 'ramping_down'
    elif bl > 60:
        return 'saturated'
    elif err > 2.0:
        return 'active_cooling'
    else:
        return 'holding'

df['regime_dynamic'] = df.apply(dynamic_regime, axis=1)
print("\nDynamic regime distribution:")
print(df['regime_dynamic'].value_counts())

# Method C: K-means clustering on state vectors
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

cluster_features = ['error', 'blower_lag1', 'body_max_roc', 'ambient']
X_clust = StandardScaler().fit_transform(df[cluster_features].values)
km = KMeans(n_clusters=5, random_state=42, n_init=10).fit(X_clust)
df['regime_cluster'] = km.labels_
print("\nCluster regime distribution:")
print(df['regime_cluster'].value_counts())

# Characterize clusters
for c in range(5):
    mask = df['regime_cluster'] == c
    print(f"  Cluster {c}: blower={df.loc[mask,'blower'].mean():.1f}%, "
          f"error={df.loc[mask,'error'].mean():.2f}°F, "
          f"blower_lag={df.loc[mask,'blower_lag1'].mean():.1f}%")

# =============================================================================
# 4. TRANSITION ANALYSIS 
# =============================================================================

print("\n=== TRANSITION ANALYSIS ===")

df['regime_prev'] = df['regime_dynamic'].shift(1)
transitions = df[df['regime_dynamic'] != df['regime_prev']].dropna(subset=['regime_prev'])
print(f"Total transitions: {len(transitions)}")

from collections import Counter
trans_counts = Counter(zip(transitions['regime_prev'].astype(str), 
                          transitions['regime_dynamic'].astype(str)))
print("\nTop transitions:")
for (fr, to), cnt in sorted(trans_counts.items(), key=lambda x: -x[1])[:12]:
    print(f"  {fr:16s} -> {to:16s}: {cnt}")

# Transition trigger conditions
print("\nTransition trigger analysis (idle -> ramping_up):")
t_idle_ramp = transitions[(transitions['regime_prev']=='idle') & 
                          (transitions['regime_dynamic']=='ramping_up')]
if len(t_idle_ramp) > 0:
    print(f"  n={len(t_idle_ramp)}")
    print(f"  error: {t_idle_ramp['error'].describe()[['mean','min','max']].to_dict()}")
    print(f"  body_max: {t_idle_ramp['body_max'].mean():.1f}")

print("\nTransition trigger (holding -> ramping_up):")
t_hold_ramp = transitions[(transitions['regime_prev']=='holding') & 
                          (transitions['regime_dynamic']=='ramping_up')]
if len(t_hold_ramp) > 0:
    print(f"  n={len(t_hold_ramp)}, error_mean={t_hold_ramp['error'].mean():.2f}")

print("\nTransition trigger (active_cooling -> holding):")
t_active_hold = transitions[(transitions['regime_prev']=='active_cooling') & 
                            (transitions['regime_dynamic']=='holding')]
if len(t_active_hold) > 0:
    print(f"  n={len(t_active_hold)}, error_mean={t_active_hold['error'].mean():.2f}, "
          f"blower_lag={t_active_hold['blower_lag1'].mean():.1f}")

# =============================================================================
# 5. PER-REGIME MODEL FITTING
# =============================================================================

print("\n=== PER-REGIME MODEL FITTING ===")

features = ['error', 'ambient_error', 'body_max', 'body_mean', 'ambient',
            'setpoint', 'body_max_roc', 'ambient_roc', 'body_spread',
            'blower_lag1', 'blower_lag5', 'error_lag5']

target = 'blower'
X_all = df[features].values
y_all = df[target].values

# Global baseline
tscv = TimeSeriesSplit(n_splits=5)
global_maes, global_r2s = [], []
for train_idx, test_idx in tscv.split(X_all):
    model = GradientBoostingRegressor(n_estimators=200, max_depth=4, 
                                       learning_rate=0.05, random_state=42)
    model.fit(X_all[train_idx], y_all[train_idx])
    pred = model.predict(X_all[test_idx])
    global_maes.append(mean_absolute_error(y_all[test_idx], pred))
    global_r2s.append(r2_score(y_all[test_idx], pred))

print(f"\nGLOBAL MODEL: MAE={np.mean(global_maes):.2f}%, R²={np.mean(global_r2s):.4f}")

# Per-regime models (dynamic regime)
regime_names = df['regime_dynamic'].unique()
regime_results = {}

for regime in sorted(regime_names):
    mask = df['regime_dynamic'] == regime
    n = mask.sum()
    if n < 30:
        continue
    
    X_r = df.loc[mask, features].values
    y_r = df.loc[mask, target].values
    
    # In-sample linear
    lr = LinearRegression().fit(X_r, y_r)
    lr_r2 = r2_score(y_r, lr.predict(X_r))
    
    # TS-CV with GB
    n_splits = min(5, max(2, n // 80))
    tscv_r = TimeSeriesSplit(n_splits=n_splits)
    maes, r2s = [], []
    for train_idx, test_idx in tscv_r.split(X_r):
        if len(train_idx) < 20:
            continue
        gb = GradientBoostingRegressor(n_estimators=100, max_depth=3, 
                                        learning_rate=0.05, random_state=42)
        gb.fit(X_r[train_idx], y_r[train_idx])
        pred = gb.predict(X_r[test_idx])
        maes.append(mean_absolute_error(y_r[test_idx], pred))
        r2s.append(r2_score(y_r[test_idx], pred))
    
    gb_mae = np.mean(maes) if maes else np.nan
    gb_r2 = np.mean(r2s) if r2s else np.nan
    
    regime_results[regime] = {
        'n': n, 'blower_mean': y_r.mean(), 'blower_std': y_r.std(),
        'lr_r2': lr_r2, 'gb_mae': gb_mae, 'gb_r2': gb_r2
    }
    print(f"  {regime:16s}: n={n:4d}, blower={y_r.mean():.1f}±{y_r.std():.1f}%, "
          f"LR_R²={lr_r2:.3f}, GB MAE={gb_mae:.2f} R²={gb_r2:.4f}")

# =============================================================================
# 6. STATE MACHINE: Decision Tree for regime transitions + per-regime equations
# =============================================================================

print("\n=== STATE MACHINE EXTRACTION ===")

# Train decision tree to predict blower output bin from sensor-only features
# (excluding blower_lag to avoid circularity for "from scratch" prediction)
sensor_only_features = ['error', 'ambient_error', 'body_max', 'ambient', 
                        'body_max_roc', 'body_spread', 'setpoint']

# But actually firmware IS a feedback controller, so it DOES use previous blower
# The state machine uses blower_lag as "current state" input
state_features = ['error', 'ambient_error', 'blower_lag1', 'body_max_roc', 
                  'body_spread', 'ambient']

X_dt = df[state_features].values
y_dt = df['blower'].values

# Regression tree as interpretable model
dt_reg = DecisionTreeRegressor(max_depth=6, min_samples_leaf=30, random_state=42)
dt_reg.fit(X_dt, y_dt)
dt_r2 = r2_score(y_dt, dt_reg.predict(X_dt))
dt_mae = mean_absolute_error(y_dt, dt_reg.predict(X_dt))
print(f"Decision Tree Regressor: R²={dt_r2:.4f}, MAE={dt_mae:.2f}%")
print("\nRegression Tree Rules:")
tree_text = export_text(dt_reg, feature_names=state_features, max_depth=6)
print(tree_text[:4000])

# =============================================================================
# 7. COMBINED PER-REGIME TS-CV EVALUATION
# =============================================================================

print("\n=== COMBINED PER-REGIME MODEL (TS-CV) ===")

combined_maes, combined_r2s = [], []
tscv = TimeSeriesSplit(n_splits=5)

for fold, (train_idx, test_idx) in enumerate(tscv.split(X_all)):
    df_train = df.iloc[train_idx]
    df_test = df.iloc[test_idx]
    
    predictions = np.full(len(test_idx), np.nan)
    
    for regime in sorted(regime_names):
        train_mask = df_train['regime_dynamic'] == regime
        test_mask = df_test['regime_dynamic'] == regime
        
        if train_mask.sum() < 20 or test_mask.sum() < 3:
            continue
        
        X_tr = df_train.loc[train_mask, features].values
        y_tr = df_train.loc[train_mask, target].values
        X_te = df_test.loc[test_mask, features].values
        
        gb = GradientBoostingRegressor(n_estimators=150, max_depth=4,
                                        learning_rate=0.05, random_state=42)
        gb.fit(X_tr, y_tr)
        
        test_positions = np.where(test_mask.values)[0]
        predictions[test_positions] = gb.predict(X_te)
    
    # Fill NaN with global model
    nan_mask = np.isnan(predictions)
    if nan_mask.any():
        gb_global = GradientBoostingRegressor(n_estimators=200, max_depth=4,
                                              learning_rate=0.05, random_state=42)
        gb_global.fit(X_all[train_idx], y_all[train_idx])
        predictions[nan_mask] = gb_global.predict(X_all[test_idx][nan_mask])
    
    mae = mean_absolute_error(y_all[test_idx], predictions)
    r2 = r2_score(y_all[test_idx], predictions)
    combined_maes.append(mae)
    combined_r2s.append(r2)
    print(f"  Fold {fold+1}: MAE={mae:.2f}%, R²={r2:.4f}")

print(f"\n  COMBINED PER-REGIME: MAE={np.mean(combined_maes):.2f}%, R²={np.mean(combined_r2s):.4f}")
print(f"  GLOBAL SINGLE:      MAE={np.mean(global_maes):.2f}%, R²={np.mean(global_r2s):.4f}")

# =============================================================================
# 8. FINAL FEATURE IMPORTANCE & EQUATIONS
# =============================================================================

print("\n=== FINAL PER-REGIME EQUATIONS ===")

# Fit linear models per regime for interpretability
for regime in sorted(regime_names):
    mask = df['regime_dynamic'] == regime
    if mask.sum() < 30:
        continue
    X_r = df.loc[mask, features].values
    y_r = df.loc[mask, target].values
    
    lr = LinearRegression().fit(X_r, y_r)
    coefs = pd.Series(lr.coef_, index=features)
    sig_coefs = coefs[coefs.abs() > 0.5].sort_values(key=abs, ascending=False)
    
    print(f"\n  {regime} (n={mask.sum()}, blower_avg={y_r.mean():.1f}%):")
    print(f"    blower ≈ {lr.intercept_:.1f}", end="")
    for feat, coef in sig_coefs.head(5).items():
        print(f" + {coef:.2f}*{feat}", end="")
    print()

# =============================================================================
# 9. SUMMARY
# =============================================================================

print("\n" + "="*70)
print("FINAL SUMMARY")
print("="*70)
print(f"Dataset: {len(df)} samples, 1-min resolution")
print(f"Regimes: {sorted(regime_names)}")
print(f"\nModel Performance (5-fold TS-CV):")
print(f"  Global GB:        MAE = {np.mean(global_maes):.2f}%  R² = {np.mean(global_r2s):.4f}")
print(f"  Per-Regime GB:    MAE = {np.mean(combined_maes):.2f}%  R² = {np.mean(combined_r2s):.4f}")
print(f"  Decision Tree:    MAE = {dt_mae:.2f}%  R² = {dt_r2:.4f} (in-sample)")
improvement = (np.mean(global_maes) - np.mean(combined_maes)) / np.mean(global_maes) * 100
print(f"  Regime improvement: {improvement:.1f}% MAE reduction")

# Store for report generation
import pickle
results = {
    'global_mae': np.mean(global_maes), 'global_r2': np.mean(global_r2s),
    'combined_mae': np.mean(combined_maes), 'combined_r2': np.mean(combined_r2s),
    'dt_mae': dt_mae, 'dt_r2': dt_r2,
    'regime_results': regime_results,
    'tree_text': tree_text,
    'n_samples': len(df),
    'improvement_pct': improvement,
}
with open('/tmp/rc_regime_results.pkl', 'wb') as f:
    pickle.dump(results, f)

print("\nAnalysis complete.")
