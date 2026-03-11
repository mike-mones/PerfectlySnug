"""
Sleep Stage Classifier — Train from controller's collected data.

Reads stage_training_data from the AppDaemon controller_state.json,
trains a lightweight Random Forest classifier, and exports it as a
compact JSON decision tree that can be loaded directly in AppDaemon
(no sklearn/pickle dependency on the HA Green).

Training data format (collected by sleep_controller_v2.py):
    {"stage": "deep", "hr": 52.3, "hrv": 48.1,
     "hr_pct": -0.12, "hrv_pct": 0.15, "hours_in": 2.3}

Usage:
    # From controller state on HA Green:
    scp root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/controller_state.json /tmp/
    python3 ml/train_stage_classifier.py --state /tmp/controller_state.json

    # Or from a manually created CSV:
    python3 ml/train_stage_classifier.py --csv ml/data/stage_samples.csv

Output:
    ml/models/stage_classifier.json — portable model (no pickle needed)
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).parent / "models"
MODEL_DIR.mkdir(exist_ok=True)

# Stages we classify (awake is handled separately by the controller)
STAGES = ["deep", "core", "rem", "awake"]


def load_from_state(state_path: str) -> list[dict]:
    """Extract stage_training_data from controller_state.json."""
    with open(state_path) as f:
        data = json.load(f)

    samples = []
    for zone, zdata in data.items():
        zone_state = zdata.get("state", {})
        training = zone_state.get("stage_training_data", [])
        samples.extend(training)

    if not samples:
        raise ValueError(
            f"No stage_training_data found in {state_path}. "
            "The controller collects this automatically when "
            "Apple Watch provides real sleep stages with HR/HRV."
        )
    return samples


def load_from_csv(csv_path: str) -> list[dict]:
    """Load from CSV with columns: stage,hr,hrv,hr_pct,hrv_pct,hours_in."""
    import csv
    samples = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples.append({
                "stage": row["stage"],
                "hr": float(row["hr"]),
                "hrv": float(row["hrv"]),
                "hr_pct": float(row["hr_pct"]),
                "hrv_pct": float(row["hrv_pct"]),
                "hours_in": float(row["hours_in"]),
            })
    return samples


def extract_features(sample: dict) -> list[float]:
    """Convert a sample dict into a feature vector."""
    return [
        sample["hr_pct"],       # HR % deviation from baseline
        sample["hrv_pct"],      # HRV % deviation from baseline
        sample["hours_in"],     # hours since bedtime
    ]


FEATURE_NAMES = ["hr_pct", "hrv_pct", "hours_in"]


def train_classifier(samples: list[dict]) -> dict:
    """Train a Random Forest and export as JSON decision trees.

    Returns a dict with the model structure that can be evaluated
    without sklearn — just nested if/else on feature thresholds.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score, LeaveOneGroupOut
        from sklearn.metrics import classification_report
    except ImportError:
        import subprocess
        import sys
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "scikit-learn"])
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
        from sklearn.metrics import classification_report

    # Filter to known stages
    samples = [s for s in samples if s["stage"] in STAGES]

    if len(samples) < 10:
        print(f"WARNING: Only {len(samples)} samples. "
              "Model will be unreliable. Need ~50+ for decent accuracy.")

    # Build arrays
    X = np.array([extract_features(s) for s in samples])
    y = np.array([s["stage"] for s in samples])

    # Class distribution
    dist = Counter(y)
    print(f"\nClass distribution ({len(samples)} samples):")
    for stage in STAGES:
        count = dist.get(stage, 0)
        pct = count / len(samples) * 100
        print(f"  {stage:8s}: {count:4d} ({pct:.0f}%)")

    # Train with conservative hyperparams (small dataset)
    clf = RandomForestClassifier(
        n_estimators=20,        # small forest — we need portability
        max_depth=4,            # prevent overfitting on small data
        min_samples_leaf=3,     # conservative
        class_weight="balanced",  # handle class imbalance
        random_state=42,
    )
    clf.fit(X, y)

    # Cross-validation (if enough data)
    if len(samples) >= 20:
        scores = cross_val_score(clf, X, y, cv=5, scoring="accuracy")
        print(f"\n5-fold CV accuracy: {scores.mean():.1%} "
              f"(±{scores.std():.1%})")

    # Full training set report
    y_pred = clf.predict(X)
    print(f"\nTraining set classification report:")
    print(classification_report(y, y_pred, target_names=STAGES,
                                zero_division=0))

    # Feature importance
    print("Feature importance:")
    for name, imp in sorted(
            zip(FEATURE_NAMES, clf.feature_importances_),
            key=lambda x: -x[1]):
        print(f"  {name:15s}: {imp:.3f}")

    # Export to portable JSON format
    model_json = _export_forest_json(clf, FEATURE_NAMES, STAGES)

    return model_json


def _export_tree_json(tree, feature_names: list[str]) -> dict:
    """Convert a sklearn DecisionTree to a JSON-serializable dict."""
    tree_ = tree.tree_
    classes = tree.classes_

    def recurse(node_id: int) -> dict:
        if tree_.children_left[node_id] == -1:  # leaf
            # Get class probabilities
            values = tree_.value[node_id][0]
            total = values.sum()
            probs = {
                str(classes[i]): round(float(values[i] / total), 3)
                for i in range(len(classes))
                if values[i] > 0
            }
            return {"leaf": True, "probs": probs}

        feature_idx = tree_.feature[node_id]
        threshold = float(tree_.threshold[node_id])
        return {
            "feature": feature_names[feature_idx],
            "threshold": round(threshold, 4),
            "left": recurse(tree_.children_left[node_id]),   # <= threshold
            "right": recurse(tree_.children_right[node_id]),  # > threshold
        }

    return recurse(0)


def _export_forest_json(clf, feature_names, stages) -> dict:
    """Export entire Random Forest as JSON."""
    trees = []
    for estimator in clf.estimators_:
        trees.append(_export_tree_json(estimator, feature_names))

    return {
        "type": "random_forest",
        "n_trees": len(trees),
        "features": feature_names,
        "classes": list(clf.classes_),
        "trees": trees,
        "metadata": {
            "n_samples": clf.n_features_in_,
            "max_depth": clf.max_depth,
        },
    }


def predict_from_json(model: dict, features: dict) -> str:
    """Predict sleep stage using the JSON model (no sklearn needed).

    This is the function that runs inside AppDaemon.
    """
    feature_vec = [features[f] for f in model["features"]]
    classes = model["classes"]

    # Vote from all trees
    votes = Counter()
    for tree in model["trees"]:
        probs = _evaluate_tree(tree, feature_vec)
        # Weighted vote by probability
        for cls, prob in probs.items():
            votes[cls] += prob

    if not votes:
        return "unknown"

    return votes.most_common(1)[0][0]


def _evaluate_tree(node: dict, feature_vec: list[float]) -> dict:
    """Walk a JSON decision tree to get leaf probabilities."""
    if node.get("leaf"):
        return node["probs"]

    feature_name = node["feature"]
    # Features are positional — find index
    # (but in AppDaemon we pass a dict, so we use the name)
    threshold = node["threshold"]

    # For the exported model, features are indexed by name
    # but feature_vec is ordered by model["features"]
    # We need the index. Since _evaluate_tree gets a list,
    # we need to know which index corresponds to this feature.
    # This is embedded in the tree — features are stored as names.
    # So we need a different approach for list input.
    # Actually, let's just pass dict input from the controller.
    #
    # For predict_from_json: features is a dict, feature_vec is a list
    # The tree stores feature names. We need to resolve to index.
    # Let's just keep it simple and resolve in predict_from_json.
    raise NotImplementedError("Use predict_with_dict instead")


def predict_with_dict(model: dict, features: dict) -> tuple[str, float]:
    """Predict sleep stage from a feature dict. Returns (stage, confidence).

    This is the actual function used in AppDaemon. No sklearn needed.
    """
    classes = model["classes"]

    # Accumulate class probabilities across all trees
    totals = {c: 0.0 for c in classes}
    for tree in model["trees"]:
        probs = _walk_tree(tree, features)
        for cls, prob in probs.items():
            totals[cls] += prob

    # Normalize
    n_trees = model["n_trees"]
    for cls in totals:
        totals[cls] /= n_trees

    # Pick winner
    best_cls = max(totals, key=totals.get)
    confidence = totals[best_cls]

    return best_cls, confidence


def _walk_tree(node: dict, features: dict) -> dict:
    """Walk a JSON decision tree using named features."""
    if node.get("leaf"):
        return node["probs"]

    val = features.get(node["feature"], 0.0)
    if val <= node["threshold"]:
        return _walk_tree(node["left"], features)
    else:
        return _walk_tree(node["right"], features)


def main():
    parser = argparse.ArgumentParser(
        description="Train sleep stage classifier from controller data")
    parser.add_argument(
        "--state", type=str,
        help="Path to controller_state.json from HA Green")
    parser.add_argument(
        "--csv", type=str,
        help="Path to CSV with stage samples")
    parser.add_argument(
        "--output", type=str,
        default=str(MODEL_DIR / "stage_classifier.json"),
        help="Output model path (JSON)")
    args = parser.parse_args()

    if args.state:
        samples = load_from_state(args.state)
    elif args.csv:
        samples = load_from_csv(args.csv)
    else:
        # Try default location
        default = Path("/tmp/controller_state.json")
        if default.exists():
            samples = load_from_state(str(default))
        else:
            parser.error(
                "Provide --state or --csv. Or place "
                "controller_state.json in /tmp/")

    print(f"Loaded {len(samples)} training samples")

    model = train_classifier(samples)

    # Save
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(model, f, indent=2)
    print(f"\nModel saved to {output_path}")
    print(f"  Trees: {model['n_trees']}")
    print(f"  Features: {model['features']}")
    print(f"  Classes: {model['classes']}")

    # Verify portability — test predict_with_dict
    if samples:
        test = samples[0]
        feat = {
            "hr_pct": test["hr_pct"],
            "hrv_pct": test["hrv_pct"],
            "hours_in": test["hours_in"],
        }
        pred, conf = predict_with_dict(model, feat)
        print(f"\n  Verification: sample[0] stage={test['stage']} "
              f"→ predicted={pred} (conf={conf:.0%})")

    print(f"\nDeploy to HA Green:")
    print(f"  scp {output_path} "
          f"root@192.168.0.106:/addon_configs/"
          f"a0d7b954_appdaemon/apps/stage_classifier.json")


if __name__ == "__main__":
    main()
