# PerfectlySnug — Evaluation Framework (2026-05-04)

**Status:** proposal · **Owner:** controller team · **Supersedes:** ad-hoc
acceptance language scattered across `docs/proposals/2026-05-01_*` and
`tools/v6_eval.py` case checks.

This document defines the *one* metric stack used to decide whether a
controller change ships, holds, or reverts. All numbers come from
PostgreSQL (`192.168.0.3:5432/sleepdata`) — no JSON, no HA history API.

The target body temperature referenced throughout is **80.0 °F**, taken
from `BODY_FB_TARGET_F` and `RIGHT_BODY_FB_TARGET_F` in
`appdaemon/sleep_controller_v5.py:116,155`. If those constants change, the
numeric thresholds in §1 must be re-derived in the same commit.

---

## 1. Metric definitions

All metrics are computed per `(night, zone, user)` over the **bed window**
defined as `[bed_onset, wake]` (see §3.2 for detection). `user` is `mike`
(left) or `partner` (right) and is fixed by zone in this house; the column
exists for future portability. Setting deltas (`Δsetting`) are computed on
the integer dial value `controller_readings.setting` ∈ [-10, 0].

Two write classes are distinguished using the existing `controller_readings.action`
column (values observed in the live corpus: `'override'`, `'controller'`,
`'init'`, `'tag'`, `'rail'`, NULL). For these metrics:

* **Manual write** ≡ `action = 'override'`
* **Controller write** ≡ `action IN ('controller','init','rail')`
  AND `setting IS DISTINCT FROM lag(setting)` over `(zone ORDER BY ts)`.
  The `IS DISTINCT FROM` filter strips no-op heartbeat rows.

### 1.1 Discomfort proxy (lower = better)

| Metric | Definition |
|---|---|
| `adj_count_per_night` | Count of rows with `action='override'` and `ts BETWEEN bed_onset AND wake`. |
| `adj_magnitude_sum`   | `Σ |override_delta|` over the same set. `override_delta` is already populated by the controller. |
| `adj_weighted_score`  | `adj_count_per_night + 0.5 * adj_magnitude_sum`. Single scalar used for ranking. |

### 1.2 Stability (controller-driven only)

Computed from the ordered series of **controller writes** in the bed window
where `setting` actually changed (`Δsetting ≠ 0`).

| Metric | Definition |
|---|---|
| `oscillation_count`        | Number of sign flips in the sequence `sign(Δsetting_t)` (excluding zeros). |
| `overcorrection_rate`      | Fraction of controller writes followed within 10 min by another controller write whose `Δsetting` has the **opposite sign**. Denominator excludes the last write of the night. |
| `setting_total_variation`  | `Σ |Δsetting_t|` over controller writes (TV norm of the controller's dial trajectory). |

### 1.3 Responsiveness

A **discomfort signal** is active at minute `t` when *either* condition holds:

1. A user `override` row exists in `(t-1min, t]`, **or**
2. `bed_occupied_{zone} = TRUE` and the body trend
   `body_avg_f(t) − body_avg_f(t-15min)` exceeds **±0.5 °F per 15 min**
   *away from* the 80 °F target (i.e. body warming above 80, or cooling
   below 80).

A **corrective write** at minute `t'` is a controller write whose
`Δsetting` sign reduces the deviation: cooler dial (`Δsetting < 0`) when
the signal is "too warm"; warmer dial (`Δsetting > 0`) when "too cold".

| Metric | Definition |
|---|---|
| `time_to_correct_median_min`  | Median over discomfort-signal events of `(t' − t)` for the first corrective controller write within 30 min. Events with no corrective write inside 30 min contribute the censoring value 30. |
| `unaddressed_discomfort_min`  | Total in-bed minutes where `body_avg_f` lies outside `[78.5, 81.5]` (target ±1.5 °F) **and** no corrective controller write occurred in the trailing 15 min. |

### 1.4 Comfort outcomes

Body channel: `controller_readings.body_avg_f` (per-zone — the v5/v6
controllers already populate the per-zone `body_left_f`/`body_right_f`
and an averaged `body_avg_f`; the latter is what the body-FB loop reads).
Restrict to rows with `bed_occupied_{zone} = TRUE`.

| Metric | Definition (target = 80 °F) |
|---|---|
| `body_in_target_band_pct` | % of in-bed minutes with `body_avg_f ∈ [78.5, 81.5]` (target ± 1.5 °F). |
| `cold_minutes`            | Minutes with `body_avg_f < 78.0` (target − 2 °F). |
| `warm_minutes`            | Minutes with `body_avg_f > 82.0` (target + 2 °F). |

Minute-resolution: `controller_readings` is 5-min cadence, so each row
contributes 5 to the minute counts. Any cycle where the row is missing
contributes 0 (gaps are fail-closed for outcome metrics so that an
outage doesn't silently look like a perfect night).

---

## 2. Schema migration — `v6_nightly_summary`

The existing `v6_nightly_summary` already has `night, zone,
controller_version, regime_histogram, override_count, minutes_above_86f,
minutes_above_84f, minutes_below_72f, rail_engagements, fallback_events,
divergence_guard_activations, proxy_minutes_score_ge_05, notes` (verified
by `\d v6_nightly_summary` on 2026-05-04). We extend it additively. Save
as `sql/v6_eval_metrics.sql`:

```sql
-- 2026-05-04 evaluation framework: per-night metric columns.
-- Pure additive, idempotent, reversible by DROP COLUMN IF EXISTS.
BEGIN;

ALTER TABLE v6_nightly_summary
    ADD COLUMN IF NOT EXISTS user_id                  TEXT,
    ADD COLUMN IF NOT EXISTS bed_onset_ts             TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS wake_ts                  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS in_bed_minutes           INTEGER,

    -- §1.1 Discomfort proxy
    ADD COLUMN IF NOT EXISTS adj_count_per_night      INTEGER,
    ADD COLUMN IF NOT EXISTS adj_magnitude_sum        INTEGER,
    ADD COLUMN IF NOT EXISTS adj_weighted_score       DOUBLE PRECISION,

    -- §1.2 Stability
    ADD COLUMN IF NOT EXISTS oscillation_count        INTEGER,
    ADD COLUMN IF NOT EXISTS overcorrection_rate      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS setting_total_variation  INTEGER,

    -- §1.3 Responsiveness
    ADD COLUMN IF NOT EXISTS discomfort_event_count       INTEGER,
    ADD COLUMN IF NOT EXISTS time_to_correct_median_min   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS unaddressed_discomfort_min   INTEGER,

    -- §1.4 Comfort outcomes
    ADD COLUMN IF NOT EXISTS body_in_target_band_pct  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cold_minutes             INTEGER,
    ADD COLUMN IF NOT EXISTS warm_minutes             INTEGER,

    -- Audit
    ADD COLUMN IF NOT EXISTS metrics_target_f         DOUBLE PRECISION DEFAULT 80.0,
    ADD COLUMN IF NOT EXISTS metrics_computed_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS metrics_source_commit    TEXT;

CREATE INDEX IF NOT EXISTS idx_v6_nightly_user_night
    ON v6_nightly_summary (user_id, night DESC);
CREATE INDEX IF NOT EXISTS idx_v6_nightly_ver_night
    ON v6_nightly_summary (controller_version, night DESC);

COMMIT;
```

Rollback (in `sql/v6_eval_metrics_rollback.sql`): the symmetric
`ALTER TABLE … DROP COLUMN IF EXISTS …` for each new column, plus the two
`DROP INDEX IF EXISTS` statements.

---

## 3. End-of-night batch pipeline

### 3.1 Where it lives

`tools/eval_nightly.py` — a standalone Python script using `psycopg2`
(already a project dep). Runs as a launchd cron on the Mac Mini at
**09:00 ET** (after wake). Default invocation:

```bash
.venv/bin/python tools/eval_nightly.py --night yesterday
.venv/bin/python tools/eval_nightly.py --night 2026-05-03 --rebuild
```

`--rebuild` deletes any prior row for `(night, zone, controller_version)`
and recomputes. No-arg default = "yesterday in America/New_York". The
controller hot path is **not** touched; this is strictly read+write batch.

### 3.2 Bed-window detection

Verified state of the world (2026-05-04):

* `nightly_summary.bedtime_ts` and `wake_ts` exist and are populated for
  every night since 2026-04-28 (queried directly).
* `controller_readings.bed_occupied_left` / `bed_occupied_right` are
  populated per-cycle and are the source of truth for *zone-specific*
  occupancy.
* `appdaemon/sleep_controller_v5.py` records `_left_bed_onset_ts` /
  `_right_bed_onset_ts` (per the bed-onset patch, PROGRESS §12) but does
  not persist them to PG.

Detection rule used by `eval_nightly.py`:

```sql
-- bed_onset_ts: first row of the night where bed_occupied_<zone> = TRUE
-- after the 18:00 ET pivot of the target date.
WITH zone_rows AS (
    SELECT ts,
           CASE WHEN %(zone)s = 'left' THEN bed_occupied_left
                ELSE bed_occupied_right END AS occ
    FROM controller_readings
    WHERE zone = %(zone)s
      AND ts >= ((%(night)s::date)::timestamp + interval '18 hours') AT TIME ZONE 'America/New_York'
      AND ts <  ((%(night)s::date + 1)::timestamp + interval '15 hours') AT TIME ZONE 'America/New_York'
),
onset AS (
    SELECT MIN(ts) AS bed_onset_ts FROM zone_rows WHERE occ IS TRUE
),
-- wake_ts: last contiguous TRUE run end before a >=20-min gap of FALSE.
runs AS (
    SELECT ts, occ,
           SUM(CASE WHEN occ IS DISTINCT FROM lag(occ) OVER (ORDER BY ts)
                    THEN 1 ELSE 0 END) OVER (ORDER BY ts) AS grp
    FROM zone_rows
),
true_runs AS (
    SELECT grp, MIN(ts) AS run_start, MAX(ts) AS run_end
    FROM runs WHERE occ IS TRUE GROUP BY grp
),
-- The "wake" is the end of the longest occupied run that started after onset.
chosen AS (
    SELECT run_end FROM true_runs
    WHERE run_start >= (SELECT bed_onset_ts FROM onset)
    ORDER BY (run_end - run_start) DESC LIMIT 1
)
SELECT (SELECT bed_onset_ts FROM onset) AS bed_onset_ts,
       (SELECT run_end       FROM chosen) AS wake_ts;
```

Sanity-check vs `nightly_summary.bedtime_ts/wake_ts` (whole-bed window):
if zone-onset is more than 90 min off, fall back to `nightly_summary`
values and log `notes -> 'bed_window_fallback'`.

### 3.3 Metric SQL (excerpts — script substitutes the bed-window CTE)

```sql
-- Adjustments (§1.1)
WITH bw AS (SELECT %(bed_onset)s::timestamptz AS s,
                   %(wake)s::timestamptz       AS e),
ovr AS (
    SELECT override_delta
    FROM controller_readings, bw
    WHERE zone = %(zone)s
      AND action = 'override'
      AND ts >= bw.s AND ts < bw.e
)
SELECT COUNT(*)                                              AS adj_count_per_night,
       COALESCE(SUM(ABS(override_delta)), 0)::int            AS adj_magnitude_sum,
       COUNT(*) + 0.5 * COALESCE(SUM(ABS(override_delta)),0) AS adj_weighted_score
FROM ovr;

-- Stability (§1.2): controller-write delta series.
WITH bw AS (SELECT %(bed_onset)s::timestamptz AS s,
                   %(wake)s::timestamptz       AS e),
cw AS (
    SELECT ts, setting,
           setting - lag(setting) OVER (ORDER BY ts) AS d
    FROM controller_readings, bw
    WHERE zone = %(zone)s
      AND action IN ('controller','init','rail')
      AND ts >= bw.s AND ts < bw.e
),
nz AS (SELECT ts, d FROM cw WHERE d IS NOT NULL AND d <> 0),
flips AS (
    SELECT COUNT(*) FILTER (
        WHERE sign(d) <> 0
          AND sign(d) <> sign(lag(d) OVER (ORDER BY ts))
          AND lag(d) OVER (ORDER BY ts) IS NOT NULL
    ) AS oscillation_count
    FROM nz
),
overc AS (
    SELECT COUNT(*) FILTER (
        WHERE EXISTS (
            SELECT 1 FROM nz n2
            WHERE n2.ts > nz.ts
              AND n2.ts <= nz.ts + interval '10 min'
              AND sign(n2.d) = -sign(nz.d)
              AND sign(nz.d) <> 0
        )
    )::float / NULLIF(COUNT(*), 0) AS overcorrection_rate
    FROM nz
)
SELECT (SELECT oscillation_count FROM flips)             AS oscillation_count,
       (SELECT overcorrection_rate FROM overc)           AS overcorrection_rate,
       COALESCE(SUM(ABS(d)), 0)::int                     AS setting_total_variation
FROM nz;

-- Comfort outcomes (§1.4): minute counts (each 5-min row contributes 5).
WITH bw AS (SELECT %(bed_onset)s::timestamptz AS s,
                   %(wake)s::timestamptz       AS e),
occ AS (
    SELECT body_avg_f
    FROM controller_readings, bw
    WHERE zone = %(zone)s
      AND ts >= bw.s AND ts < bw.e
      AND ((zone = 'left'  AND bed_occupied_left  IS TRUE)
        OR (zone = 'right' AND bed_occupied_right IS TRUE))
      AND body_avg_f IS NOT NULL
)
SELECT 5 * COUNT(*) FILTER (WHERE body_avg_f BETWEEN 78.5 AND 81.5)
         / NULLIF(5.0 * COUNT(*), 0) * 100.0  AS body_in_target_band_pct,
       5 * COUNT(*) FILTER (WHERE body_avg_f < 78.0)  AS cold_minutes,
       5 * COUNT(*) FILTER (WHERE body_avg_f > 82.0)  AS warm_minutes,
       5 * COUNT(*)                                   AS in_bed_minutes
FROM occ;
```

Responsiveness (§1.3) is computed in Python because the censored
event→write join is awkward in pure SQL: pull the event timestamps and
the controller-write series, then for each event walk forward 30 min and
record the first sign-correct write or the censoring value.

### 3.4 Upsert

```sql
INSERT INTO v6_nightly_summary
    (night, zone, controller_version, user_id,
     bed_onset_ts, wake_ts, in_bed_minutes,
     adj_count_per_night, adj_magnitude_sum, adj_weighted_score,
     oscillation_count, overcorrection_rate, setting_total_variation,
     discomfort_event_count, time_to_correct_median_min, unaddressed_discomfort_min,
     body_in_target_band_pct, cold_minutes, warm_minutes,
     metrics_target_f, metrics_computed_at, metrics_source_commit)
VALUES (...)
ON CONFLICT (night, zone, controller_version) DO UPDATE
SET user_id                    = EXCLUDED.user_id,
    bed_onset_ts               = EXCLUDED.bed_onset_ts,
    wake_ts                    = EXCLUDED.wake_ts,
    in_bed_minutes             = EXCLUDED.in_bed_minutes,
    adj_count_per_night        = EXCLUDED.adj_count_per_night,
    adj_magnitude_sum          = EXCLUDED.adj_magnitude_sum,
    adj_weighted_score         = EXCLUDED.adj_weighted_score,
    oscillation_count          = EXCLUDED.oscillation_count,
    overcorrection_rate        = EXCLUDED.overcorrection_rate,
    setting_total_variation    = EXCLUDED.setting_total_variation,
    discomfort_event_count     = EXCLUDED.discomfort_event_count,
    time_to_correct_median_min = EXCLUDED.time_to_correct_median_min,
    unaddressed_discomfort_min = EXCLUDED.unaddressed_discomfort_min,
    body_in_target_band_pct    = EXCLUDED.body_in_target_band_pct,
    cold_minutes               = EXCLUDED.cold_minutes,
    warm_minutes               = EXCLUDED.warm_minutes,
    metrics_target_f           = EXCLUDED.metrics_target_f,
    metrics_computed_at        = EXCLUDED.metrics_computed_at,
    metrics_source_commit      = EXCLUDED.metrics_source_commit;
```

The unique constraint already exists
(`v6_nightly_summary_night_zone_controller_version_key`).
`controller_version` is read from the per-night majority of
`controller_readings.controller_version`, so a v5.2→v6 swap mid-night
correctly produces two rows for the same date.

---

## 4. A/B comparison harness

`tools/eval_compare.py` — CLI that diffs two cohorts of nights from
`v6_nightly_summary` and prints a single decision-grade table.

### 4.1 Cohort selection

Two equivalent modes:

```bash
# Date-range mode
.venv/bin/python tools/eval_compare.py \
    --A "2026-04-25..2026-05-01" --A-label "v5.2" \
    --B "2026-05-02..2026-05-08" --B-label "v6_state" \
    --zone left

# controller_version-tag mode
.venv/bin/python tools/eval_compare.py \
    --A-version "v5_2_rc_off+%" --B-version "v6_state%" --zone left
```

### 4.2 Pairing

Default = **paired by night** when the same `night` exists in both cohorts
and the two rows belong to *different* `controller_version` values
(useful for a future shadow-vs-live comparison). Otherwise unpaired
two-sample. The script states which mode it used in the table footer.

### 4.3 Bootstrap CI

For each metric:

* `n_A`, `n_B` = number of nights with non-NULL value
* point estimates = median (robust to single bad nights)
* `Δ = median(B) − median(A)`
* 95% CI via 10,000-iteration paired bootstrap if paired, else
  cluster-bootstrap by night for unpaired
* p-value via a permutation test (shuffle cohort labels, 10,000 iters)

### 4.4 Output

```
Zone: left   |  paired nights: 6   |  A=v5.2 (12 nights)   B=v6_state (6 nights)

metric                          A       B       Δ       95% CI            p
adj_weighted_score              4.20    2.10   −2.10   [−3.50, −0.70]   0.01
adj_count_per_night             3.00    1.50   −1.50   [−2.50, −0.50]   0.02
oscillation_count              11.0     3.0    −8.0    [−12.0, −4.0]    <0.01
overcorrection_rate             0.18    0.07   −0.11   [−0.20, −0.03]   0.02
setting_total_variation        24      11      −13     [−18,   −7]      <0.01
time_to_correct_median_min     14.0     6.5    −7.5    [−12.0, −2.5]    0.02
unaddressed_discomfort_min     62      24      −38     [−65,   −12]     0.01
body_in_target_band_pct        58.4    74.1    +15.7   [+ 7.2, +23.4]   <0.01
cold_minutes                   45      30      −15     [−40,    +5]     0.21
warm_minutes                   12      18      + 6     [ −5,   +18]     0.42

Decision: ACCEPT (see §5).
```

The script exits `0` on ACCEPT, `1` on HOLD, `2` on REVERT (§5).

---

## 5. ACCEPT / HOLD / REVERT thresholds

A change ships only after **≥3 paired nights** (or ≥5 unpaired) of
controller-version data with the new code live. Decision is computed by
`eval_compare.py` and printed at the bottom of the table.

### 5.1 ACCEPT — all three must hold

1. `adj_weighted_score` improves (B < A) by **≥ 15 %**, *and* the upper
   95 % CI bound is < 0 (significant improvement).
2. `cold_minutes` does not worsen by more than **+10 min** (median diff).
3. `oscillation_count` does not worsen by more than **+25 %** of the A
   cohort median.

### 5.2 REVERT — any one triggers

* `adj_weighted_score` worsens (B > A) by **≥ 20 %** with lower CI > 0.
* `cold_minutes` increases by **≥ 30 min** (median diff).
* `body_in_target_band_pct < 50 %` for **> 50 %** of nights in cohort B
  (i.e., on more than half the nights the user spent the majority of
  in-bed minutes outside [78.5, 81.5]).
* Any new safety regression detected by the existing
  `right_overheat_safety` rail engagement count
  (`v6_nightly_summary.rail_engagements`) increasing by ≥3 events on any
  single night vs the historical ≤1.

### 5.3 HOLD — gather more data

Only when *neither* §5.1 nor §5.2 fires **and** the
`adj_weighted_score` 95 % CI crosses zero **and** no safety counter
moved. Continue collecting paired nights and re-run after each.

---

## 6. Required controller logging additions

For the metrics to be computable, the v6 controller (and the v5.2 it
replaces, where feasible) must emit the following per cycle. All are
*additive* to `controller_readings` columns already present in
`sql/v6_schema.sql` and already populated by the v6 scaffold; this list
documents what the eval harness depends on.

| Column / channel | Source | Used for |
|---|---|---|
| `regime` (existing) | v6 `regime.py` | sanity gating in `notes` JSONB; not in any metric directly |
| `state_estimate` (NEW, TEXT) | one of `cold|cool|neutral|warm|hot|unknown` derived from body vs target ± bands at write time | grouping responsiveness events by perceived state |
| `state_confidence` (NEW, REAL ∈ [0,1]) | from residual head LCB or, in v5.2, a constant 0.5 | weight responsiveness events; future calibration |
| `policy_reason` (NEW, TEXT) | short tag for which branch produced the setting (`cycle_baseline`, `body_fb_cold`, `cold_room_comp`, `bed_onset_max_cool`, `rail`, `override_freeze`, …) | attribution; lets us regress metrics on branch counts |
| `learner_offset_applied` (NEW, INTEGER) | the integer offset the learner contributed to `setting` this cycle | distinguish learner-driven vs baseline-driven oscillation |
| `discomfort_signal_active` (NEW, BOOLEAN) | TRUE when condition §1.3 is satisfied at write time | denominator for `time_to_correct` (no need to recompute) |

Schema migration to land alongside this proposal:

```sql
BEGIN;
ALTER TABLE controller_readings
    ADD COLUMN IF NOT EXISTS state_estimate            TEXT,
    ADD COLUMN IF NOT EXISTS state_confidence          REAL,
    ADD COLUMN IF NOT EXISTS policy_reason             TEXT,
    ADD COLUMN IF NOT EXISTS learner_offset_applied    INTEGER,
    ADD COLUMN IF NOT EXISTS discomfort_signal_active  BOOLEAN;
CREATE INDEX IF NOT EXISTS idx_controller_readings_policy_reason
    ON controller_readings (policy_reason)
    WHERE policy_reason IS NOT NULL;
COMMIT;
```

Until the controller is updated, these columns will be NULL and the eval
harness must compute the equivalents from raw signals (which §1.3 already
does for `discomfort_signal_active`). The harness writes its own audit
trail in `v6_nightly_summary.notes`, so we can later cross-check
controller-emitted vs harness-derived flags and confirm the controller
is reasoning correctly.

---

## 7. Backfill plan

We have ~30 nights of `controller_readings` (and the matching
`nightly_summary` rows from at least 2026-04-15 onward — verified live).
Backfill produces the historical baseline against which any v6 candidate
will be compared.

```bash
.venv/bin/python tools/eval_nightly.py --backfill \
    --from 2026-04-05 --to 2026-05-03 --rebuild
```

Equivalent SQL driver to enumerate the (night, zone) pairs:

```sql
WITH nights AS (
    SELECT generate_series(date '2026-04-05', date '2026-05-03', interval '1 day')::date AS night
),
zones AS (SELECT unnest(ARRAY['left','right']) AS zone)
SELECT n.night, z.zone
FROM nights n CROSS JOIN zones z
WHERE EXISTS (
    SELECT 1 FROM controller_readings r
    WHERE r.zone = z.zone
      AND r.ts >= ((n.night)::timestamp + interval '18 hours') AT TIME ZONE 'America/New_York'
      AND r.ts <  ((n.night + 1)::timestamp + interval '15 hours') AT TIME ZONE 'America/New_York'
);
```

The script loops over the result, runs the metric SQL of §3.3, and
upserts. Backfill is **idempotent** — re-running with `--rebuild` produces
the same row. The `metrics_source_commit` column records the git SHA of
the harness so we can detect "did the metric definition change since this
row was written" later.

Expected row count after backfill: ~30 nights × 2 zones ≈ 60 rows
(controller_version mostly `v5_2_rc_off`/`v5_rc_off`).

---

## 8. Self-rejection criteria for the framework

We require that the metrics correlate with the user's *subjective*
morning report, otherwise we are optimising the wrong thing.

* The user logs a single morning rating
  `input_number.snug_morning_comfort_rating` (1–5, 5 = best). Already
  exists; if not, create it as part of landing this proposal.
* `tools/eval_nightly.py` records the rating (read from HA REST and
  stored in `v6_nightly_summary.notes -> 'morning_rating'`).
* After **14 morning reports** (≈ 2 weeks), compute Spearman ρ between
  `adj_weighted_score` (lower = better, sign-flip) and the rating.
* **Acceptance:** ρ ≥ 0.5 (with 95 % CI lower bound ≥ 0.2).
* **Rejection:** if not, the metric set is wrong and §1 must be revisited
  before any controller-version decision is made on metric grounds. Until
  ρ is established, the §5 ACCEPT thresholds are advisory only and
  changes additionally require user "yes" confirmation.

---

## 9. Files to create / modify

| File | Action |
|---|---|
| `sql/v6_eval_metrics.sql`             | NEW (§2 migration) |
| `sql/v6_eval_metrics_rollback.sql`    | NEW (symmetric drops) |
| `sql/v6_controller_logging.sql`       | NEW (§6 controller column adds) |
| `tools/eval_nightly.py`               | NEW (§3 batch) |
| `tools/eval_compare.py`               | NEW (§4 A/B) |
| `tools/launchd/com.snug.eval-nightly.plist` | NEW (09:00 ET trigger on Mac Mini) |
| `appdaemon/sleep_controller_v5.py` / `_v6.py` | MODIFY to populate §6 columns |
| `tests/test_eval_nightly.py`          | NEW (golden-night regression) |
| `tests/test_eval_compare.py`          | NEW (synthetic cohorts) |

---

## 10. Open questions

1. Are there nights in the corpus where the user toggled the topper to
   *manual* mid-night? Those rows have `manual_mode=TRUE` in
   `nightly_summary` and should likely be excluded from cohorts. Confirm
   policy before backfill.
2. Right-zone overrides are sparse (partner rarely adjusts). Sample-size
   rules in §5 may need a per-zone relaxation; revisit after first
   right-zone v6 cohort.
3. The "minute-resolution" outcome metrics multiply the 5-min cadence by
   5. If the v6 logger ever moves to 1-min cadence, the SQL in §3.3 must
   drop the `5 *` factors. A `cycle_seconds` column on `controller_readings`
   would future-proof this; out of scope here.
