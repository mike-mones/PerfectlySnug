"""Tests for v6 R1A schema migration and backfill SQL.

These tests run against the live PG database (host 192.168.0.3). They are
read-only / additive and rely on the migration already having been applied
(the migration is itself idempotent, so re-applying is safe).

If PG is unreachable the suite is skipped — local CI without the SQL host
can still pass `pytest tests/` cleanly.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = ROOT / "sql/v6_schema.sql"
BACKFILL_SQL = ROOT / "sql/v6_backfill_actual_blower.sql"
ROLLBACK_SQL = ROOT / "sql/v6_schema_rollback.sql"

DB = {
    "host": os.environ.get("SLEEPDATA_HOST", "192.168.0.3"),
    "port": int(os.environ.get("SLEEPDATA_PORT", "5432")),
    "dbname": os.environ.get("SLEEPDATA_DBNAME", "sleepdata"),
    "user": os.environ.get("SLEEPDATA_USER", "sleepsync"),
    "password": os.environ.get("SLEEPDATA_PASSWORD", "sleepsync_local"),
}

REQUIRED_NEW_COLUMNS = {
    "regime": "character varying",
    "regime_reason": "character varying",
    "residual": "integer",
    "residual_n_support": "integer",
    "residual_lcb": "double precision",
    "divergence_steps": "integer",
    "plant_predicted_setpoint_f": "double precision",
    "bedjet_active": "boolean",
    "movement_density_15m": "double precision",
    "post_bedjet_min": "double precision",
    "mins_since_onset": "double precision",
    "l_active_dial": "integer",
    "three_level_off": "boolean",
    "right_rail_engaged": "boolean",
    "actual_blower_pct_typed": "integer",
}


@pytest.fixture(scope="module")
def conn():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        c = psycopg2.connect(**DB, connect_timeout=3)
    except Exception as exc:  # pragma: no cover — depends on env
        pytest.skip(f"PG unreachable: {exc}")
    yield c
    c.close()


def _run_psql(path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PGPASSWORD"] = DB["password"]
    return subprocess.run(
        [
            "psql",
            "-h", DB["host"], "-p", str(DB["port"]),
            "-U", DB["user"], "-d", DB["dbname"],
            "-v", "ON_ERROR_STOP=1",
            "-f", str(path),
        ],
        capture_output=True, text=True, env=env,
    )


def test_schema_files_exist():
    assert SCHEMA_SQL.exists(), SCHEMA_SQL
    assert BACKFILL_SQL.exists(), BACKFILL_SQL
    assert ROLLBACK_SQL.exists(), ROLLBACK_SQL


def test_schema_uses_if_not_exists_throughout():
    txt = SCHEMA_SQL.read_text()
    # All ALTER ADD COLUMN must use IF NOT EXISTS so the migration is idempotent.
    add_columns = re.findall(r"ADD COLUMN\s+(IF NOT EXISTS\s+)?[a-zA-Z_]+", txt)
    assert add_columns, "no ADD COLUMN statements found"
    for m in add_columns:
        assert m.strip() == "IF NOT EXISTS", "ADD COLUMN missing IF NOT EXISTS guard"
    assert "CREATE TABLE IF NOT EXISTS" in txt
    assert "CREATE INDEX IF NOT EXISTS" in txt


def test_rollback_drops_everything_added():
    txt = ROLLBACK_SQL.read_text()
    for col in REQUIRED_NEW_COLUMNS:
        assert f"DROP COLUMN IF EXISTS {col}" in txt, f"rollback missing DROP COLUMN {col}"
    assert "DROP TABLE IF EXISTS controller_pressure_movement" in txt
    assert "DROP TABLE IF EXISTS v6_nightly_summary" in txt


def test_migration_is_idempotent(conn):
    # Run twice; second pass must succeed (only NOTICEs).
    r1 = _run_psql(SCHEMA_SQL)
    assert r1.returncode == 0, r1.stderr
    r2 = _run_psql(SCHEMA_SQL)
    assert r2.returncode == 0, r2.stderr
    assert "ERROR" not in r2.stderr.upper().replace("ON_ERROR_STOP", "")


def test_all_v6_columns_present(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name='controller_readings'"
    )
    found = {row[0]: row[1] for row in cur.fetchall()}
    for col, expected_type in REQUIRED_NEW_COLUMNS.items():
        assert col in found, f"controller_readings missing column {col}"
        assert expected_type in found[col], (
            f"column {col} expected type ~{expected_type}, got {found[col]}"
        )


def test_new_tables_exist(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ('controller_pressure_movement','v6_nightly_summary')"
    )
    names = {r[0] for r in cur.fetchall()}
    assert "controller_pressure_movement" in names
    assert "v6_nightly_summary" in names


def test_backfill_regex_extracts_actual_blower_from_notes_samples():
    """The backfill SQL relies on Postgres regexp_match. Mirror the regex in
    Python and assert it parses each canonical notes shape produced by
    sleep_controller_v5._log_to_postgres / _log_override.
    """
    rx = re.compile(r"actual_blower=(\d+)")
    samples = [
        ("cycle=1 src=time_cycle+learned+room room_comp=+22 stage=unknown "
         "base_proxy_blower=100 proxy_blower=100 actual_blower=100 rc=off", 100),
        ("cycle=1 src=passive_right room_comp=+0 stage=unknown actual_blower=33", 33),
        ("cycle=6 src=passive_right room_comp=+0 stage=unknown actual_blower=0", 0),
        ("override delta=-2 actual_blower=50 notes=stuff", 50),
    ]
    for note, expected in samples:
        m = rx.search(note)
        assert m, f"failed to match {note!r}"
        assert int(m.group(1)) == expected
    # Negative: notes without the marker must yield no match.
    assert rx.search("cycle=1 src=time_cycle stage=deep") is None


def test_backfill_sql_uses_regexp_match():
    txt = BACKFILL_SQL.read_text()
    assert "regexp_match" in txt
    assert "actual_blower=([0-9]+)" in txt
    assert "actual_blower_pct_typed" in txt


def test_backfill_populated_typed_column(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM controller_readings "
        "WHERE notes LIKE '%actual_blower=%' "
        "AND actual_blower_pct_typed IS NULL"
    )
    leftover = cur.fetchone()[0]
    assert leftover == 0, f"{leftover} rows still un-backfilled"
