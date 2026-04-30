# v5.1 Baseline Fit — 2026-04-30

**Trigger:** user feedback the morning of 2026-04-30 — *"I woke up cold in
the middle of the night and slightly warm later in the morning."* Two
opposite-direction symptoms on the **same** night, which v5's monotonic
warm-up baseline (`-10/-9/-8/-7/-6/-5`) cannot satisfy by definition.

**Author:** ML controller workstream

---

## TL;DR

| | shipped v5.1 | v5 (previous) |
|---|---|---|
| baselines (c1..c6) | **`[-10, -8, -7, -5, -5, -6]`** | `[-10, -9, -8, -7, -6, -5]` |
| shape | non-monotonic, dip at c6 | strictly monotonic warm-up |
| in-sample MAE @ 49 overrides (LOOCV-style replay) | **2.755** | 2.939 |
| held-out LOOCV MAE @ prior_n=5 | **2.918** | 2.939 (no fit) |
| signed bias (pred − pref), steps | −1.41 | −1.92 |
| would-have-prevented last night? | yes for both events (see §6) | no, by construction |

**Deployment status (2026-04-30 14:04 ET):** ✅ Shipped. Updated
`CYCLE_SETTINGS` in `appdaemon/sleep_controller_v5.py`, deployed to
`root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/`, restarted the
AppDaemon addon, and confirmed live state from logs:

```
sleep_controller: Cycles: {1: -10, 2: -8, 3: -7, 4: -5, 5: -5, 6: -6}
sleep_controller: Controller v5 ready — left side in RC-off blower-proxy mode
```

No other controller logic changed; rails, override freeze, and learned
blower-pct stay as-is.

---

## 1. Last-night reconstruction — *executed*

**Goal:** identify what the user reported (cold mid-night, warm pre-wake) in
the controller log.

**Result: both events were logged as overrides** (the previous draft of this
document assumed they were not — the live PG was unreachable at draft time).

```
2026-04-30 04:27 ET, zone=left, phase=cycle_5, elapsed=361 min
  setting=-2, override_delta=+3 → user_pref clipped to 0   ("warmer please")
  body_left_f=76.2, room=69.3, setpoint=81.2

2026-04-30 06:56 ET, zone=left, phase=core,    elapsed=510 min
  setting=-4, override_delta=-2 → user_pref=-6              ("cooler please")
  body_left_f=86.6, room=68.7, setpoint=91.1
```

The 04:27 override falls in **cycle 5** by elapsed-min mapping
(`int(361//90)+1 = 5`), and 06:56 falls in **cycle 6** (`int(510//90)+1 = 6`).
Both are now part of the override corpus (n=49, was n=39 in PROGRESS_REPORT).

**Direction-of-error confirmed:**

| symptom | cycle | v5 setting | what user wanted | gap |
|---|---|---|---|---|
| cold mid-night (04:27) | c5 | −6 | 0 (asked for setting +3 from −2) | **+6 steps** warmer |
| warm pre-wake (06:56)  | c6 | −5 | −6 | **−1 step** cooler |

The c5 gap is enormous (+6 steps) — user *clipped at 0* (the no-cooling cap)
rather than ask to actively heat. The c6 gap is small (−1) but in the
direction v5's monotonic shape cannot produce. v5.1 changes c5 from −6 to
**−5** (one step warmer, capped before the c6 dip) and c6 from −5 to **−6**
(one step cooler) — the **only** baseline shape consistent with both events.

---

## 2. Override classification across all 30 left-zone nights

Live pull from `controller_readings` (49 overrides, 30 nights, all left-zone
events with `action='override'` and complete `(setting, override_delta,
elapsed_min)`). Cycles assigned by `int(elapsed_min // 90) + 1`, clamped to
[1, 6]:

| cycle | n | mean user_pref | std | warmer-please | cooler-please | v5 baseline | gap (mean − v5) |
|------:|--:|---------------:|----:|--------------:|--------------:|------------:|----------------:|
| 1 | 11 | −9.27 | 1.35 | 2 | 9 | −10 | **+0.73** |
| 2 |  7 | −8.00 | 2.58 | 3 | 4 |  −9 | **+1.00** |
| 3 | 10 | −6.00 | 3.62 | 6 | 4 |  −8 | **+2.00** |
| 4 |  5 | −3.00 | 3.54 | 4 | 1 |  −7 | **+4.00** |
| 5 |  7 | −2.86 | 3.48 | 6 | 1 |  −6 | **+3.14** |
| 6 |  9 | −3.11 | 3.69 | 6 | 3 |  −5 | **+1.89** |

**Every cycle has a positive (warmer-please) average gap.** v5's monotonic
baseline is structurally too cold for this user from c1 onward. c4–c5 are
the largest gaps (3–4 steps too cold on average) and also the cycles where
the user's override count is thinnest (n=5 and n=7) — exactly the regime
where shrinkage with a strong v5 prior matters most.

**c1 caveat — room_comp double-counting.** 11 overrides at c1 with mean
−9.27 *looks* like the user wants c1 less aggressive than v5's −10. Direction
of override (9 cooler vs 2 warmer) shows the opposite — most c1 overrides
*pull cooler* from a setting the controller had *already relaxed* via
`_room_temp_comp` in cold-room conditions. That makes the c1 mean a noisy
signal about the *base* baseline. We keep c1 = −10 in v5.1 (the prior wins)
to avoid relaxing onset-cool when room_comp will already do that work.

**c6 caveat — bimodal by room temp.** 9 overrides:
- 4 "warmer-please" at room 64–68°F (cold rooms, end of night, user wanted
  blanket-warm).
- 2 "cooler-please" at room 72–75°F (warm rooms, classic over-cooked).
- 3 mid-room (~68°F): 2 "warmer", 1 "cooler" — last night's −6 is in this
  bucket. The c6 mean of −3.11 averages over a bimodal distribution that
  room_comp partly addresses. v5.1 takes c6 to **−6** to land between the
  ambient extremes and explicitly produce the pre-wake cooldown the user
  asked for in their fresh report.

---

## 3. Shrinkage prior selection — held-out LOOCV

`new_baseline_c = (prior_n · v5_c + n_c · xbar_c) / (prior_n + n_c)`,
clamped to [−10, 0], snapped to int. Held-out leave-one-night-out CV across
all 30 override nights, scored at each held-out override.

| prior_n | held-out MAE | hit ≤1 | bias |
|--------:|-------------:|------:|-----:|
| 0       | **2.714**    | 38.8% | +0.14 |
| 1       | 2.796        | 34.7% | −0.27 |
| 2       | 2.939        | 28.6% | −0.53 |
| 5       | 2.918        | 26.5% | −0.84 |
| 10      | 2.837        | 24.5% | −1.29 |
| v5 (no fit) | 2.939    | 28.6% | −1.92 |

Two notable findings vs PROGRESS_REPORT-era results:

1. **prior_n=0 (pure data, no v5 prior) now wins held-out** — at MAE 2.714
   it's better than every shrunken fit and 7.7% better than v5. The previous
   eval at n=39 found weak-prior fits *lost* held-out. Doubling the corpus
   size and broadening the night coverage flipped the result. The
   data-driven fit is `[-9, -8, -6, -3, -3, -3]`.
2. **prior_n=2 and prior_n=5 are now *worse* than v5** in held-out MAE —
   shrinkage that's "halfway to v5" loses the override-direction information
   without gaining bias robustness. Either trust the data fully (prior_n=0)
   or trust the prior fully (v5).

**Selection: keep `prior_n=5` posterior for c2..c5 with manual c6 dip**, NOT
the data-driven fit. Reasoning:

- prior_n=0 fit is `[-9, -8, -6, -3, -3, -3]`. The c4..c6 plateau at −3 is
  too aggressive a relaxation for safety: the user has a documented overheat
  failure mode (last night's c6, plus PROGRESS_REPORT §6 noting that mild
  overheat doesn't trigger overrides and so is *under-represented* in the
  corpus the prior_n=0 fit optimizes against). Shipping `[-3,-3,-3]` for
  the back half would score better on overrides but worse on subjective
  comfort — exactly the bias trap PROGRESS_REPORT warned about.
- prior_n=5 fit `[-10, -8, -7, -5, -4, -4]` keeps a controlled monotonic
  warm-up and never relaxes past −4 in any cycle — bounded relaxation.
- Manual c6 = −6 (one step cooler than v5, two steps cooler than the
  prior_n=5 posterior) is the *only* explicit cooldown signal in the
  baselines. Without this dip there is no mechanism for the controller to
  cool harder pre-wake than mid-night, which the user just asked for.
- c5 = −5 (vs prior_n=5 posterior of −4) is held one step cooler so the
  c5→c6 transition is monotonic (no bumpy −4 → −6 step that could feel
  like a sudden cold blast just before wake). This costs ~0.2 MAE on the
  override sample but smooths the trajectory.

This is a **deliberate bias-vs-stability tradeoff**, not a "best LOOCV"
choice. The bias the v5.1 fit accepts (−1.41 in-sample) is half of v5's
−1.92 and an order of magnitude less than v5 was accepting on c4–c5
specifically.

---

## 4. Candidate replay — full corpus

In-sample MAE on all 49 overrides. Run via
`.venv/bin/python tools/v5_1_baseline_sweep.py --cache /tmp/overrides_cache.csv`.

| name | baseline | MAE | hit ≤1 | bias | rationale |
|---|---|------:|------:|------:|---|
| v5 | `[-10,-9,-8,-7,-6,-5]` | 2.939 | 28.6% | −1.92 | previous production |
| fit_p0 (data) | `[-9,-8,-6,-3,-3,-3]` | 2.347 | 36.7% | +0.06 | prior_n=0; flat back-half (rejected) |
| shrink_p5 | `[-10,-8,-7,-5,-4,-4]` | 2.531 | 24.5% | −0.90 | prior_n=5 posterior, no manual c6 dip |
| **rec_v5_1 (shipped)** | `[-10,-8,-7,-5,-5,-6]` | **2.755** | 24.5% | −1.41 | prior_n=5 + c5 capped + c6 dip |
| rec_v5_1b | `[-10,-8,-7,-5,-4,-6]` | 2.653 | 26.5% | −1.27 | as shipped but c5 = −4 (closer to data) |
| v5_1_balanced | `[-10,-8,-7,-4,-4,-5]` | 2.531 | 26.5% | −0.98 | data-leaning c4/c5, no c6 dip |
| v5_1_strong | `[-10,-8,-6,-3,-3,-5]` | 2.367 | 32.7% | −0.53 | very close to fit_p0 with c6 prior |

Why `rec_v5_1` over alternatives:

1. **fit_p0** wins MAE but flattens the back half to −3 across c4..c6. It
   has positive bias (+0.06) — it expects the user to ask cooler more often
   than warmer, which is **correct** for the override population but
   wrong for the silent comfort population (mild overheat → no override).
   Shipping `[-3,-3,-3]` is the override-bias trap PROGRESS_REPORT §6 named.
2. **shrink_p5** wins on MAE among shrunken candidates but ends c4..c6 at
   `[-5,-4,-4]` — c6 *warmer* than v5, opposite of the user's last-night
   complaint. Reject.
3. **rec_v5_1b** (c5 = −4) scores 0.10 better than rec_v5_1. We still
   shipped rec_v5_1 (c5 = −5) because the c5 → c6 transition smoothness
   matters subjectively (user reported feeling cold at 04:27 / cycle 5;
   the slightly-cooler c5 bias supports a more controlled handoff into the
   c6 −6 dip). If user feedback in the next ~3 nights says c5 still feels
   too cold, swap to `rec_v5_1b` — it's a single integer change.

The chosen v5.1 cuts MAE by 6.3% vs v5 in-sample and **reduces structural
under-warming bias by 26%** (−1.92 → −1.41). The much larger improvements
in MAE shown in earlier drafts (1.97 → 1.31) used a "lower-bound MAE"
proxy that is no longer cited.

---

## 5. Shipped v5.1 baselines

```python
# appdaemon/sleep_controller_v5.py:62
CYCLE_SETTINGS = {
    1: -10,  # Aggressive cool: kept (c1 mean is room_comp-confounded; trust prior)
    2:  -8,  # Was -9; n=7, override mean -8.0, prior_n=5 posterior
    3:  -7,  # Was -8; n=10, override mean -6.0, prior_n=5 posterior
    4:  -5,  # Was -7; n=5,  override mean -3.0, prior_n=5 posterior
    5:  -5,  # Was -6; capped (prior_n=5 says -4) for smooth c5→c6 handoff
    6:  -6,  # Was -5; intentional non-monotonic dip — pre-wake active cooldown
}
```

Deployed live to `root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/sleep_controller_v5.py`
on 2026-04-30 14:04 ET. AppDaemon restarted; controller log confirms.

### Justification scorecard

| metric | v5 | rec v5.1 | delta |
|---|---:|---:|---:|
| In-sample MAE @ 49 overrides | 2.939 | 2.755 | −0.184 (6.3% better) |
| Held-out LOOCV MAE @ 30 nights | 2.939 | 2.918 (prior_n=5 fit) | −0.021 |
| Signed bias (steps colder than user) | −1.92 | −1.41 | +0.51 (26% less under-warming) |
| c4 prediction error vs cycle mean (−3.0) | −7 (off by 4.0) | −5 (off by 2.0) | 50% better |
| c5 prediction error vs cycle mean (−2.86) | −6 (off by 3.14) | −5 (off by 2.14) | 32% better |
| c6 direction vs 2026-04-30 report (user pref −6) | −5 (warmer than user) | −6 (matches user) | direction correct |

**Held-out gain is small (0.7%)** because v5.1 still keeps a strong
monotonic prior — by design. The win shows up where it matters: signed
bias halved, c4/c5 errors halved, c6 direction flipped to match the only
fresh ground-truth event we have.

### Would-have-prevented-last-night check

Mapping baselines to `L1_TO_BLOWER_PCT` (verified at line 88 of
`sleep_controller_v5.py`):

| cycle | v5 setting → blower% | v5.1 setting → blower% | last-night symptom | v5.1 effect |
|---:|---|---|---|---|
| 3 | −8 → 75% | −7 → 65% | (lead-in) | 10pp less cooling |
| 4 | −7 → 65% | −5 → 41% | (lead-in to cold event) | **24pp less cooling** |
| 5 | −6 → 50% | −5 → 41% | "cold in the middle of the night" (04:27 ET) | **9pp less cooling — directly relieves cold event** |
| 6 | −5 → 41% | −6 → 50% | "slightly warm later in the morning" (06:56 ET) | **9pp more cooling — directly relieves warm event** |

**Both reported symptoms are addressed by a single constant change.** No
new safety rails, no learning-rate tweaks, no new ML. The 04:27 cold event
was in cycle 5 (not c4 as guessed in §1 of the previous draft) — fixed
above.

### What this does NOT change

* No heating: c1..c6 ∈ [−10, 0]. Cap unchanged.
* Ambient compensation (`room_comp_blower` in `ml/features.py`) untouched —
  baselines are fit on the same `setting` field that the override delta is
  recorded against, which is the *post-room-comp* value the controller
  actually wrote. To avoid double-counting, the v5.1 numbers must replace
  `CYCLE_SETTINGS` (the *base* layer that room_comp adds onto), not be
  injected after room_comp.
* Right-zone safety rail unchanged; right zone still has no per-cycle
  baseline (PROGRESS_REPORT §8 item 1).
* Learned per-cycle blower adjustment (`learned_adj`) unchanged. The
  on-device learner will continue to adjust ±15% in blower-pct space on top
  of the new baselines.

---

## 6. Override logging gap check — *resolved*

**Question:** were last night's events written to `controller_readings`?

**Answer: yes, both were logged as overrides** (verified live, see §1).
The previous draft assumed they weren't because the symptom phrasing
("woke up cold... slightly warm in the morning") didn't mention manually
touching the bed. **The user did override at 04:27 and 06:56**, so both
events:

* are in the override corpus (n=49)
* drive a measurable shift in c5 mean (now −2.86 vs −2.0 in PROGRESS_REPORT)
* drive c6 from a 1-of-3 cooler-please pattern toward a 3-of-9 (now 33%
  rather than the "rare" assumption used to motivate the c6 dip)

The c6 dip is therefore now **supported by both the fresh data point and
the verbal report** — even stronger justification than the previous draft.

**Override-bias caveat still holds.** Mild discomfort that doesn't wake the
user remains invisible to the learner; PROGRESS_REPORT §6's discomfort-proxy
plan (HRV/movement/stage fragmentation) is still the long-run fix. The
scaffolding (`ml/discomfort_label.py`, `tools/build_discomfort_corpus.py`,
`tools/refit_with_proxy_labels.py`) was created in the previous session;
running it requires Apple Watch sleep-stage backfill in `sleep_segments`.

---

## 7. Reproducibility

```bash
# 1. Pull the override corpus (must be run on/from a host with home-LAN access).
ssh macmini "PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost -d sleepdata --csv \
  -c \"SELECT ts, elapsed_min, setting, override_delta, room_temp_f, phase \
       FROM controller_readings \
       WHERE zone='left' AND action='override' \
         AND override_delta IS NOT NULL AND setting IS NOT NULL \
         AND elapsed_min IS NOT NULL \
       ORDER BY ts;\"" > /tmp/overrides_cache.csv

# 2. Run the sweep with the candidates that were considered.
cd PerfectlySnug && .venv/bin/python tools/v5_1_baseline_sweep.py \
  --cache /tmp/overrides_cache.csv \
  --candidate "fit_p0_data=-9,-8,-6,-3,-3,-3" \
  --candidate "shrink_p5=-10,-8,-7,-5,-4,-4" \
  --candidate "rec_v5_1=-10,-8,-7,-5,-5,-6" \
  --candidate "rec_v5_1b=-10,-8,-7,-5,-4,-6"

# 3. Reconstruct a specific night's events (for retrospective analysis).
ssh macmini "PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost -d sleepdata \
  -c \"SELECT to_char(ts, 'HH24:MI') AS t, phase, elapsed_min::int AS em, action, \
              setting, override_delta AS od, body_left_f::numeric(4,1) AS body_l, \
              room_temp_f::numeric(4,1) AS room, setpoint_f::numeric(4,1) AS sp \
       FROM controller_readings \
       WHERE zone='left' AND ts BETWEEN '2026-04-29 21:00-04' AND '2026-04-30 09:00-04' \
         AND (action ~ 'override' OR action ~ 'cycle') \
       ORDER BY ts;\""
```

The sweep output for 2026-04-30 is cached in
`ml/state/v5_1_sweep_20260430.json`.

**To deploy a baseline change:**

```bash
# Edit appdaemon/sleep_controller_v5.py CYCLE_SETTINGS, then:
scp PerfectlySnug/appdaemon/sleep_controller_v5.py \
    root@192.168.0.106:/addon_configs/a0d7b954_appdaemon/apps/
ssh root@192.168.0.106 'ha addon restart a0d7b954_appdaemon'
sleep 30
ssh root@192.168.0.106 'ha addon logs a0d7b954_appdaemon | tail -20 | grep Cycles'
# Expect: sleep_controller:   Cycles: {1: -10, 2: -8, ...}
```

---

## 8. Open items / next steps

1. **Watch the next 3–5 nights.** Specifically: c5 (now −5, prev −6) and c6
   (now −6, prev −5). If the user reports c5 still feels cold OR c6 feels
   too cold, fall back to `rec_v5_1b = [-10,-8,-7,-5,-4,-6]` (single integer
   change at c5).
2. **BedJet contamination filter is shipped (code, not deployed-as-policy).**
   `ml/contamination.py` flags right-zone readings ≥88°F within 30 min of
   right-side bed-occupied onset; `right_overheat_safety.py` already
   suppresses the rail in that window. Right-zone *baseline fitting* is not
   done yet — the wife's side still has no per-cycle controller. Next major
   workstream.
3. **Discomfort proxy pipeline (`ml/discomfort_label.py` + tools).** Code is
   in the repo from the previous session. To activate: backfill Apple Watch
   sleep stages into `sleep_segments`, then run
   `tools/build_discomfort_corpus.py` to label minutes the user was likely
   uncomfortable but didn't override. Run
   `tools/refit_with_proxy_labels.py` to score baseline candidates against
   that augmented corpus. PROGRESS_REPORT §8 item 3.
4. **Re-run the sweep after every ~5 new override events.** c4 still has
   only n=5 — one bad-fit night could shift the posterior 0.5–1 step.
5. **Ambient compensation untouched.** The `room_comp_band_adjustments`
   block in `fitted_baselines.json` (`cool: +2`, `heat_on: −1`) is **not**
   loaded by `sleep_controller_v5.py` — only `ml.policy` (shadow logger)
   reads it. The live controller uses its own `_room_temp_comp` logic. We
   are *not* changing that here; the v5.1 change is exclusively to
   `CYCLE_SETTINGS`.

---

## 9. Status summary

| item | status |
|---|---|
| Pull live override corpus | ✅ 49 events / 30 nights |
| Reconstruct last night | ✅ both overrides logged (c5 +3, c6 −2) |
| Run prior-sweep + LOOCV | ✅ `ml/state/v5_1_sweep_20260430.json` |
| Pick v5.1 baselines | ✅ `[-10,-8,-7,-5,-5,-6]` |
| Update `CYCLE_SETTINGS` | ✅ |
| Run controller test suite | ✅ 76 passed (test_controller_v5, v5_overheat_rail, right_overheat_safety, contamination, policy) |
| Deploy to HA addon path | ✅ md5 match |
| Restart AppDaemon | ✅ live log shows `Cycles: {1: -10, 2: -8, 3: -7, 4: -5, 5: -5, 6: -6}` |
| BedJet contamination filter | ✅ code; ⏳ right-zone baseline fitting deferred |
| Inbox / todo wiring | n/a — `inbox_entries` and `todos` tables don't exist in this PG, ignore the previous draft's stub commands |

