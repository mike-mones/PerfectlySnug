"""Tests for the v6 evaluation harness scaffolding (tools/v6_eval.py).

Covers the §11.3 rollback-criteria metric helpers, graceful fallback when
ml.v6.* modules are absent, and CLI wiring for --policy v5_2_actual.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import v6_eval  # noqa: E402


def _fixture_night_df() -> pd.DataFrame:
    """Two-zone, one-night, 12-row fixture covering each metric path."""
    base = pd.Timestamp("2026-04-30 22:00:00", tz="America/New_York")
    rows = []
    # Left zone: one override, one row above 84F, one row below 72F
    for i in range(6):
        rows.append({
            "ts": base + pd.Timedelta(minutes=5 * i),
            "zone": "left",
            "night": "2026-04-30",
            "body_left_f": [80, 85, 84.5, 70, 71, 78][i],
            "action": ("override" if i == 1 else "decision"),
            "override_delta": (-2 if i == 1 else None),
            "notes": ("hot_rail engage" if i == 4 else "cycle=1 stage=deep"),
            "divergence_steps": (3 if i == 5 else 0),
        })
    for i in range(6):
        rows.append({
            "ts": base + pd.Timedelta(minutes=5 * i),
            "zone": "right",
            "night": "2026-04-30",
            "body_left_f": [82, 87, 86.5, 88, 80, 78][i],
            "action": "decision",
            "override_delta": None,
            "notes": "cycle=1 stage=core",
            "divergence_steps": 0,
        })
    return pd.DataFrame(rows)


def test_compute_v6_night_metrics_basic():
    df = _fixture_night_df()
    out = v6_eval.compute_v6_night_metrics(df)
    assert len(out) == 2
    by_zone = {r["zone"]: r for r in out}

    left = by_zone["left"]
    assert left["override_count"] == 1
    assert left["override_mae_steps"] == 2.0
    # 2 left rows above 84F (85, 84.5) → 5min each (one diff is 5 min, the
    # very first row gets the 5.0 fillna default).
    assert left["minutes_above_84f"] >= 5.0
    # below 72F: rows i=3 (70) and i=4 (71)
    assert left["minutes_below_72f"] >= 5.0
    # Notes contain "hot_rail" once → one rail engagement
    assert left["rail_engagements"] == 1
    # divergence_steps>0 once → one event start
    assert left["divergence_guard_activations"] == 1

    right = by_zone["right"]
    # rows i=1 (87), i=2 (86.5), i=3 (88) above 86F
    assert right["minutes_above_86f"] >= 10.0
    assert right["override_count"] == 0
    assert right["override_mae_steps"] is None


def test_aggregate_v6_metrics_per_zone():
    df = _fixture_night_df()
    per_night = v6_eval.compute_v6_night_metrics(df)
    agg = v6_eval.aggregate_v6_metrics(per_night)
    assert agg["n_nights"] == 1
    assert set(agg["by_zone"]) == {"left", "right"}
    assert agg["by_zone"]["left"]["override_count_total"] == 1
    assert agg["by_zone"]["right"]["override_count_total"] == 0


def test_count_event_starts_on_pulse_train():
    s = pd.Series([False, True, True, False, True, False, True])
    assert v6_eval._count_event_starts(s) == 3
    assert v6_eval._count_event_starts(pd.Series([], dtype=bool)) == 0


def test_aggregate_handles_empty_input():
    assert v6_eval.aggregate_v6_metrics([]) == {"n_nights": 0}


def test_v6_synth_falls_back_when_module_missing(monkeypatch, capsys):
    # Force ImportError for ml.v6.policy
    real_import = __import__

    def fake_import(name, *a, **kw):
        if name == "ml.v6.policy":
            raise ImportError("ml.v6.policy not built yet")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)
    pol = v6_eval._load_policy("v6_synth")
    assert isinstance(pol, v6_eval.V52BaselinePolicy)
    err = capsys.readouterr().err
    assert "falling back" in err


def test_default_findings_path_uses_policy_name(tmp_path):
    p = v6_eval._default_findings_path(tmp_path, "v5_2_actual")
    assert p.parent == tmp_path
    assert p.name.startswith("v6_eval_v5_2_actual_") and p.suffix == ".json"


def test_filter_recent_nights_keeps_only_last_n():
    df = pd.DataFrame({
        "ts": pd.date_range("2026-04-01", periods=5, freq="D"),
        "zone": ["left"] * 5,
        "night": ["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04", "2026-04-05"],
    })
    out = v6_eval._filter_recent_nights(df, 2)
    assert sorted(out["night"].unique()) == ["2026-04-04", "2026-04-05"]
    # nights=None / 0 → no filter
    assert len(v6_eval._filter_recent_nights(df, None)) == 5


def test_safe_import_returns_none_on_missing_module():
    mod, err = v6_eval._safe_import("ml.v6.definitely_not_a_real_module")
    assert mod is None
    assert err is not None


def test_run_v6_actual_with_injected_db(monkeypatch):
    fixture = _fixture_night_df()
    fixture["ts"] = pd.to_datetime(fixture["ts"], utc=True)
    monkeypatch.setattr(v6_eval, "load_data", lambda db_conn=None: fixture)
    result = v6_eval.run_v6_actual()
    assert result["policy"] == "v5_2_actual"
    assert result["aggregate"]["n_nights"] == 1
    assert "left" in result["aggregate"]["by_zone"]


def test_cli_v5_2_actual_writes_json(monkeypatch, tmp_path):
    fixture = _fixture_night_df()
    fixture["ts"] = pd.to_datetime(fixture["ts"], utc=True)
    monkeypatch.setattr(v6_eval, "load_data", lambda db_conn=None: fixture)
    out = tmp_path / "result.json"
    rc = v6_eval.main(["--policy", "v5_2_actual", "--out", str(out),
                       "--findings-dir", str(tmp_path)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["policy"] == "v5_2_actual"
    assert payload["aggregate"]["n_nights"] == 1


def test_cli_shadow_compare_handles_missing_v6(monkeypatch, tmp_path):
    fixture = _fixture_night_df()
    fixture["ts"] = pd.to_datetime(fixture["ts"], utc=True)
    monkeypatch.setattr(v6_eval, "load_data", lambda db_conn=None: fixture)

    # Force ml.v6.policy import to fail so the v6 leg becomes a stub.
    original_safe_import = v6_eval._safe_import

    def patched_safe_import(modpath):
        if "v6.policy" in modpath:
            return None, "not built"
        return original_safe_import(modpath)

    monkeypatch.setattr(v6_eval, "_safe_import", patched_safe_import)

    real_import = __import__

    def fake_import(name, *a, **kw):
        if "ml.v6.policy" in name:
            raise ImportError("not built")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)
    # Also remove from sys.modules cache
    import sys as _sys
    for key in list(_sys.modules.keys()):
        if "ml.v6.policy" in key:
            monkeypatch.delitem(_sys.modules, key)

    out = tmp_path / "cmp.json"
    rc = v6_eval.main(["--policy", "shadow_compare", "--out", str(out),
                       "--findings-dir", str(tmp_path)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["v5_2_actual"]["aggregate"]["n_nights"] == 1
    assert payload["v6_synth"]["status"] == "module_not_built"
    assert payload["v6_synth"]["import_error"]  # any non-empty string


def test_cli_unknown_policy_raises():
    with pytest.raises(SystemExit):
        v6_eval._load_policy("totally_unknown_policy")
