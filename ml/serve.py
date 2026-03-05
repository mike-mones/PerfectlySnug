"""
Inference server for the Perfectly Snug ML temperature controller.

Runs as a lightweight HTTP service that the HA automation calls.
Reads the current sensor state from HA and returns a recommended setting.

Usage:
    python3 ml/serve.py --model ml/models/snug_model_latest.pkl --ha-url http://192.168.0.106:8123

The HA automation calls:
    POST http://localhost:8551/predict/left
    POST http://localhost:8551/predict/right
    
Returns: {"recommended_setting": 7, "confidence": 0.85, "reason": "body_temp 93.2 > target 88.0"}
"""

import json
import os
import pickle
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from http.server import HTTPServer, BaseHTTPRequestHandler

MODEL_DIR = Path(__file__).parent / "models"
HA_URL = os.environ.get("HA_URL", "http://192.168.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
PORT = 8551

# Sensor entity mappings per zone
ZONE_ENTITIES = {
    "left": {
        "body_sensor_right": "sensor.smart_topper_left_side_body_sensor_right",
        "body_sensor_center": "sensor.smart_topper_left_side_body_sensor_center",
        "body_sensor_left": "sensor.smart_topper_left_side_body_sensor_left",
        "ambient": "sensor.smart_topper_left_side_ambient_temperature",
        "pid_control_output": "sensor.smart_topper_left_side_pid_control_output",
        "pid_integral_term": "sensor.smart_topper_left_side_pid_integral_term",
        "pid_proportional_term": "sensor.smart_topper_left_side_pid_proportional_term",
        "blower_output": "sensor.smart_topper_left_side_blower_output",
        "current_setting": "number.smart_topper_left_side_bedtime_temperature",
    },
    "right": {
        "body_sensor_right": "sensor.smart_topper_right_side_body_sensor_right",
        "body_sensor_center": "sensor.smart_topper_right_side_body_sensor_center",
        "body_sensor_left": "sensor.smart_topper_right_side_body_sensor_left",
        "ambient": "sensor.smart_topper_right_side_ambient_temperature",
        "pid_control_output": "sensor.smart_topper_right_side_pid_control_output",
        "pid_integral_term": "sensor.smart_topper_right_side_pid_integral_term",
        "pid_proportional_term": "sensor.smart_topper_right_side_pid_proportional_term",
        "blower_output": "sensor.smart_topper_right_side_blower_output",
        "current_setting": "number.smart_topper_right_side_bedtime_temperature",
    },
}


def get_ha_state(entity_id: str) -> float | None:
    """Get current state of an HA entity."""
    try:
        req = Request(
            f"{HA_URL}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            val = data.get("state")
            if val in ("unknown", "unavailable", None):
                return None
            return float(val)
    except Exception:
        return None


def get_current_features(zone: str) -> dict:
    """Fetch current sensor state from HA and compute features."""
    entities = ZONE_ENTITIES.get(zone, {})
    raw = {}
    for key, entity_id in entities.items():
        raw[key] = get_ha_state(entity_id)

    features = {}

    # Body temps
    for sensor in ["body_sensor_right", "body_sensor_center", "body_sensor_left"]:
        if raw.get(sensor) is not None:
            features[sensor] = raw[sensor]

    body_vals = [v for k, v in raw.items() if "body_sensor" in k and v is not None]
    if body_vals:
        features["body_avg"] = sum(body_vals) / len(body_vals)

    if raw.get("ambient") is not None:
        features["ambient"] = raw["ambient"]
        if "body_avg" in features:
            features["body_minus_ambient"] = features["body_avg"] - features["ambient"]

    for key in ["pid_control_output", "pid_integral_term", "pid_proportional_term", "blower_output"]:
        if raw.get(key) is not None:
            features[key] = raw[key]

    # Time features
    now = datetime.now()
    features["hour"] = now.hour
    features["minute"] = now.minute
    features["day_of_week"] = now.weekday()
    features["is_weekend"] = 1 if now.weekday() >= 5 else 0
    features["minutes_since_midnight"] = now.hour * 60 + now.minute

    # Placeholder for rate-of-change (need historical buffer)
    features["body_avg_delta_5m"] = 0.0
    features["body_avg_delta_15m"] = 0.0
    features["ambient_delta_15m"] = 0.0

    return features


def load_latest_model() -> tuple | None:
    """Load the most recent model file."""
    models = sorted(MODEL_DIR.glob("snug_model_*.pkl"))
    if not models:
        return None
    with open(models[-1], "rb") as f:
        return pickle.load(f)


class PredictHandler(BaseHTTPRequestHandler):
    model_data = None

    def do_POST(self):
        # Parse zone from path: /predict/left or /predict/right
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "predict" or parts[1] not in ("left", "right"):
            self.send_error(404, "Use POST /predict/left or /predict/right")
            return

        zone = parts[1]

        # Load model if not loaded
        if PredictHandler.model_data is None:
            PredictHandler.model_data = load_latest_model()

        if PredictHandler.model_data is None:
            # No model yet — fall back to simple proportional control
            features = get_current_features(zone)
            result = fallback_predict(features)
        else:
            features = get_current_features(zone)
            model = PredictHandler.model_data["model"]
            feature_names = PredictHandler.model_data["features"]

            import pandas as pd
            X = pd.DataFrame([features]).reindex(columns=feature_names, fill_value=0)
            raw_pred = model.predict(X)[0]
            result = {
                "recommended_setting": int(max(0, min(20, round(raw_pred)))),
                "confidence": 0.8,  # TODO: compute from model uncertainty
                "model": "lgbm",
            }

        result["features"] = features
        result["zone"] = zone
        result["timestamp"] = datetime.now().isoformat()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, format, *args):
        pass  # Suppress default logging


def fallback_predict(features: dict) -> dict:
    """Simple proportional controller when no ML model is available."""
    body_avg = features.get("body_avg", 88)
    target = 88.0  # Default target body temp

    error = body_avg - target
    # Gain of 1.0: each degree of error = 1 step of adjustment
    adjustment = 10 - int(error * 1.0)
    setting = max(0, min(20, adjustment))

    return {
        "recommended_setting": setting,
        "confidence": 0.5,
        "model": "fallback_proportional",
        "reason": f"body_avg={body_avg:.1f} target={target} error={error:+.1f}",
    }


def main():
    if not HA_TOKEN:
        print("Set HA_TOKEN environment variable")
        print("  export HA_TOKEN='your_long_lived_access_token'")
        return

    print(f"Perfectly Snug ML Inference Server")
    print(f"  HA: {HA_URL}")
    print(f"  Port: {PORT}")

    model = load_latest_model()
    if model:
        print(f"  Model: loaded ({len(model['features'])} features)")
    else:
        print(f"  Model: none found, using fallback proportional controller")

    print(f"\n  POST http://localhost:{PORT}/predict/left")
    print(f"  POST http://localhost:{PORT}/predict/right")

    server = HTTPServer(("127.0.0.1", PORT), PredictHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
