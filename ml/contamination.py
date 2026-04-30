"""
Right-zone body-sensor contamination filter (BedJet artifact).

Background
----------
The wife uses a BedJet on heat for the first ~30 minutes after getting into
bed to pre-warm the sheets while the topper cools. The BedJet blows heated
air directly across the right-zone body sensors, producing artificially high
readings (commonly 90-99°F) that have nothing to do with the topper's
performance and nothing to do with her actual skin/microclimate temperature.

Per the user (2026-04-30):
  - BedJet is on heat for the first ~30 min of sleep, only on the right side.
  - Body-sensor readings ≥ 88°F should never happen naturally on either zone.
  - Anything ≥ 88°F outside the BedJet window is genuine overheat, not noise.

Implications for analysis and control
-------------------------------------
1. Percentile statistics computed over raw right-zone body readings (e.g. the
   p95=94.6°F / p99=95.3°F figures used to set previous safety thresholds)
   are contaminated and biased high. Any threshold derived from them is
   too lax. Safety thresholds should be set from user-stated comfort
   physics (88°F absolute ceiling), not from observed percentiles.
2. Override-fit baselines (`tools/fit_baselines.py`) and any future
   per-zone preference learning must drop these readings, otherwise the
   model encodes the BedJet artifact as her preferred operating range.
3. The right-zone overheat safety rail must NOT engage during the BedJet
   window even if the body sensor reads ≥ 88°F — engaging would force
   the topper to max-cool while the BedJet is intentionally heating, an
   energy fight the user explicitly does not want.

The only public function consumers should call is `is_body_right_valid`.
The other helpers exist for SQL/Pandas pipelines.
"""
from __future__ import annotations

from typing import Optional

# Hard ceiling per user: any natural body reading at or above this is suspicious.
BODY_NATURAL_CEILING_F: float = 88.0

# BedJet warm-blanket window: minutes after right-zone bed-occupancy onset
# during which body_right readings ≥ ceiling are presumed to be BedJet
# artifacts rather than topper failure or genuine overheat.
BEDJET_WINDOW_MIN: float = 30.0


def is_body_right_valid(minutes_since_onset: Optional[float],
                        body_f: Optional[float]) -> bool:
    """Return True if a right-zone body reading should be trusted for analysis/control.

    Drops only the BedJet-contaminated cases:
      - body_f ≥ 88°F AND minutes_since_onset is in [0, 30].

    All other rows are valid (including ≥ 88°F readings outside the window —
    those are real overheat events that must reach the safety rail and any
    audit pipeline).

    Missing/NaN inputs are treated conservatively:
      - body_f None/NaN → invalid (no signal to use).
      - minutes_since_onset None → assumed pre-onset / unknown; if body_f is
        ≥ ceiling we cannot rule out BedJet, so we drop. This is the safer
        bias for *training data hygiene*; the live safety rail uses an
        explicit `engaged_window` check instead and does NOT call this
        function (different operating point).
    """
    if body_f is None:
        return False
    try:
        bf = float(body_f)
    except (TypeError, ValueError):
        return False
    if bf != bf:  # NaN
        return False

    if bf < BODY_NATURAL_CEILING_F:
        return True  # any non-extreme reading is fine

    # bf ≥ 88°F: only valid if we KNOW we're past the BedJet window.
    if minutes_since_onset is None:
        return False
    try:
        m = float(minutes_since_onset)
    except (TypeError, ValueError):
        return False
    if m != m:  # NaN
        return False
    return m > BEDJET_WINDOW_MIN


def in_bedjet_window(minutes_since_onset: Optional[float]) -> bool:
    """True iff we are within the BedJet warm-blanket suppression window."""
    if minutes_since_onset is None:
        return False
    try:
        m = float(minutes_since_onset)
    except (TypeError, ValueError):
        return False
    if m != m:
        return False
    return 0.0 <= m <= BEDJET_WINDOW_MIN


# ── SQL helpers ────────────────────────────────────────────────────────
#
# Suggested view (deploy on Postgres `sleepdata` when LAN access permits):
#
# CREATE OR REPLACE VIEW v_body_right_valid AS
# WITH onset AS (
#   SELECT
#     date_trunc('day', ts AT TIME ZONE 'America/New_York'
#                       - INTERVAL '12 hours') AS night,
#     MIN(ts) FILTER (WHERE bed_right_pressure_pct > 5) AS onset_ts
#   FROM controller_readings
#   WHERE zone = 'right'
#   GROUP BY 1
# )
# SELECT cr.*,
#        EXTRACT(EPOCH FROM (cr.ts - o.onset_ts))/60.0 AS minutes_since_onset,
#        CASE
#          WHEN cr.body_f IS NULL THEN FALSE
#          WHEN cr.body_f <  88.0 THEN TRUE
#          WHEN o.onset_ts IS NULL THEN FALSE
#          WHEN EXTRACT(EPOCH FROM (cr.ts - o.onset_ts))/60.0 > 30.0 THEN TRUE
#          ELSE FALSE
#        END AS body_right_valid
# FROM controller_readings cr
# LEFT JOIN onset o
#   ON  cr.zone = 'right'
#   AND date_trunc('day', cr.ts AT TIME ZONE 'America/New_York'
#                         - INTERVAL '12 hours') = o.night;
#
# Use `WHERE body_right_valid` in any analytical query that wants
# decontaminated right-zone body readings.
SQL_VIEW_DDL: str = """\
CREATE OR REPLACE VIEW v_body_right_valid AS
WITH onset AS (
  SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York'
                      - INTERVAL '12 hours') AS night,
    MIN(ts) FILTER (WHERE bed_right_pressure_pct > 5) AS onset_ts
  FROM controller_readings
  WHERE zone = 'right'
  GROUP BY 1
)
SELECT cr.*,
       EXTRACT(EPOCH FROM (cr.ts - o.onset_ts))/60.0 AS minutes_since_onset,
       CASE
         WHEN cr.body_f IS NULL THEN FALSE
         WHEN cr.body_f <  88.0 THEN TRUE
         WHEN o.onset_ts IS NULL THEN FALSE
         WHEN EXTRACT(EPOCH FROM (cr.ts - o.onset_ts))/60.0 > 30.0 THEN TRUE
         ELSE FALSE
       END AS body_right_valid
FROM controller_readings cr
LEFT JOIN onset o
  ON  cr.zone = 'right'
  AND date_trunc('day', cr.ts AT TIME ZONE 'America/New_York'
                        - INTERVAL '12 hours') = o.night;
"""


def filter_dataframe(df, *,
                     zone_col: str = "zone",
                     body_col: str = "body_f",
                     minutes_col: str = "minutes_since_onset"):
    """Return df with right-zone BedJet-contaminated rows removed.

    Left-zone rows are passed through unchanged. Requires a precomputed
    `minutes_since_onset` column (use `add_minutes_since_onset` first).
    """
    import pandas as pd  # local import: keeps the module import-light for AppDaemon
    is_right = df[zone_col].astype(str).str.lower() == "right"
    keep = ~is_right | df.apply(
        lambda r: is_body_right_valid(r.get(minutes_col), r.get(body_col)),
        axis=1,
    )
    return df.loc[keep].copy()


def add_minutes_since_onset(df, *,
                            zone_col: str = "zone",
                            ts_col: str = "ts",
                            pressure_col: str = "bed_right_pressure_pct",
                            night_col: str = "night",
                            occupancy_threshold: float = 5.0):
    """Compute minutes_since_onset per (night, zone) for the right zone.

    Onset = first row with `pressure_col > occupancy_threshold` in that
    night for the right zone. Left-zone rows get NaN minutes_since_onset
    (irrelevant — no BedJet on left).
    """
    import pandas as pd
    out = df.copy()
    if night_col not in out.columns:
        # default night = local-day shifted by 12h (same convention as SQL view)
        ts = pd.to_datetime(out[ts_col], utc=True).dt.tz_convert("America/New_York")
        out[night_col] = (ts - pd.Timedelta(hours=12)).dt.floor("D")

    out["minutes_since_onset"] = float("nan")
    right = out[zone_col].astype(str).str.lower() == "right"
    if not right.any():
        return out
    rdf = out.loc[right].copy()
    rdf["_occ"] = rdf[pressure_col].fillna(0) > occupancy_threshold
    onset = (
        rdf[rdf["_occ"]]
        .groupby(night_col)[ts_col]
        .min()
        .rename("onset_ts")
    )
    rdf = rdf.merge(onset, left_on=night_col, right_index=True, how="left")
    delta = (pd.to_datetime(rdf[ts_col], utc=True)
             - pd.to_datetime(rdf["onset_ts"], utc=True)).dt.total_seconds() / 60.0
    out.loc[right, "minutes_since_onset"] = delta.values
    return out
