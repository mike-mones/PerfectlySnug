# Right-Zone Rollout Analysis — 2026-04-30

**Goal:** decide what blocks shipping a per-cycle controller and a tuned safety rail to the wife's side.

**Headline finding:** the "wife runs 88°F+" story is **wrong**. It's the
**bed-center sensor (`body_center_f`) under her sheet pile that reads high**,
not her actual body temperature. The skin-side sensor (`body_left_f`) shows
her operating range is statistically indistinguishable from the user's.

---

## 1. The data that flips the framing

Per-zone body sensor percentiles, BedJet-window filtered (>30 min after
right-bed onset, occupied only):

| sensor | wife (right zone) p50 | p95 | p99 | user (left zone) p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|---:|
| `body_left_f`   (skin-side) | **79.7** | **86.5** | **88.7** | 79.3 | 84.7 | 86.6 |
| `body_center_f` (sheet-heat side) | 86.1 | 95.5 | — | 83.3 | 88.3 | — |
| `body_right_f`  (sheet-heat side) | 83.0 | 94.5 | — | — | — | — |
| `body_avg_f`    (mean of all 3)   | 83.8 | 94.5 | 96.1 | 82.6 | 88.3 | 89.9 |

Two things to take from this:

1. **`body_left_f` is the right operational signal for both zones.** It
   reads ~80°F median in both, ~88°F at p99 in both. The 88°F "natural
   ceiling" you stated as physiology is *correct* — it just has to be
   measured at the skin-adjacent sensor, not at the warm-sheet sensors or
   the average that includes them.
2. **`body_center_f` and `body_right_f` are dominated by sheet/blanket heat
   on the wife's side.** The 88-95°F range that the previous threshold
   tuning was reacting to is largely sheet temperature, not skin
   temperature.

The implication: nothing physiologically unique about the wife. The
problem is *which sensor we listen to*.

---

## 2. What's currently miscalibrated

`appdaemon/right_overheat_safety.py:64`:

```python
E_BODY_CENTER_R = "sensor.smart_topper_right_side_body_sensor_center"
```

The rail reads the **center** sensor and engages at ≥88°F. With her
`body_center_f` p50 = 86.1°F and p95 = 95.5°F, **the rail would fire on
roughly half her occupied minutes** post-BedJet-window. That's exactly the
energy fight you don't want — topper max-cooling against the warm sheets
while she's actually fine.

This is also why the *current* configuration (rail enabled with
`input_boolean.snug_right_overheat_rail_enabled` and BedJet suppression
deployed today) is still unsafe to flip on without the sensor swap. The
BedJet 30-min suppression buys us the *startup* phase, but not the rest
of the night.

---

## 3. Recommended right-zone rollout sequence

### Phase 1 — Sensor swap (safe, ship now)

Change `right_overheat_safety.py` to read the right-zone *body-left*
sensor (closest to her body) instead of *center*:

```python
# Before
E_BODY_CENTER_R = "sensor.smart_topper_right_side_body_sensor_center"

# After
E_BODY_LEFT_R = "sensor.smart_topper_right_side_body_sensor_left"
```

Engage threshold can stay at 88°F: that's at her own p99 of `body_left_f`,
matching the user's p99 + 2°F (a standard 2-sigma overheat ceiling). The
release threshold of 84°F is well below her p95 (86.5°F) so it won't
chatter.

**Effect:** rail engages only when she's *actually* overheating (skin
temperature 88+°F sustained 2 min), not when the topper cooling is fine
but the sheets happen to be warm. Should reduce false engagements from
~50% of minutes to <1%.

**Risk:** misses an overheat case where center is hot but skin is fine —
but if skin is fine she's *not* overheating in any meaningful sense, so
this is the correct trade.

### Phase 2 — Right-zone shadow controller (1-2 weeks of data)

Add a "right" branch to `sleep_controller_v5.py` that **logs but does not
act**: same cycle baseline computation as left, but reads
`body_right_side_left_sensor` for body and writes to a JSONL log
(`/config/snug_right_shadow.jsonl`) rather than calling
`set_setting`. This is the same shadow pattern already used for
`ml.policy`.

Goal: collect 1–2 weeks of "what would v5.1 have set on her side" data
plus any overrides she logs, so we can:
- Confirm the cycle structure (90-min cycles with the same baseline
  shape) actually fits her data, not just yours.
- Refit per-cycle baselines on *her* override corpus once we have ≥30
  events. Right now we have **6** (1 c1, 4 c2, 1 core). Way too few.

### Phase 3 — Right-zone live (when the data supports it)

Wire the right branch's output to her bedtime-temperature entity. Gate
behind `input_boolean.snug_right_controller_enabled` (mirroring the
left-side pattern) so it can be killed instantly. Use her own override
corpus (≥30 events, ideally 50+) for the per-cycle baselines; do not just
copy the user's v5.1 values because the override-direction signal is
already different (4 of 6 are cooler-please vs the user's 6 of 39 c4
events all being warmer-please).

**Estimated time:** 4-8 weeks at her current ~0.4 overrides/night cadence.
The discomfort proxy pipeline can shorten this if `sig_body_sd_q4`
proves to recover proxy-discomfort minutes for her zone too — that
needs her own validation pass through `tools/build_discomfort_corpus.py`
parameterized for `zone='right'`.

---

## 4. What changes from yesterday's plan

The 2026-04-30 v5.1 ship report assumed her side just needed the BedJet
window and her own per-cycle fitter. That's still true, but:

- **Add a Phase-1 sensor swap before Phase 2.** Without it, the safety
  rail is unusable on her side (would fire constantly on warm-sheet
  readings).
- **Don't widen the BedJet window.** The data shows the contamination
  *isn't* prolonged BedJet airflow — `body_left_f` shows normal skin temps
  all night. The sheets just stay warm because they're under blankets and
  next to a body, which is exactly what sheets do.
- **The discomfort proxy pipeline ran end-to-end as of 2026-04-30 14:40
  ET.** Verdict on the candidate refit was HOLD (MAE 2.73 vs v5 1.71) —
  the proxy direction in `tools/refit_with_proxy_labels.py` (`PROXY_DIR=-1`,
  prefer cooler) is wrong for the user (left zone wants warmer); needs
  per-zone direction logic. Tracked separately.

---

## 5. Numbers, for reference

```
Right zone, BedJet-window filtered, occupied (n=1123):
  body_avg_f      p50=83.8  p95=94.5  p99=96.1
  body_left_f     p50=79.7  p95=86.5  p99=88.7   ← skin signal
  body_center_f   p50=86.1  p95=95.5             ← sheet signal
  body_right_f    p50=83.0  p95=94.5             ← sheet signal

Left zone (user), occupied (n=1132):
  body_avg_f      p50=82.6  p95=88.3  p99=89.9
  body_left_f     p50=79.3  p95=84.7  p99=86.6   ← skin signal

Right-zone overrides (n=6):
  2026-04-15 13:43 core    em=60   set=-5  od=+1  pref=-4   bl=83.5  bc=84.0  br=84.5
  2026-04-15 23:20 cycle_2 em=140  set=-4  od=+2  pref=-2   bl=74.6  bc=78.2  br=79.1
  2026-04-21 23:01 cycle_2 em=121  set=-6  od=-2  pref=-8   bl=77.2  bc=81.8  br=79.7
  2026-04-24 22:38 cycle_2 em=99   set=-6  od=-1  pref=-7   bl=77.8  bc=80.1  br=82.6
  2026-04-25 23:30 cycle_2 em=150  set=-6  od=-2  pref=-8   bl=75.4  bc=85.9  br=85.5
  2026-04-26 21:09 cycle_1 em=10   set=-9  od=-3  pref=-12  bl=70.5  bc=71.3  br=73.9

  4 of 6 cooler-please. None of the 6 took effect at body_left_f ≥ 84°F
  (her p95). All overrides are early-night (cycles 1-2, none past elapsed
  150min). She's never overridden during the second half of the night.
  Either she sleeps through, or her late-night reality is acceptable.
```

---

## 6. Concrete next decisions for you

1. ✋ **Approve the sensor swap (Phase 1)** — `body_center_f` →
   `body_left_f` in `right_overheat_safety.py`. Keep 88°F engage, 84°F
   release, 30-min BedJet suppression. Ready to deploy in <5 min once
   approved.

2. ⏳ **Decide on Phase 2 timing** — shadow logger for the right zone is
   ~30 lines added to `sleep_controller_v5.py`. Worth doing in the next
   few days if you want to start collecting right-zone "what-would-v5-do"
   data.

3. ⏳ **Apple Watch sleep-stage gap** — Some nights have only 1-6 stage
   segments instead of the typical 16-21. Worth tracing the iOS Health
   Receiver → PG ingestion to find why. Not blocking, but the discomfort
   proxy's recall climbs with denser stage data.
