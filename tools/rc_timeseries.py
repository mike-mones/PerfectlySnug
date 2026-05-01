"""
RC Deep Time-Series Analysis for PerfectlySnug topper.

Approach: ARMAX / Transfer-Function / Granger / VAR / Kalman state-space
to model blower_output as a function of body sensors, ambient, setpoint.
"""
import json
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

DATA_PATH = "/tmp/snug_v2.json"
RC_ON_START = pd.Timestamp("2026-04-17 11:39:24", tz="UTC")
RC_ON_END   = pd.Timestamp("2026-04-30 20:35:30", tz="UTC")

def load_series(path):
    raw = json.load(open(path))
    out = {}
    for s in raw:
        eid = s[0]["entity_id"]
        ts, vals = [], []
        for pt in s:
            t = pt.get("last_changed") or pt.get("last_updated")
            v = pt.get("state")
            if t is None or v in (None, "unknown", "unavailable", ""):
                continue
            try:
                v = float(v)
            except Exception:
                # for switches keep string
                pass
            ts.append(pd.Timestamp(t))
            vals.append(v)
        out[eid] = pd.Series(vals, index=pd.DatetimeIndex(ts).tz_convert("UTC"),
                             name=eid).sort_index()
    return out

def resample_align(series_dict, freq="30s"):
    df = pd.DataFrame()
    for k, s in series_dict.items():
        is_switch = k.startswith("switch.")
        if is_switch:
            s = s.map(lambda x: 1 if x == "on" else 0).astype(float)
            df[k] = s.resample(freq).ffill()
        else:
            s = pd.to_numeric(s, errors="coerce").dropna()
            df[k] = s.resample(freq).mean().ffill()
    return df

print("Loading data ...")
series = load_series(DATA_PATH)
df = resample_align(series, freq="30s")

# rename for convenience
ren = {
    "sensor.smart_topper_right_side_temperature_setpoint": "setpoint",
    "sensor.smart_topper_right_side_ambient_temperature": "ambient",
    "sensor.smart_topper_right_side_blower_output": "blower",
    "sensor.smart_topper_right_side_body_sensor_center": "body_c",
    "sensor.smart_topper_right_side_body_sensor_left": "body_l",
    "sensor.smart_topper_right_side_body_sensor_right": "body_r",
    "switch.smart_topper_right_side_responsive_cooling": "rc_on",
    "switch.smart_topper_right_side_running": "running",
    "number.smart_topper_right_side_bedtime_temperature": "bedtime_setting",
    "sensor.bed_presence_2bcab8_right_pressure": "pressure",
}
df = df.rename(columns=ren)

# Filter RC window + RC on + running on
mask = (df.index >= RC_ON_START) & (df.index <= RC_ON_END)
df = df.loc[mask].copy()
df["rc_on"] = df["rc_on"].fillna(0).round()
df["running"] = df["running"].fillna(0).round()
df = df[(df["rc_on"] == 1) & (df["running"] == 1)].copy()

# Derived
df["body_max"] = df[["body_c", "body_l", "body_r"]].max(axis=1)
df["body_avg"] = df[["body_c", "body_l", "body_r"]].mean(axis=1)
df["body_min"] = df[["body_c", "body_l", "body_r"]].min(axis=1)
# "setting" = setpoint (RC-mode setting)
df["setting"] = df["setpoint"]
df["err_max"] = df["body_max"] - df["setting"]
df["err_avg"] = df["body_avg"] - df["setting"]
df["err_amb"] = df["ambient"] - df["setting"]

# drop early NaNs
need = ["blower","body_max","body_avg","ambient","setting","err_max","err_avg"]
df = df.dropna(subset=need).copy()
print(f"Filtered rows: {len(df)}; range {df.index.min()} -> {df.index.max()}")
print(df[need].describe().round(3))

# ---------- Stationarity / ACF look ---------- #
from statsmodels.tsa.stattools import adfuller, grangercausalitytests
adf_blow = adfuller(df["blower"].values, maxlag=20, regression="c")
print(f"\nADF blower p-value: {adf_blow[1]:.4g}")

# ---------- Granger causality ---------- #
print("\n--- Granger causality (does X help predict blower?) ---")
gc_results = {}
for col in ["err_max","err_avg","body_max","body_avg","ambient","setting"]:
    try:
        sub = df[["blower", col]].dropna().values
        # subsample for speed
        if len(sub) > 5000:
            sub = sub[::max(1,len(sub)//5000)]
        gc = grangercausalitytests(sub, maxlag=5, verbose=False)
        pvals = [gc[l][0]["ssr_ftest"][1] for l in range(1,6)]
        gc_results[col] = pvals
        print(f"  {col:10s}  p(F) lags1-5 = {[f'{p:.3g}' for p in pvals]}")
    except Exception as e:
        print(f"  {col}: {e}")

# ---------- ARMAX / SARIMAX ---------- #
from statsmodels.tsa.statespace.sarimax import SARIMAX

# choose exogenous set
EXOG = ["err_max","err_avg","ambient","setting"]
y = df["blower"].astype(float).values
X = df[EXOG].astype(float).values

# Walk-forward split: 70% train, 30% test
n = len(y)
split = int(n*0.7)
y_tr, y_te = y[:split], y[split:]
X_tr, X_te = X[:split], X[split:]

print(f"\nTrain {len(y_tr)}, Test {len(y_te)}")

best = None
candidates = [(p,0,q) for p in (1,2,3) for q in (0,1,2)]
print("\n--- ARMAX order search (AIC) ---")
for order in candidates:
    try:
        mod = SARIMAX(y_tr, exog=X_tr, order=order,
                      enforce_stationarity=False, enforce_invertibility=False)
        res = mod.fit(disp=False, method="lbfgs", maxiter=80)
        print(f"  ARMAX{order} AIC={res.aic:.1f} BIC={res.bic:.1f}")
        if best is None or res.aic < best[1]:
            best = (order, res.aic, res)
    except Exception as e:
        print(f"  ARMAX{order} failed: {e}")

best_order, best_aic, best_res = best
print(f"\nBest ARMAX order: {best_order}  AIC={best_aic:.1f}")
print(best_res.summary().tables[1])

# Walk-forward forecast
fc = best_res.forecast(steps=len(y_te), exog=X_te)
mae = np.mean(np.abs(fc - y_te))
ss_res = np.sum((y_te-fc)**2); ss_tot = np.sum((y_te-y_te.mean())**2)
r2 = 1 - ss_res/ss_tot
print(f"\nARMAX walk-forward: MAE={mae:.2f}%  R2={r2:.3f}")

# Re-fit on all data for reporting coefficients
full_res = SARIMAX(y, exog=X, order=best_order,
                   enforce_stationarity=False, enforce_invertibility=False)\
                  .fit(disp=False, method="lbfgs", maxiter=120)
print("\n--- Full-data ARMAX coefficients ---")
print(full_res.summary().tables[1])
ARMAX_PARAMS = dict(zip(full_res.param_names, full_res.params))

# ---------- VAR ---------- #
from statsmodels.tsa.api import VAR
print("\n--- VAR(blower, body_max, ambient, setting) ---")
var_df = df[["blower","body_max","ambient","setting"]].astype(float)
# ensure stationarity by differencing setting+ambient slowly varying? Use levels.
var_mod = VAR(var_df.values).fit(maxlags=8, ic="aic")
print(f"VAR selected lag order: {var_mod.k_ar}")
print("VAR Granger summary (blower caused by ...):")
for col in ["body_max","ambient","setting"]:
    try:
        gc = var_mod.test_causality("blower", [col], kind="f")
        print(f"  {col}: F={gc.test_statistic:.2f}  p={gc.pvalue:.3g}")
    except Exception as e:
        print(f"  {col}: {e}")
# IRFs at 30s steps -> minutes
irf = var_mod.irf(20)
print("\nImpulse-response of blower to a 1-unit shock in body_max (lags 0..10):")
print(np.round(irf.irfs[:11, 0, 1], 3))
print("Impulse-response of blower to a 1-unit shock in ambient (lags 0..10):")
print(np.round(irf.irfs[:11, 0, 2], 3))

# ---------- Kalman state-space: hidden integral state ---------- #
# Hypothesis: blower_t = a*err_max_t + b*I_t  ; I_{t+1} = I_t + k*err_max_t
# Express as state-space and let Kalman recover I_t and the gains.
print("\n--- Kalman / unobserved-component state-space ---")
from statsmodels.tsa.statespace.mlemodel import MLEModel

class IntegralBlower(MLEModel):
    """
    State: [I_t]  (hidden integrator on err_max)
    Transition: I_{t+1} = I_t + k * err_max_t   (err_max enters via state intercept)
    Observation: blower_t = c0 + c1 * err_max_t + c2 * ambient_t + c3 * setting_t + b * I_t + eps
    """
    start_params = [0.05, 50.0, 5.0, 0.0, 0.0, 1.0, 0.5, 0.5]
    param_names  = ["k","c0","c1_err","c2_amb","c3_set","b_I","sigma_obs","sigma_state"]

    def __init__(self, endog, exog):
        # exog cols: err_max, ambient, setting
        super().__init__(endog, k_states=1, exog=exog,
                         initialization="approximate_diffuse")
        self.exog_arr = np.asarray(exog, dtype=float)
        # transition: I_{t+1} = 1*I_t + (k*err_max_t)  -> via state_intercept time-varying
        self["transition", 0, 0] = 1.0
        self["selection", 0, 0] = 1.0
        # design will be filled in update
        self["design", 0, 0] = 1.0  # b_I placeholder
        self.ssm._time_invariant = False

    def update(self, params, **kwargs):
        params = super().update(params, **kwargs)
        k, c0, c1, c2, c3, b, so, ss = params
        n = self.nobs
        err = self.exog_arr[:,0]; amb = self.exog_arr[:,1]; setn = self.exog_arr[:,2]
        # state intercept (k_states, nobs)
        st_int = np.zeros((1, n))
        st_int[0,:] = k * err
        self["state_intercept"] = st_int
        # observation intercept (k_endog, nobs)
        ob_int = np.zeros((1, n))
        ob_int[0,:] = c0 + c1*err + c2*amb + c3*setn
        self["obs_intercept"] = ob_int
        self["design", 0, 0] = b
        self["obs_cov", 0, 0] = so**2
        self["state_cov", 0, 0] = ss**2

    def transform_params(self, p):
        p = np.array(p, dtype=float)
        p[6] = np.exp(p[6]); p[7] = np.exp(p[7]); return p
    def untransform_params(self, p):
        p = np.array(p, dtype=float)
        p[6] = np.log(max(p[6],1e-6)); p[7] = np.log(max(p[7],1e-6)); return p

kal_exog = df[["err_max","ambient","setting"]].astype(float).values
y_full   = df["blower"].astype(float).values

# fit on train portion, evaluate on test
kal_train = IntegralBlower(y_tr, kal_exog[:split])
kal_res   = kal_train.fit(disp=False, maxiter=200)
print("Kalman train log-lik:", float(kal_res.llf))
for n_, p_ in zip(kal_res.model.param_names, kal_res.params):
    print(f"  {n_:12s} = {p_:+.4f}")

# Evaluate on held-out test using filtered state propagation
# Build full model with fitted params and apply
kal_full_mod = IntegralBlower(y_full, kal_exog)
kal_full_mod.update(kal_res.params, transformed=True)
filt = kal_full_mod.filter(kal_res.params)
pred = filt.fittedvalues
mae_kal = np.mean(np.abs(pred[split:] - y_te))
ss_res = np.sum((y_te-pred[split:])**2); ss_tot = np.sum((y_te-y_te.mean())**2)
r2_kal = 1 - ss_res/ss_tot
print(f"Kalman walk-forward: MAE={mae_kal:.2f}%  R2={r2_kal:.3f}")

# Hidden integrator path summary
I_path = filt.filtered_state[0]
print(f"Hidden state I_t  range [{I_path.min():.2f}, {I_path.max():.2f}]")

# ---------- Naive baselines ---------- #
# (1) static linear regression
from sklearn.linear_model import LinearRegression
lr = LinearRegression().fit(X_tr, y_tr)
mae_lr = np.mean(np.abs(lr.predict(X_te)-y_te))
r2_lr = lr.score(X_te, y_te)
print(f"\nBaseline linreg : MAE={mae_lr:.2f}%  R2={r2_lr:.3f}  coefs={dict(zip(EXOG, np.round(lr.coef_,3)))} intercept={lr.intercept_:.2f}")

# (2) persistence (predict y_t = y_{t-1})
pers = np.r_[y_te[0], y_te[:-1]]
mae_pers = np.mean(np.abs(pers-y_te))
print(f"Baseline persistence MAE={mae_pers:.2f}%")

# ---------- Cross-correlation: lag from err_max change to blower change ---------- #
from scipy.signal import correlate
de = np.diff(df["err_max"].values)
db = np.diff(df["blower"].values)
de = (de-de.mean())/(de.std()+1e-9); db = (db-db.mean())/(db.std()+1e-9)
xc = correlate(db, de, mode="full")/len(db)
lags = np.arange(-len(db)+1, len(db))
# focus on +/- 60 lags (=30 minutes at 30s)
mid = len(db)-1
window = slice(mid-60, mid+61)
peak_lag = lags[window][np.argmax(np.abs(xc[window]))]
print(f"\nCross-corr: peak |lag|<=60 at lag={peak_lag} (each lag = 30 s)")

# ---------- Save summary ---------- #
import textwrap, os
out = {
    "n_rows": int(len(df)),
    "armax_order": best_order,
    "armax_aic": float(best_aic),
    "armax_mae": float(mae),
    "armax_r2": float(r2),
    "armax_coefs": {k_:float(v_) for k_,v_ in ARMAX_PARAMS.items()},
    "kalman_params": {n_:float(p_) for n_,p_ in zip(kal_res.model.param_names, kal_res.params)},
    "kalman_mae": float(mae_kal),
    "kalman_r2": float(r2_kal),
    "linreg_mae": float(mae_lr),
    "linreg_r2": float(r2_lr),
    "linreg_coefs": dict(zip(EXOG, [float(c) for c in lr.coef_])),
    "linreg_intercept": float(lr.intercept_),
    "var_lag_order": int(var_mod.k_ar),
    "irf_blower_to_body_max": [float(x) for x in irf.irfs[:11,0,1]],
    "irf_blower_to_ambient": [float(x) for x in irf.irfs[:11,0,2]],
    "granger_pvals": {k_:[float(x) for x in v_] for k_,v_ in gc_results.items()},
    "xcorr_peak_lag_30s_steps": int(peak_lag),
}
with open("/tmp/rc_timeseries_results.json","w") as f:
    json.dump(out, f, indent=2, default=str)
print("\nWrote /tmp/rc_timeseries_results.json")

# ---------- PI / PID controller fit (the natural firmware hypothesis) ---------- #
# blower_t = clip( Kp*err_max_t + Ki*I_t + Kd*derr_t + bias , 0, 100 )
# I_{t+1} = I_t + err_max_t * dt
# Fit Kp, Ki, Kd, bias by grid+lstsq, then compare to ARMAX/Kalman.

print("\n--- PI/PID controller fit ---")
err = df["err_max"].values.astype(float)
amb = df["ambient"].values.astype(float)
setn = df["setting"].values.astype(float)
y_all = df["blower"].values.astype(float)
dt = 30.0  # seconds

# integrator with anti-windup: only accumulate when blower not saturated
# We don't yet know the controller, so use a leaky integrator: I_{t+1} = lam*I_t + err_t
# Search leak (lam) and offset over a coarse grid; fit (Kp, Ki, Kd, c_amb, c_set, bias) by OLS.
from numpy.linalg import lstsq

def pi_design(lam):
    n = len(err)
    I = np.zeros(n)
    for t in range(1,n):
        I[t] = lam*I[t-1] + err[t-1]
    derr = np.r_[0, np.diff(err)]
    X = np.column_stack([err, I, derr, amb, setn, np.ones(n)])
    return X, I

best_pi = None
for lam in [1.0, 0.999, 0.995, 0.99, 0.97, 0.95, 0.9, 0.8]:
    X, I = pi_design(lam)
    Xtr, ytr = X[:split], y_all[:split]
    coefs, *_ = lstsq(Xtr, ytr, rcond=None)
    pred_te = np.clip(X[split:] @ coefs, 0, 100)
    mae_pi = np.mean(np.abs(pred_te - y_all[split:]))
    ss_res = np.sum((y_all[split:]-pred_te)**2)
    ss_tot = np.sum((y_all[split:]-y_all[split:].mean())**2)
    r2_pi = 1 - ss_res/ss_tot
    print(f"  lam={lam:.4f}  Kp={coefs[0]:+.3f}  Ki={coefs[1]:+.4f}  Kd={coefs[2]:+.3f} "
          f"c_amb={coefs[3]:+.3f}  c_set={coefs[4]:+.3f}  bias={coefs[5]:+.3f}  "
          f"MAE={mae_pi:.2f}  R2={r2_pi:.3f}")
    if best_pi is None or mae_pi < best_pi[0]:
        best_pi = (mae_pi, r2_pi, lam, coefs)

mae_pi, r2_pi, lam_pi, coefs_pi = best_pi
print(f"\nBest PI controller: lam={lam_pi}  MAE={mae_pi:.2f}%  R2={r2_pi:.3f}")
print(f"  Kp={coefs_pi[0]:.4f}  Ki={coefs_pi[1]:.6f}  Kd={coefs_pi[2]:.4f}")
print(f"  c_ambient={coefs_pi[3]:.4f}  c_setting={coefs_pi[4]:.4f}  bias={coefs_pi[5]:.3f}")

# ---------- Multi-step walk-forward ---------- #
# For ARMAX with order (3,0,2) using best_res, forecast 10-step ahead
# rolling through test set
print("\n--- Multi-step walk-forward (h=20 steps = 10 min) ---")
H = 20
mod_full = SARIMAX(np.r_[y_tr, y_te], exog=np.vstack([X_tr, X_te]),
                   order=best_order,
                   enforce_stationarity=False, enforce_invertibility=False)
res_full = mod_full.smooth(best_res.params)
# 1-step in-sample residual MAE across test
pred_one = res_full.predict(start=split, end=len(y_all)-1)
mae_one = np.mean(np.abs(pred_one - y_te))
print(f"ARMAX one-step in-sample MAE on test segment: {mae_one:.2f}%")

# For PI controller, h-step prediction: hold integrator running, but predict purely from err
# (which we treat as known exog) — already evaluated above (MAE_pi).

# ---------- Save augmented results ---------- #
out["pi_controller"] = {
    "lam": float(lam_pi),
    "Kp_err_max": float(coefs_pi[0]),
    "Ki_integral": float(coefs_pi[1]),
    "Kd_derivative": float(coefs_pi[2]),
    "c_ambient": float(coefs_pi[3]),
    "c_setting": float(coefs_pi[4]),
    "bias": float(coefs_pi[5]),
    "mae_test_pct": float(mae_pi),
    "r2_test": float(r2_pi),
    "form": "blower = clip(Kp*err_max + Ki*I + Kd*derr + c_amb*amb + c_set*set + bias, 0, 100); I_{t+1}=lam*I_t + err_max_t",
}
with open("/tmp/rc_timeseries_results.json","w") as f:
    json.dump(out, f, indent=2, default=str)
print("Wrote augmented results.")
