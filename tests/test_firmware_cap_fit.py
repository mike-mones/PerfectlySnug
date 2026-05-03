"""Tests for tools/firmware_cap_fit.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import firmware_cap_fit as fcf  # noqa: E402


def test_fit_from_rows_groups_by_setting():
    rows = [(-5, 86.0), (-5, 87.0), (-5, 85.0), (-3, 90.0), (-3, 88.0)]
    pts = fcf.fit_from_rows(rows)
    assert [p.setting for p in pts] == [-5, -3]
    assert pts[0].n == 3
    assert pts[0].median_setpoint_f == pytest.approx(86.0)
    assert pts[1].median_setpoint_f == pytest.approx(89.0)


def test_fit_from_rows_skips_nones():
    rows = [(-5, None), (None, 86.0), (-5, 86.0), ("bad", 1.0)]
    pts = fcf.fit_from_rows(rows)
    assert len(pts) == 1 and pts[0].setting == -5 and pts[0].n == 1


def test_check_monotonic_passes_on_monotonic_table():
    pts = [
        fcf.CapPoint(-8, 5, 70.0, 69.0, 71.0),
        fcf.CapPoint(-5, 5, 80.0, 79.0, 81.0),
        fcf.CapPoint(0, 5, 91.0, 90.0, 92.0),
    ]
    assert fcf.check_monotonic(pts) == []


def test_check_monotonic_warns_on_inversion():
    pts = [
        fcf.CapPoint(-8, 5, 75.0, 74.0, 76.0),
        fcf.CapPoint(-5, 5, 70.0, 69.0, 71.0),  # warmer L but colder setpoint
        fcf.CapPoint(0, 5, 90.0, 89.0, 91.0),
    ]
    warnings = fcf.check_monotonic(pts)
    assert len(warnings) == 1
    assert "non-monotonic" in warnings[0]


def test_build_table_schema_keys():
    pts = [fcf.CapPoint(-5, 3, 86.0, 85.0, 87.0)]
    table = fcf.build_table(pts, since="2026-04-01")
    assert set(table) >= {
        "generated_at", "since", "n_settings",
        "anchor_points", "monotonic_warnings", "is_monotonic", "table",
    }
    assert table["n_settings"] == 1
    assert table["table"][0]["setting"] == -5
    assert table["is_monotonic"] is True


def test_cli_end_to_end_with_csv_fixture(tmp_path: Path):
    csv = tmp_path / "fixture.csv"
    csv.write_text(
        "setting,setpoint_f\n"
        "-8,70.0\n-8,71.0\n"
        "-5,80.0\n-5,82.0\n"
        "0,91.0\n0,92.0\n"
    )
    out = tmp_path / "cap.json"
    rc = fcf.main(["--from-csv", str(csv), "--output", str(out)])
    assert rc == 0
    table = json.loads(out.read_text())
    assert table["n_settings"] == 3
    settings = [r["setting"] for r in table["table"]]
    assert settings == [-8, -5, 0]
    assert table["is_monotonic"] is True
    assert table["table"][0]["median_setpoint_f"] == pytest.approx(70.5)


def test_cli_warns_but_succeeds_on_nonmonotonic(tmp_path: Path, capsys):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "setting,setpoint_f\n"
        "-8,80.0\n"  # warmer than -5 → non-monotonic
        "-5,70.0\n"
        "0,90.0\n"
    )
    out = tmp_path / "cap.json"
    rc = fcf.main(["--from-csv", str(csv), "--output", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "non-monotonic" in captured.err
    table = json.loads(out.read_text())
    assert table["is_monotonic"] is False
    assert len(table["monotonic_warnings"]) >= 1


def test_quantile_helper():
    assert fcf._quantile([1.0, 2.0, 3.0], 0.5) == pytest.approx(2.0)
    assert fcf._quantile([1.0, 2.0, 3.0, 4.0], 0.25) == pytest.approx(1.75)
    assert fcf._quantile([5.0], 0.5) == 5.0
