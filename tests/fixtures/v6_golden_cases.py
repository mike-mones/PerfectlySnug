"""Golden case fixtures for v6 integration tests.

Per proposal §9, three named counterfactual cases:
- Case A: LEFT cold-cluster (01:37–02:05)
- Case B: RIGHT under-cooled override (-4 → -5 at 03:25)
- Case C: Cold mid-night (04:27) + warm AM (06:56)

DATA SOURCE: Synthesized from proposal §9 specifications.
The exact historical PG rows for 2026-05-01 / 2026-04-30 are referenced in
v6_eval.py CASE_DEFS. These fixtures replicate the environmental conditions
described in proposal §9 and §13 tables so integration tests can run without
PG access.

Each fixture is a "snapshot" dict containing all fields needed by
ml.v6.policy.compute_v6_plan().
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent


def _load_json(name: str) -> dict:
    path = FIXTURE_DIR / name
    with open(path) as f:
        return json.load(f)


def load_fixture(name: str) -> dict:
    """Load a golden case fixture by short name.

    Names: "v6_case_A", "v6_case_B", "v6_case_C_cold", "v6_case_C_warm"
    """
    return _load_json(f"{name}.json")


# ── Fixture data (synthesized per §9 specifications) ──────────────────

CASE_A_SNAPSHOT = {
    "_meta": {
        "case": "A",
        "description": "LEFT cold-cluster 01:37–02:05 on 2026-05-01",
        "source": "synthesized from proposal §9 table",
        "zone": "left",
        "trigger_time": "2026-05-01T01:37:00-04:00",
    },
    "zone": "left",
    "elapsed_min": 120.0,
    "mins_since_onset": 120.0,
    "post_bedjet_min": None,
    "sleep_stage": "light",
    "bed_occupied": True,
    "room_f": 67.4,
    "body_skin_f": 76.1,
    "body_hot_f": 76.1,
    "body_avg_f": 75.9,
    "body_center_f": None,
    "override_freeze_active": False,
    "right_rail_engaged": False,
    "pre_sleep_active": False,
    "three_level_off": True,
    "movement_density_15m": 0.02,
    "current_setting": -10,
    "v52_setting": -10,
    "body_trend_15m": 0.0,
    "history": [
        {"ts": "2026-05-01T01:20:00-04:00", "setting": -10, "room_f": 67.5, "body_skin_f": 76.3},
        {"ts": "2026-05-01T01:25:00-04:00", "setting": -10, "room_f": 67.5, "body_skin_f": 76.2},
        {"ts": "2026-05-01T01:30:00-04:00", "setting": -10, "room_f": 67.4, "body_skin_f": 76.1},
        {"ts": "2026-05-01T01:35:00-04:00", "setting": -10, "room_f": 67.4, "body_skin_f": 76.1},
    ],
}

CASE_B_SNAPSHOT = {
    "_meta": {
        "case": "B",
        "description": "RIGHT under-cooled override at 03:25 on 2026-05-01",
        "source": "synthesized from proposal §9 table",
        "zone": "right",
        "trigger_time": "2026-05-01T03:25:00-04:00",
    },
    "zone": "right",
    "elapsed_min": 245.0,
    "mins_since_onset": 245.0,
    "post_bedjet_min": 260.0,
    "sleep_stage": "deep",
    "bed_occupied": True,
    "room_f": 68.3,
    "body_skin_f": 73.1,
    "body_hot_f": 77.0,
    "body_avg_f": 75.0,
    "body_center_f": 77.0,
    "override_freeze_active": False,
    "right_rail_engaged": False,
    "pre_sleep_active": False,
    "three_level_off": True,
    "movement_density_15m": 0.12,
    "current_setting": -3,
    "v52_setting": -3,
    "body_trend_15m": 0.1,
    "history": [
        {"ts": "2026-05-01T03:10:00-04:00", "setting": -3, "room_f": 68.3, "body_skin_f": 73.4},
        {"ts": "2026-05-01T03:15:00-04:00", "setting": -3, "room_f": 68.3, "body_skin_f": 73.2},
        {"ts": "2026-05-01T03:20:00-04:00", "setting": -3, "room_f": 68.3, "body_skin_f": 73.1},
    ],
}

CASE_C_COLD_SNAPSHOT = {
    "_meta": {
        "case": "C_cold",
        "description": "LEFT cold mid-night at 04:27 on 2026-04-30",
        "source": "synthesized from proposal §9 table",
        "zone": "left",
        "trigger_time": "2026-04-30T04:27:00-04:00",
    },
    "zone": "left",
    "elapsed_min": 270.0,
    "mins_since_onset": 270.0,
    "post_bedjet_min": None,
    "sleep_stage": "rem",
    "bed_occupied": True,
    "room_f": 68.5,
    "body_skin_f": 76.0,
    "body_hot_f": 76.0,
    "body_avg_f": 75.8,
    "body_center_f": None,
    "override_freeze_active": False,
    "right_rail_engaged": False,
    "pre_sleep_active": False,
    "three_level_off": True,
    "movement_density_15m": 0.01,
    "current_setting": -5,
    "v52_setting": -2,
    "body_trend_15m": 0.0,
    "history": [
        {"ts": "2026-04-30T04:15:00-04:00", "setting": -5, "room_f": 68.6, "body_skin_f": 76.1},
        {"ts": "2026-04-30T04:20:00-04:00", "setting": -5, "room_f": 68.5, "body_skin_f": 76.0},
        {"ts": "2026-04-30T04:25:00-04:00", "setting": -5, "room_f": 68.5, "body_skin_f": 76.0},
    ],
}

CASE_C_WARM_SNAPSHOT = {
    "_meta": {
        "case": "C_warm",
        "description": "LEFT warm AM at 06:56 on 2026-04-30",
        "source": "synthesized from proposal §9 table",
        "zone": "left",
        "trigger_time": "2026-04-30T06:56:00-04:00",
    },
    "zone": "left",
    "elapsed_min": 420.0,
    "mins_since_onset": 420.0,
    "post_bedjet_min": None,
    "sleep_stage": "awake",
    "bed_occupied": True,
    "room_f": 70.5,
    "body_skin_f": 84.0,
    "body_hot_f": 84.0,
    "body_avg_f": 82.0,
    "body_center_f": None,
    "override_freeze_active": False,
    "right_rail_engaged": False,
    "pre_sleep_active": False,
    "three_level_off": True,
    "movement_density_15m": 0.03,
    "current_setting": -6,
    "v52_setting": -6,
    "body_trend_15m": 0.3,
    "history": [
        {"ts": "2026-04-30T06:40:00-04:00", "setting": -6, "room_f": 70.4, "body_skin_f": 83.5},
        {"ts": "2026-04-30T06:45:00-04:00", "setting": -6, "room_f": 70.4, "body_skin_f": 83.8},
        {"ts": "2026-04-30T06:50:00-04:00", "setting": -6, "room_f": 70.5, "body_skin_f": 84.0},
    ],
}


def write_fixtures():
    """Write all golden case fixtures to JSON files."""
    fixtures = {
        "v6_case_A.json": CASE_A_SNAPSHOT,
        "v6_case_B.json": CASE_B_SNAPSHOT,
        "v6_case_C_cold.json": CASE_C_COLD_SNAPSHOT,
        "v6_case_C_warm.json": CASE_C_WARM_SNAPSHOT,
    }
    for name, data in fixtures.items():
        path = FIXTURE_DIR / name
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  wrote {path}")


if __name__ == "__main__":
    write_fixtures()
