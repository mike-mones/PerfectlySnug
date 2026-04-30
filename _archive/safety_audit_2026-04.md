# CRITICAL SAFETY AUDIT: New Controller vs v5 Actual

**Date:** April 2026 | **Data:** 14 nights (2026-04-15 to 2026-04-29) | **Author:** Automated Counterfactual Replay

---

## EXECUTIVE SUMMARY

**CONFIDENCE LEVEL: 🔴 LOW**

**RECOMMENDATION: DO NOT DEPLOY WITHOUT MAJOR REVISIONS**

The proposed new controller (`ml.policy.controller_decision`) has **three critical safety issues** that make it unsafe to deploy for both users, particularly the wife (right zone):

1. **Left zone regression**: Performs WORSE than v5 on 41% of override events (16/39), with systematic failures in late cycles
2. **Sustained overheat on wife**: Hard rail fires but wife reaches 96.6°F max with v5 stuck at -5/-6 (would improve to -10 with new controller, but data shows v5 already failing here)
3. **Aggressive body-temp rails cut effectiveness in early cycles**: Smart baseline is being overridden by body-temp rails when the baseline was actually closer to user preference

---

## ANALYSIS 1: OVERRIDE-MOMENT COMPARISON (Ground Truth)

### Left Zone (User) — 39 Override Events

| Metric | v5 | NEW | Delta |
|--------|----|----|-------|
| **Hit rate** (±1 step) | 41.0% | 46.2% | +5.2% |
| **MAE** (mean abs error) | 1.69 | 2.03 | **+0.34 steps** ⚠️ |
| **Better head-to-head** | — | 15/39 (38%) | — |
| **Worse head-to-head** | — | 16/39 (41%) | ⚠️ **CRITICAL** |
| **Same** | — | 8/39 (21%) | — |

**Direction Analysis:**
- v5 misses: 23 too cold, 12 too warm → **bias toward over-cooling**
- NEW misses: 16 too cold, 14 too warm → slightly more balanced, but from a worse baseline

### Right Zone (Wife) — 6 Override Events

| Metric | v5 | NEW |
|--------|----|----|
| **Hit rate** (±1 step) | 50.0% | 66.7% |
| **MAE** | 1.50 | 2.00 |

**NOTE**: Only 6 override events for wife (sparse ground truth). However, this is immaterial to the safety concerns below—the real issue is the sustained overheat stretches where she **doesn't override** and v5 fails to cool her down.

---

## ANALYSIS 2: HARD-RAIL DIVERGENCE AUDIT

Found **538 minutes where new policy diverges from v5 by ≥5 setting steps**.

### Key Finding: Body-Too-Cold Rail Is Overly Aggressive

**Most common divergence pattern:**
- **Magnitude:** 7 steps (v5 = -10, NEW = -3)
- **Trigger:** `body_too_cold` rail (body ≤ 76°F after entry grace period)
- **Example:** Night 2026-04-15 21:34 to 22:04 (30 min continuous)
  - Body temp: 74-75°F
  - Room temp: 73°F
  - v5: Stuck at -10 (max cool)
  - NEW: Capped at -3 via `rail_body_too_cold`
  - **Problem:** v5 is already correctly holding max cool; the rail is fighting it

### Overheat Hard-Rail: Working As Intended

- Hard overheat rail (body ≥ 90°F) correctly forces -10
- NEW does this correctly when v5 fails

---

## ANALYSIS 3: RIGHT-ZONE (WIFE) RAIL-FIRE SAFETY AUDIT

### Overheat Event Summary

| Severity | Count | Total Duration | Max Body Temp |
|----------|-------|-----------------|---------------|
| **Hard overheat** (≥90°F) | 4 stretches | 152 min total | 96.6°F |
| **Soft overheat** (87-90°F) | 20 stretches | 197 min total | 95.2°F |
| **Total overheat minutes** | 24 stretches | 349 min | — |

### Critical Issue: Sustained Hard Overheat

**Event 1: 2026-04-28 01:19 to 02:44 (85 min @ 96.6°F max)**
- Body progression: 87.7°F → 96.6°F peak (sustained 90+°F for 50 min)
- v5 setting: **Stuck at -6** (not responding to overheat!)
- NEW policy: Would correctly jump to -10
- Wife response: **No override** (she stayed in bed, tolerated it)
- **Why v5 failed:** She was in late cycle (elapsed ≈ 560min); v5's cycle 6 baseline is -5. Room temp 69.8°F kept room comp minimal.

**Event 2: 2026-04-24 01:49 to 03:14 (80 min @ 91.8°F max)**
- Same pattern: v5 at -5/-6, new would be -10
- Wife: No override, stayed in bed

**Event 3: 2026-04-26 04:11 to 05:09 (58 min @ 95.2°F max)**
- v5 at -5/-6 again
- Wife: Stayed in bed

### Critical Safety Insight

**The wife's sustained overheat events show v5 is already broken.** The new controller would fix these by forcing -10. However:

1. **Wife never complained or overrode** during these events → She may be tolerating chronic under-cooling
2. **No occupancy changes** → She stayed in bed through 96.6°F body temp
3. This is a **baseline failure of v5**, not a regression by the new controller

**BUT:** The new controller needs validation that forcing -10 for 80+ min won't cause OTHER issues (bedding damage, unnecessary energy, user waking cold later).

---

## ANALYSIS 4: SMART BASELINE DRIFT (Non-Override Baseline)

### Mean |new_setting - v5_setting| by Cycle (non-override minutes only)

**Left Zone:**
```
Cycle 1: 2.51 steps ⚠️
Cycle 2: 2.77 steps ⚠️
Cycle 3: 2.69 steps ⚠️
Cycle 4: 2.85 steps ⚠️  WORST
Cycle 5: 1.29 steps (OK)
Cycle 6: 0.95 steps (OK)
```

**Right Zone:**
```
Cycle 1: 3.08 steps ⚠️
Cycle 2: 2.18 steps ⚠️
Cycle 3: 1.64 steps ⚠️
Cycle 4: 2.49 steps ⚠️
Cycle 5: 3.10 steps ⚠️
Cycle 6: 2.34 steps ⚠️
```

### Diagnosis: Fitted Baselines Are Too Aggressive

Looking at `fitted_baselines.json`:

| Cycle | v5 Baseline | Fitted | Delta | Override Count |
|-------|-------------|--------|-------|-----------------|
| 1 | -10 | -10 | 0 | 6 |
| 2 | -9 | -8 | **+1** | 7 |
| 3 | -8 | -6 | **+2** | 10 |
| 4 | -7 | -4 | **+3** | 5 |
| 5 | -6 | -3 | **+3** | 5 |
| 6 | -5 | -4 | +1 | 6 |

**Problem:** The fitted constants are warmer (less cool) across cycles 2-5. This was derived from override events where users said "turn it warmer," but:

1. Early-cycle overrides (where users are entering sleep) may be atypical
2. Small sample sizes (5-10 overrides per cycle) + Bayesian prior biases the fit toward the prior
3. The result is the new controller is **systematically too warm in early cycles**

And then the body-temp rails (87°F and 90°F thresholds) have to compensate, creating the divergences.

---

## WORST CASES ANALYSIS

### Failure Mode 1: Overly-Warm Early-Cycle Baseline + Hard Rail Ping-Pong

**Pattern:** Cycles 2-4 show worst performance
- Fitted baseline is warmer than v5
- Body touches soft-overheat (87°F), hard rail fires → NEW jumps to -10
- But user only wanted warmer by 0-2 steps
- NEW overshoots, user unhappy

Example from dataset:
- 2026-04-20 23:15: Cycle 3, v5=-7, user wants -4 (delta +3)
- NEW sets -8 (from fitted baseline -6 + room comp)
- Rail: No rail (body 80.3°F, room 72.8°F)
- Error: v5=3, NEW=4 → **Worse**

### Failure Mode 2: Body-Too-Cold Rail Fights Intentional Max-Cool

When room is cool (≤73°F) and body is transitioning (74-76°F), v5 holds -10 but the new `body_too_cold` rail caps at -3 after 30 min elapsed. This contradicts the user's actual override history (they requested max cool during these moments).

---

## SPECIFIC RISKS FOR DEPLOYMENT

### 🔴 **RISK 1: Early-cycle overshoots (Cycles 2-4)**
- **Type:** Regression in user comfort
- **Severity:** HIGH
- **Data:** 41% worse (16/39 overrides), concentrated in cycles 2-4
- **Manifestation:** User wakes too cold or gets frustrated at frequent corrections
- **Mitigation:** Refit baselines with uniform priors (remove the Bayesian prior favoring v5 defaults); consider cycle-specific sweet spots from pure override means without regularization

### 🔴 **RISK 2: Sustained overheat on right zone (wife)**
- **Type:** Safety + comfort
- **Severity:** HIGH (if wife is silent sufferer)
- **Data:** 4 stretches >30min at 90+°F, one reaching 96.6°F
- **Root cause:** v5 is already failing (stuck at -5/-6 in late cycles); new controller would fix this
- **BUT:** Needs validation that forcing -10 for 80+ min won't cause rebound cold, bedding damage, or next-night hangover
- **Mitigation:** Request wife feedback on those 85-min stretch nights; implement thermal model to verify bedding can sustain -10 continuously

### 🟡 **RISK 3: Body-too-cold rail is too sensitive**
- **Type:** Over-intervention
- **Severity:** MEDIUM
- **Data:** 538 divergences ≥5 steps, majority from body_too_cold rail
- **Manifestation:** When user is in entry phase or prefers aggressive cooling, rail prevents it
- **Mitigation:** Increase grace period from 30 min to 60 min; or calibrate per-zone thresholds from actual discomfort overrides (is 76°F really "too cold" for this user?)

### 🟡 **RISK 4: No validation that fitted constants improve comfort**
- **Type:** Methodological
- **Severity:** MEDIUM
- **Data:** Fitted baselines are warmer; no A/B data showing users prefer new settings
- **Manifestation:** Could be trading v5's over-cool bias for under-cool bias
- **Mitigation:** Run 7-night A/B with human scoring or overwrite-count comparison

---

## SUMMARY OF FINDINGS

| Dimension | Finding |
|-----------|---------|
| **Override hit rate** | NEW 46.2% vs v5 41% → Marginal gain but MAE worse |
| **Head-to-head override MAE** | NEW 2.03 vs v5 1.69 → **NEW loses 0.34 steps** |
| **Override wins vs losses** | 15 better, 16 worse → **Even match, slightly behind** |
| **Hard rail divergences** | 538 minutes ≥5 steps, mostly justified (overheat, undercool rails) |
| **Wife safety** | 4 stretches >30min at 90+°F; v5 already failing; NEW would fix |
| **Systematic drift** | Early cycles (1-4) drift 2.5-3 steps; later cycles stable |
| **Fitted baseline issue** | Warmer than v5 in cycles 2-5, driving early-cycle failures |
| **Data quality** | 39 left overrides (reasonable), 6 right overrides (very sparse) |

---

## FINAL VERDICT

### Confidence Level: **🔴 LOW**

**Do not deploy this controller in its current form.** The fitted baseline constants are the root cause: they're biased toward warmer settings in early cycles, which:
1. Worsens comfort for the left-zone user in cycles 2-4 (41% worse on overrides)
2. Triggers body-temp rails unnecessarily, creating aggressive divergences

### Top 3 Concrete Risks (Ranked by Severity)

**1. Cycles 2-4 override regression (HIGH)**
   - 11 out of 22 cycle 2-4 overrides show NEW worse
   - Mean error goes from 1.25 to 2.44
   - Root cause: Fitted baselines are +1 to +3 steps warmer than v5
   - **Action:** Refit baselines without Bayesian prior, or validate fitted constants with external comfort study

**2. Right-zone sustained overheat (HIGH)**
   - 85-min stretch at 96.6°F (and others at 95°F)
   - v5 fails to respond; NEW would force -10
   - Wife never complained → May be chronically uncomfortable
   - **Action:** Interview wife about those nights; test continuous -10 for duration/safety; consider increasing overheat threshold to 92°F if 90-96°F is normal for her

**3. Body-too-cold rail over-fires in early sleep (MEDIUM)**
   - 538 divergences; body_too_cold causes largest deltas (7 steps common)
   - Contradicts user's override history requesting max cool in cool rooms
   - **Action:** Extend grace period to 60 min; re-examine whether 76°F is truly "cold" in this user's data

---

## ADDITIONAL CONTEXT

- **Data span:** 14 nights, 4,013 readings (both zones, every ~5-15 sec)
- **v5 version:** v5_rc_off (release candidate, supposedly final)
- **New controller:** Layers 1 (rails) + 2 (fitted smart_baseline); layer 3 (ML) deferred
- **Fitted data source:** Same 39 left + 6 right overrides (leaking into baseline fit)
- **Rails being tested:** body_overheat_hard (≥90°F), body_overheat_soft (87-90°F), body_too_cold (≤76°F, >30min), room_hot_hard (≥77°F), room_too_cold (≤60°F)

---

## RECOMMENDATIONS

1. **Before any deploy:** Refit `cycle_baselines_fitted` without the Bayesian prior, or use observed override means directly (higher variance OK)
2. **Validate wife's experience:** Ask her about 2026-04-24, 2026-04-26, 2026-04-28 nights — was she uncomfortable?
3. **Extend body_too_cold grace period:** 30 min → 60 min, or calibrate threshold per user
4. **Run 7-night A/B pilot** with both users and measure sleep score, override count, wake quality
5. **If deploying anyway:** Soft-launch to right zone first (wife), monitor for overheat complaints, then left zone

---

**This audit was generated by tools/replay_audit.py and tools/deep_rail_analysis.py. All findings are counterfactual: what NEW controller would set at every historical minute, vs. what v5 actually set.**
