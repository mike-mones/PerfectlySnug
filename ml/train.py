"""
Training pipeline for the Perfectly Snug temperature controller.

Reads historical data from InfluxDB, constructs training samples
from manual override events, and trains a LightGBM model.

Usage:
    python3 ml/train.py --influx-url http://192.168.0.106:8086 --influx-token <token>
    
Or with CSV export:
    python3 ml/train.py --csv-dir ml/data/
"""

import argparse
import json
import pickle
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

MODEL_DIR = Path(__file__).parent / "models"
MODEL_DIR.mkdir(exist_ok=True)


def load_from_ha_json(data_path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load data from HA JSON export (overnight_data.json format)."""
    with open(data_path) as f:
        raw = json.load(f)
    
    frames = {}
    for entity_id, points in raw.items():
        if not points:
            continue
        short_name = entity_id.split(".")[-1]
        records = []
        for ts, val in points:
            try:
                records.append({
                    "timestamp": pd.to_datetime(ts),
                    "value": float(val),
                })
            except (ValueError, TypeError):
                continue
        if records:
            df = pd.DataFrame(records).set_index("timestamp")
            df.columns = [short_name]
            frames[short_name] = df
    
    if not frames:
        raise ValueError("No data found")
    
    # Merge all into a single time-aligned DataFrame
    combined = pd.concat(frames.values(), axis=1)
    combined = combined.sort_index()
    # Forward-fill missing values (sensors update at different rates)
    combined = combined.ffill()
    
    return combined


def compute_features(df: pd.DataFrame, zone: str = "left") -> pd.DataFrame:
    """Compute ML features from raw sensor DataFrame."""
    prefix = f"smart_topper_{zone}_side_"
    
    features = pd.DataFrame(index=df.index)
    
    # Body temps
    for sensor in ["body_sensor_right", "body_sensor_center", "body_sensor_left"]:
        col = f"{prefix}{sensor}"
        if col in df.columns:
            features[sensor] = df[col]
    
    # Average body temp
    body_cols = [c for c in features.columns if "body_sensor" in c]
    if body_cols:
        features["body_avg"] = features[body_cols].mean(axis=1)
    
    # Ambient
    amb_col = f"{prefix}ambient_temperature"
    if amb_col in df.columns:
        features["ambient"] = df[amb_col]
        if "body_avg" in features.columns:
            features["body_minus_ambient"] = features["body_avg"] - features["ambient"]
    
    # PID
    for component in ["pid_control_output", "pid_integral_term", "pid_proportional_term"]:
        col = f"{prefix}{component}"
        if col in df.columns:
            features[component] = df[col]
    
    # Blower
    blower_col = f"{prefix}blower_output"
    if blower_col in df.columns:
        features["blower_output"] = df[blower_col]
    
    # Rate of change (5 min and 15 min windows)
    if "body_avg" in features.columns:
        features["body_avg_delta_5m"] = features["body_avg"].diff(periods=10)   # 10 * 30s = 5min
        features["body_avg_delta_15m"] = features["body_avg"].diff(periods=30)  # 30 * 30s = 15min
    
    if "ambient" in features.columns:
        features["ambient_delta_15m"] = features["ambient"].diff(periods=30)
    
    # Time features
    features["hour"] = features.index.hour
    features["minute"] = features.index.minute
    features["day_of_week"] = features.index.dayofweek
    features["is_weekend"] = features["day_of_week"].isin([5, 6]).astype(int)
    
    # Minutes since midnight (proxy for minutes since bedtime)
    features["minutes_since_midnight"] = features["hour"] * 60 + features["minute"]
    
    return features.dropna()


def create_training_samples(
    features: pd.DataFrame,
    overrides: list[dict],
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Create training samples from override events.
    
    Each override event becomes a training sample using the feature state
    at the time of the override. The target is the value the user set.
    """
    X_rows = []
    y_values = []
    
    for override in overrides:
        ts = pd.to_datetime(override["timestamp"])
        desired = override["new_value"]
        
        # Find the closest feature row before this override
        mask = features.index <= ts
        if not mask.any():
            continue
        
        row = features.loc[mask].iloc[-1]
        X_rows.append(row)
        y_values.append(desired)
    
    if not X_rows:
        return pd.DataFrame(), pd.Series()
    
    X = pd.DataFrame(X_rows)
    y = pd.Series(y_values, name="desired_setting")
    
    return X, y


def train_model(X: pd.DataFrame, y: pd.Series, model_path: Path | None = None):
    """Train a LightGBM model on the override data."""
    try:
        import lightgbm as lgb
    except ImportError:
        print("Installing lightgbm...")
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "lightgbm"])
        import lightgbm as lgb
    
    # For small datasets, use simple params to avoid overfitting
    params = {
        "objective": "regression",
        "metric": "mae",
        "num_leaves": 8,
        "learning_rate": 0.1,
        "min_child_samples": 2,
        "max_depth": 4,
        "verbose": -1,
    }
    
    dataset = lgb.Dataset(X, label=y)
    
    model = lgb.train(
        params,
        dataset,
        num_boost_round=100,
        valid_sets=[dataset],
        callbacks=[lgb.log_evaluation(period=20)],
    )
    
    # Save model
    if model_path is None:
        model_path = MODEL_DIR / f"snug_model_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pkl"
    
    with open(model_path, "wb") as f:
        pickle.dump({"model": model, "features": list(X.columns)}, f)
    
    print(f"\nModel saved to {model_path}")
    print(f"  Training samples: {len(X)}")
    print(f"  Features: {len(X.columns)}")
    print(f"  Feature importance:")
    for name, imp in sorted(zip(X.columns, model.feature_importance()), key=lambda x: -x[1]):
        if imp > 0:
            print(f"    {name:35s}: {imp}")
    
    return model


def predict(model_path: Path, features: dict) -> int:
    """Load model and predict optimal setting for current state."""
    with open(model_path, "rb") as f:
        data = pickle.load(f)
    
    model = data["model"]
    feature_names = data["features"]
    
    X = pd.DataFrame([features])[feature_names]
    prediction = model.predict(X)[0]
    
    # Clamp to valid range
    return int(max(0, min(20, round(prediction))))


def main():
    parser = argparse.ArgumentParser(description="Train Perfectly Snug temperature model")
    parser.add_argument("--data", type=str, help="Path to HA JSON data export")
    parser.add_argument("--overrides", type=str, help="Path to overrides JSON")
    parser.add_argument("--zone", type=str, default="left", help="Zone to train for")
    args = parser.parse_args()
    
    if args.data:
        print(f"Loading data from {args.data}...")
        df = load_from_ha_json(args.data)
        print(f"  Loaded {len(df)} rows, {len(df.columns)} columns")
        print(f"  Time range: {df.index[0]} to {df.index[-1]}")
        
        features = compute_features(df, args.zone)
        print(f"  Computed {len(features)} feature rows")
        
        if args.overrides:
            with open(args.overrides) as f:
                overrides = json.load(f)
            print(f"  Loaded {len(overrides)} override events")
            
            X, y = create_training_samples(features, overrides)
            if len(X) > 0:
                print(f"\n  Training with {len(X)} samples...")
                train_model(X, y)
            else:
                print("  No training samples created. Need more override data.")
        else:
            print("  No overrides file provided. Saving features for later.")
            out = f"ml/data/features_{args.zone}_{datetime.now().strftime('%Y%m%d')}.csv"
            Path("ml/data").mkdir(parents=True, exist_ok=True)
            features.to_csv(out)
            print(f"  Features saved to {out}")
    else:
        print("Usage: python3 ml/train.py --data docs/overnight_data.json --zone left")
        print("       python3 ml/train.py --data docs/overnight_data.json --overrides ml/data/overrides.json --zone left")


if __name__ == "__main__":
    main()
