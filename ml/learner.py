"""
Adaptive Sleep Temperature Learner
====================================

A real ML model that learns optimal per-phase temperature settings from
accumulated overnight data. Replaces the simple EMA with a multi-signal
learning system.

Learning signals:
  1. Override history — what the user actually changed to (strongest signal)
  2. Room temperature correlation — optimal settings vary with room temp
  3. Night quality scoring — nights with no overrides = good settings
  4. Seasonal/time trends — preferences shift over weeks
  5. Sleep curve science — baselines from sleep_curve.py

Model: Bayesian-inspired adaptive regression per phase.
  - Maintains a belief about optimal setting as a function of room temp
  - Updates beliefs using override data (stronger signal) and no-override
    nights (weaker confirmation signal)
  - Blends model predictions with science-backed baselines using a
    confidence score that grows with more data

No external dependencies (no scikit-learn, no LightGBM). Pure Python + math.
Runs on HA Green / Raspberry Pi without issues.
"""

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional


# ── Data Structures ──────────────────────────────────────────────────────

@dataclass
class NightRecord:
    """Summary of one night's sleep session."""
    night_date: str
    zone: str
    duration_hours: float
    avg_body_f: float
    room_temp_f: Optional[float]
    override_count: int
    overrides: list[dict] = field(default_factory=list)
    final_settings: dict[str, int] = field(default_factory=dict)
    manual_mode: bool = False
    # Health data from SleepSync (captured at end of night from HA entities)
    avg_hr: Optional[float] = None
    avg_hrv: Optional[float] = None
    sleep_stage: Optional[str] = None
    user_rating: Optional[int] = None  # 1-5 from morning notification

    @property
    def quality_score(self) -> float:
        """Score 0-1 where 1 = perfect night (no overrides, good duration).

        When a user_rating is available (1-5 from morning notification),
        it takes priority as the strongest quality signal.
        """
        # User rating is the strongest signal when available
        if self.user_rating is not None:
            return max(0.0, min(1.0, (self.user_rating - 1) / 4.0))

        override_penalty = min(1.0, self.override_count * 0.2)
        duration_score = 1.0
        if self.duration_hours < 4:
            duration_score = self.duration_hours / 4.0
        elif self.duration_hours > 10:
            duration_score = max(0.5, 1.0 - (self.duration_hours - 10) * 0.1)
        manual_penalty = 0.5 if self.manual_mode else 0.0
        return max(0.0, (1.0 - override_penalty - manual_penalty) * duration_score)


@dataclass
class PhaseModel:
    """Learned model for a single sleep phase.

    Tracks the relationship between room temperature and optimal setting
    using online linear regression (setting = slope * room_temp + intercept).
    """
    # Linear model: optimal_setting = slope * room_temp_f + intercept
    intercept: float = 0.0
    slope: float = 0.0

    # Confidence: 0.0 = no data (use baseline), 1.0 = fully learned
    confidence: float = 0.0

    # Number of data points incorporated
    n_samples: int = 0

    # Running statistics for online regression
    sum_x: float = 0.0      # sum of room_temp values
    sum_y: float = 0.0      # sum of setting values
    sum_xx: float = 0.0     # sum of room_temp^2
    sum_xy: float = 0.0     # sum of room_temp * setting
    sum_weight: float = 0.0  # sum of weights

    def predict(self, room_temp_f: float) -> float:
        """Predict optimal setting given room temperature."""
        return self.intercept + self.slope * room_temp_f

    def update(self, room_temp_f: float, optimal_setting: float,
               weight: float = 1.0, recency_decay: float = 0.98):
        """Incorporate a new data point using weighted online regression.

        Args:
            room_temp_f: Room temperature for this data point.
            optimal_setting: The setting that was comfortable.
            weight: How much to trust this point (override=1.0, no-override=0.3).
            recency_decay: Decay factor for old data (0.98 = 2% per night).
        """
        # Guard against None/NaN/inf inputs
        if room_temp_f is None or optimal_setting is None:
            return
        if not math.isfinite(room_temp_f) or not math.isfinite(optimal_setting):
            return

        # Decay existing statistics to favor recent data
        self.sum_x *= recency_decay
        self.sum_y *= recency_decay
        self.sum_xx *= recency_decay
        self.sum_xy *= recency_decay
        self.sum_weight *= recency_decay

        # Add new data point
        self.sum_x += weight * room_temp_f
        self.sum_y += weight * optimal_setting
        self.sum_xx += weight * room_temp_f ** 2
        self.sum_xy += weight * room_temp_f * optimal_setting
        self.sum_weight += weight
        self.n_samples += 1

        # Recompute linear regression coefficients
        if self.sum_weight > 0:
            x_mean = self.sum_x / self.sum_weight
            y_mean = self.sum_y / self.sum_weight
            var_x = self.sum_xx / self.sum_weight - x_mean ** 2

            if var_x > 0.5:  # Need meaningful room temp variation
                self.slope = (
                    self.sum_xy / self.sum_weight - x_mean * y_mean
                ) / var_x
                # Clamp slope to reasonable range (-1.0 to +1.0 per °F)
                self.slope = max(-1.0, min(1.0, self.slope))
            else:
                self.slope = 0.0

            self.intercept = y_mean - self.slope * x_mean

        # Update confidence: grows with data, asymptotic to 1.0
        # ~5 nights = 0.5 confidence, ~15 nights = 0.8, ~30 = 0.9
        self.confidence = 1.0 - math.exp(-self.n_samples / 10.0)


@dataclass
class ZoneModel:
    """Complete learned model for one zone (all phases)."""
    phases: dict[str, PhaseModel] = field(default_factory=lambda: {
        "bedtime": PhaseModel(),
        "deep": PhaseModel(),
        "rem": PhaseModel(),
        "wake": PhaseModel(),
    })
    nights_trained: int = 0
    last_trained: Optional[str] = None


# ── The Learner ──────────────────────────────────────────────────────────

class SleepLearner:
    """Multi-signal adaptive learner for sleep temperature optimization.

    Usage:
        learner = SleepLearner(state_dir)
        learner.load()

        # During control loop — get recommendations
        baselines = learner.get_recommendations("left", room_temp=68.5)
        # Returns: {"bedtime": -8, "deep": -6, "rem": -5, "wake": -4}

        # After each night — update model
        learner.update_after_night(night_record)
        learner.save()
    """

    # Science-backed baselines from sleep_curve.py analysis
    SCIENCE_BASELINES = {
        "bedtime": -8,   # Aggressive cooling for sleep onset
        "deep":    -7,   # Cooler for deep/NREM-dominant first half
        "rem":     -5,   # Warmer for REM-dominant second half
        "wake":    -4,   # Ease off toward morning
    }

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_file = state_dir / "learner_state.json"
        self.override_file = state_dir / "override_history.jsonl"
        self.models: dict[str, ZoneModel] = {}

    def load(self):
        """Load model state from disk."""
        if not self.state_file.exists():
            self.models = {}
            return

        try:
            data = json.loads(self.state_file.read_text())
            for zone_name, zone_data in data.get("zones", {}).items():
                zm = ZoneModel(
                    nights_trained=zone_data.get("nights_trained", 0),
                    last_trained=zone_data.get("last_trained"),
                )
                for phase_name, phase_data in zone_data.get("phases", {}).items():
                    zm.phases[phase_name] = PhaseModel(**phase_data)
                self.models[zone_name] = zm
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            self.models = {}

    def save(self):
        """Save model state to disk."""
        data = {"zones": {}}
        for zone_name, zm in self.models.items():
            zone_data = {
                "nights_trained": zm.nights_trained,
                "last_trained": zm.last_trained,
                "phases": {},
            }
            for phase_name, pm in zm.phases.items():
                zone_data["phases"][phase_name] = {
                    "intercept": round(pm.intercept, 4),
                    "slope": round(pm.slope, 6),
                    "confidence": round(pm.confidence, 4),
                    "n_samples": pm.n_samples,
                    "sum_x": round(pm.sum_x, 4),
                    "sum_y": round(pm.sum_y, 4),
                    "sum_xx": round(pm.sum_xx, 4),
                    "sum_xy": round(pm.sum_xy, 4),
                    "sum_weight": round(pm.sum_weight, 4),
                }
            data["zones"][zone_name] = zone_data

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(data, indent=2))

    def get_recommendations(
        self,
        zone: str,
        room_temp_f: Optional[float] = None,
    ) -> dict[str, int]:
        """Get recommended settings for each phase.

        Blends science baselines with learned model based on confidence:
          recommendation = baseline * (1 - confidence) + model * confidence

        Args:
            zone: Zone name ("left" or "right").
            room_temp_f: Current room temperature in °F (improves prediction).

        Returns:
            Dict mapping phase name to recommended setting (-10 to +10).
        """
        zm = self.models.get(zone)
        result = {}

        for phase, baseline in self.SCIENCE_BASELINES.items():
            if zm is None or phase not in zm.phases:
                result[phase] = baseline
                continue

            pm = zm.phases[phase]

            if pm.confidence < 0.05 or room_temp_f is None:
                # Not enough data — use baseline
                result[phase] = baseline
                continue

            # Model prediction
            model_pred = pm.predict(room_temp_f)

            # Blend: low confidence → mostly baseline, high → mostly model
            blended = baseline * (1 - pm.confidence) + model_pred * pm.confidence

            # Clamp and round
            result[phase] = max(-10, min(10, round(blended)))

        return result

    def update_after_night(self, night: NightRecord):
        """Update the model with data from a completed night.

        Two types of learning signal:
        1. Override data (strong): User told us exactly what they want.
           Each override's `actual` value at the given room temp is a
           direct training point.
        2. No-override confirmation (weak): If the user didn't override
           a phase, the current setting was acceptable. Weakly reinforce
           whatever setting was active.
        """
        zone = night.zone
        if zone not in self.models:
            self.models[zone] = ZoneModel()
        zm = self.models[zone]

        room_temp = night.room_temp_f or 70.0  # Fallback to reference

        # 1. Learn from overrides (strong signal)
        overridden_phases = set()
        for ov in night.overrides:
            phase = ov.get("phase")
            actual = ov.get("actual")
            if phase is None or actual is None:
                continue

            overridden_phases.add(phase)
            if phase not in zm.phases:
                zm.phases[phase] = PhaseModel()

            # Override = direct signal: "I want setting X at this room temp"
            # Weight 1.0 for strong signal
            ov_room = ov.get("room_temp_f") or room_temp
            zm.phases[phase].update(ov_room, float(actual), weight=1.0)

        # 2. Learn from no-override phases (weak confirmation)
        quality = night.quality_score
        for phase, setting in night.final_settings.items():
            if phase in overridden_phases:
                continue  # Already learned from override
            if phase not in zm.phases:
                zm.phases[phase] = PhaseModel()

            # No override = setting was acceptable. Reinforce it weakly.
            # Weight scales with night quality (better night = stronger signal)
            confirm_weight = 0.3 * quality
            if confirm_weight > 0.05:
                zm.phases[phase].update(room_temp, float(setting),
                                        weight=confirm_weight)

        zm.nights_trained += 1
        zm.last_trained = datetime.now().isoformat()

    def get_model_summary(self, zone: str) -> dict:
        """Get a human-readable summary of the model state."""
        zm = self.models.get(zone)
        if zm is None:
            return {"status": "no_data", "nights": 0}

        summary = {
            "status": "trained",
            "nights": zm.nights_trained,
            "last_trained": zm.last_trained,
            "phases": {},
        }
        for phase, pm in zm.phases.items():
            baseline = self.SCIENCE_BASELINES.get(phase, -6)
            summary["phases"][phase] = {
                "baseline": baseline,
                "confidence": f"{pm.confidence:.0%}",
                "n_samples": pm.n_samples,
                "slope": f"{pm.slope:+.3f}/°F",
                "prediction_at_68F": round(pm.predict(68.0), 1),
                "prediction_at_72F": round(pm.predict(72.0), 1),
                "blended_at_70F": round(
                    baseline * (1 - pm.confidence) +
                    pm.predict(70.0) * pm.confidence, 1
                ),
            }
        return summary

    def bootstrap_from_override_history(self):
        """Bootstrap the model from existing override_history.jsonl.

        Call this once to seed the model with historical data.
        """
        if not self.override_file.exists():
            return 0

        count = 0
        nights: dict[str, list[dict]] = {}  # group by night_date

        with open(self.override_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    key = f"{record.get('night_date', 'unknown')}_{record.get('zone', 'left')}"
                    nights.setdefault(key, []).append(record)
                    count += 1
                except json.JSONDecodeError:
                    continue

        for key, overrides in nights.items():
            zone = overrides[0].get("zone", "left")
            # Use room temp from the first override if available
            stored_room = None
            for ov in overrides:
                rt = ov.get("room_temp_f")
                if rt is not None:
                    stored_room = rt
                    break
            night = NightRecord(
                night_date=overrides[0].get("night_date", "unknown"),
                zone=zone,
                duration_hours=8.0,
                avg_body_f=82.0,
                room_temp_f=stored_room or 70.0,
                override_count=len(overrides),
                overrides=overrides,
                final_settings={},
                manual_mode=False,
            )
            self.update_after_night(night)

        return count


# ── CLI for testing ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    state_dir = Path(__file__).parent / "state"
    learner = SleepLearner(state_dir)
    learner.load()

    # Bootstrap from existing data if model is empty
    if not learner.models:
        count = learner.bootstrap_from_override_history()
        if count > 0:
            print(f"Bootstrapped from {count} override records")
            learner.save()
        else:
            print("No existing data to bootstrap from")

    # Show current recommendations
    for zone in ["left", "right"]:
        summary = learner.get_model_summary(zone)
        if summary["status"] == "no_data":
            print(f"\n{zone}: No data yet")
            continue

        print(f"\n{'=' * 50}")
        print(f"Zone: {zone} ({summary['nights']} nights trained)")
        print(f"Last trained: {summary['last_trained']}")
        for phase, info in summary["phases"].items():
            print(f"\n  {phase}:")
            print(f"    Baseline:     {info['baseline']:+d}")
            print(f"    Confidence:   {info['confidence']}")
            print(f"    Samples:      {info['n_samples']}")
            print(f"    Room temp sensitivity: {info['slope']}")
            print(f"    Prediction @68°F: {info['prediction_at_68F']:+.1f}")
            print(f"    Prediction @72°F: {info['prediction_at_72F']:+.1f}")
            print(f"    Blended @70°F:    {info['blended_at_70F']:+.1f}")

    # Show recommendations at different room temps
    print(f"\n{'=' * 50}")
    print("Recommendations by room temperature:")
    for temp in [66, 68, 70, 72, 74]:
        recs = learner.get_recommendations("left", room_temp_f=float(temp))
        rec_str = " | ".join(f"{p}={v:+d}" for p, v in recs.items())
        print(f"  {temp}°F → {rec_str}")
