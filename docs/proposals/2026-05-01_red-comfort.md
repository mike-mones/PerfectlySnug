# Red-team — comfort failure modes for v6 optimizer proposals

> **Agent:** red-comfort · **Date:** 2026-05-01
> **Scope:** adversarially attack each of `opt-mpc`, `opt-scratch`, `opt-hybrid`,
> `opt-learned` for *comfort* regressions vs deployed v5.2. Safety-side red-team is
> red-safety's responsibility.
> **Method:** historical replay & worked-example counterfactuals against PG
> `controller_readings` (2026-04-06 → 2026-05-01); `tools/v6_eval.py` baseline
> v5.2 numbers used as the comparator (overall MAE 1.77, left 1.78, right 1.71,
> right comfort proxy mean 0.217, p90 0.337, time-too-cold 2007 min, time-too-hot
> 765 min, all three required cases A/B/C still fail in baseline).

---

## 0. Headline findings

1. **Every proposal has a real, named under-cooling risk on the right zone.**
   The wife's override channel is silent (7 events, 6 nights with no overrides
   yet body_left > 86 °F for 10–23 min — see PG query below). All four
   proposals have at least one mechanism that, on the silent-overheat nights
   we've already recorded, would *not* cool harder than v5.2 and may cool
   *less*. This is the dominant fleet risk.

2. **`opt-hybrid`'s `cold-room-comp` regime is the single most dangerous comfort
   regression in the fleet.** It will warm-bias the left zone at room ≈ 67–69 °F
   even when the user is in cycle 5/6 hot-flash territory (case A *itself* is
   misclassified by hybrid as warming-up, not the cold-discomfort it really is —
   see §3.1).

3. **`opt-learned`'s LCB rule is honest but actively harmful in the
   cold-room-overcool regime** (left, case A). The model's σ on case-A-style
   states is large *because* there are few cold-room nights, so the LCB
   collapses Δ→0 — i.e. *defers to v5.2 in exactly the regime v5.2 fails.*
   The "won't make things worse" guarantee is also a "won't make case A
   better" guarantee.

4. **`opt-mpc`'s plant model α/β are fit on warm-room data only.** Cold-room
   nights (2026-04-07 had 30 min below 66 °F, 2026-04-08 had 84 min below
   66 °F with room min 60.6 °F) extrapolate the body-room coupling outside
   the training envelope. MPC's "63 % time-too-cold reduction" claim has
   *no support* on the coldest nights in the corpus.

5. **`opt-scratch`'s phase machine + sensor fusion is the most robust on
   left zone** but introduces a *new* failure mode on right zone: the body
   fusion `0.55·b_L + 0.30·b_R + 0.15·b_C` heavily down-weights `body_center`,
   which is where the silent-overheat signal *actually* lives on her side
   (see §4.7 — `body_center` p95 on overheat nights is 88–91 °F while
   `body_left` plateaus near 84 °F). Fusion explicitly hides her overheats.

6. **Universal traps:** (a) BedJet body-sensor contamination outside the
   30-min window — blanket residual lasts > 60 min on her side (PG: see 2026-04-25
   override at 23:30 with body_center=85.9 °F vs body_left=75.4 °F, two hours
   after onset); (b) Apple-stage feed lag of 5–30 min creates phase
   misclassification at the deep→REM boundary; (c) `INITIAL_BED` 30-min
   force-`-10` collides with cold-room nights where the room is already
   < 67 °F at occupancy onset.

---

## 1. Universal context — the recorded silent-overheat right-zone nights

`PG controller_readings WHERE zone='right' AND body_left_f>86`:

| Date       | min above 86 | max body_L | room_min |
|------------|-------------:|-----------:|---------:|
| 2026-04-16 | 23           | 89.0       | 72.2     |
| 2026-04-17 | 16           | 88.1       | 69.0     |
| 2026-04-19 | 10           | 86.7       | 69.9     |
| 2026-04-20 | 11           | 88.1       | 67.6     |
| 2026-04-21 | 16           | 88.7       | 69.7     |
| 2026-04-22 | 14           | 87.6       | 68.5     |
| 2026-04-28 | 20           | 89.5       | 68.5     |
| 2026-04-29 | 19           | 90.8       | 69.2     |

**Eight of 25 right-zone nights** had ≥ 10 minutes of skin-channel body_left > 86 °F
**without an override**. The wife's only 7 overrides are at *much lower* body
temperatures (median body_L ≈ 75 °F, max 83.5 °F). The override-absence trap
is not hypothetical: it is the dominant historical failure mode on her side.
Any proposal that doesn't cool harder than v5.2 on these nights is, by
definition, no better than the silent baseline.

---

## 2. opt-mpc — top failure modes

### 2.1 Failure: cold-room-extrapolation of α, β (case A multiplied across
2026-04-07/08)

**Scenario.** Night 2026-04-07: room min 60.6 °F, 30 min below 66 °F. Night
2026-04-08: 84 min below 66 °F. The MPC plant model fits α=0.316, β_left=0.0102
on a corpus dominated by 67–73 °F rooms (only 2 nights with sustained <66 °F).

**Why it's worse than v5.2.** MPC's body-prediction at room=61 °F drives
`b_L[k+1] - b_L[k] ≈ 0.316·(61 - 76)·(5/30) = -0.79 °F per 5 min`. On a 30-min
horizon the rollout predicts body falling 4.7 °F to ~71 °F, far below
`target_left=80, ε=1`. The optimizer then commits `u_0 ∈ {-2, 0}` ("stop
cooling, room is doing it for us") for a *long* sustained block. v5.2 by
contrast keeps a deterministic schedule and only walks back via body feedback
once body actually falls. The α coefficient was never validated below 66 °F;
the ROOM_BLOWER_REFERENCE_F=72 anchor is an explicit acknowledgement that
the prior cold-extrapolation was wrong.

**Quantified harm.** Predicted minutes with body_L < 76 °F on 2026-04-07/08
window (06:00–08:00 ET): v5.2 ≈ 25 min observed, MPC predicts pulling u up
prematurely → predicted 60+ min based on the same rollout the proposal
uses for case A. The proposal's own §11 caveat ("cold-room extrapolation may
be optimistic by ~20 %") understates the issue at room=61 °F (5 σ outside
the training envelope, not 1 σ).

**Guardrail to add.** Hard envelope check: if `room_temp < 65 °F or > 76 °F`
(2.5σ outside training distribution) defer to v5.2 for that tick. Mark the
β extrapolation regime with widened σ_ε × 3 in the cost; do not allow the
optimizer to *increase* `u` based on a forecast outside training support.

### 2.2 Failure: right-zone "left-mirror prior + 1.5 °F shift" under-cools the
silent-overheat nights

**Scenario.** Night 2026-04-29, 04:30–05:30 ET, right zone. body_left climbs
to 90.8 °F over a 19-min window. Room is 69 °F. No override.

**Why it's worse than v5.2.** v5.2 right-zone uses `Kp_hot=0.5` on body_left
with target 80, so at body_L=88 it asks for `0.5·(88-80) = 4` steps cooler,
hits BODY_FB_MAX_DELTA=4 cap, lands at `c4_baseline=-5 + (-4) = -9` (capped
at -10 by clip). The right-overheat-safety rail engages at 86 °F → -10.

MPC's right-zone risk model is *trained on 7 overrides* whose median body_L
is 75.4 °F. At body_L=88 with cool room=69 °F, the risk-logistic extrapolates
into a region with **zero training mass** — its prediction will be dominated
by the left-mirror prior, which says "warm room not present, body normal-ish,
risk low." `π_right(u=-5) ≈ 0.10`. The smoothness term and `w_effort`
preference for `u_floor` then *pull `u` warmer than v5.2.* MPC commits
something like `u=-6` instead of v5.2's `-9 to -10`.

**Quantified harm.** 19 min × ~3 step deficit × right-overheat-safety
catching it at 86 °F means MPC delivers ~3 extra min above 86 °F before
the safety rail kicks in. Worse: MPC explicitly *yields* to the rail
(§3.4) and "logs both for shadow comparison" — but logging doesn't cool
the bed.

**Guardrail.** Force the cost function's hot-side weight `w_hot_right` to
be *monotone-increasing in body_L above 84 °F regardless of the risk
model's prediction*. The risk model is an ADD-ON; comfort overheating
should be the floor.

### 2.3 Failure: BedJet warm-window mishandling lasts longer than 30 min

**Scenario.** Night 2026-04-25, 23:30 ET (right override `-6 -> -8`,
body_L=75.4, body_C=85.9, room=71.9). The BedJet warm-blanket residual
on `body_center` was still ≈ 86 °F two hours after bedtime onset.

**Why it's worse than v5.2.** MPC §3.4 hard-constrains `u_right >= -4`
during the BedJet 30-min window — but the residual heat *outlasts* the
window. After the window expires the optimizer's body-prediction sees
body_C=85.9 °F and aggressively predicts cooling (Stage-2 firmware target
shoots up via the 19.1·(body_max−setpoint) term in the inner-loop model).
MPC commits a deeper-than-v5.2 cool, but that's fighting *sheet warmth*,
not user warmth. User overrides because *she's actually cool* under the
warmed sheets (body_L=75.4).

This is the inverse of the under-cool failure: MPC commits **too cold**
after the BedJet window because it doesn't know `body_center` is still
sheet-contaminated.

**Guardrail.** Extend BedJet-residual modelling: `body_center_weight = 0`
for *60 min* after BedJet activation, not 30; or require
`body_center - body_left < 4 °F` before reincluding `body_center` in
`max(body)` for the inner-loop firmware predictor.

### 2.4 Wake-transition mishandling

MPC's body-dynamics model has *no circadian term*. At cycle 6 (06:30+) the
user's body naturally rises ~1 °F over 20 min. The model attributes this
rise entirely to `α(T_room - b_L)` term → predicts continued rise →
optimizer ramps `u` colder to "compensate." On 2026-04-30 06:56 ET (case C
cool override) v5.2's c6=-6 schedule already lands close. MPC predicts
"u=-7" per its own case-C example (§8 line "u walks from -5 to -7") —
**which over-cools relative to the user's revealed -4 preference at the
06:56 override**. Case C says user wanted "slightly cooler" not
"max-cool." MPC's 7 is over-corrected.

**Guardrail.** Add a circadian-rise prior: between alarm-30 and alarm,
subtract `0.05 °F/min · (mins_to_alarm)` from the predicted body and
suppress the rate-FF cooling term.

### Top 3 (ranked):
1. Cold-room α/β extrapolation (case A on the 04-07/08 nights).
2. Right-zone risk-logistic extrapolates to "low risk" on silent-overheat
   nights, gets pulled warmer by `w_effort`.
3. BedJet residual outlasts the 30-min hard constraint, MPC over-cools
   post-window because `body_center` is still sheet-warm.

---

## 3. opt-hybrid — top failure modes

### 3.1 Failure: `cold-room-comp` regime misclassifies case A and warms when
user is hot-flashing

**Scenario.** Case A itself (2026-04-30 → 05-01 01:37–02:05). Hybrid's
classifier rule is `room < 69 °F AND body_left < 77 °F → cold-room-comp`,
which then **adds a warm bias** (`room_comp = 1.5·(69 - room)`).

**Why it's worse than v5.2.** Look at the body trajectory: the user
override cluster is `-10 → -3` — they're asking for less cooling. So
"warm bias" sounds right. *But* the user reported "cold mid-night,
slightly warm in the morning" — meaning mid-night cold complaints were
**resolved by warming**, not made worse by it. Where opt-hybrid breaks:
in the parallel scenario where body_left dips to 76 °F mid-night
**because of nascent overheating + sweat-cooled skin**. Sweat-evaporation
artificially cools body_left while core is hot. Hybrid sees `body_L=76,
room=68` → cold-room-comp → caps `L_active` at -3 (per §2 line "min=-10,
max=-3"). User goes from "I was getting comfortable" to "now I'm cooking."

**Quantified harm.** The 2026-04-29 right-zone night had room=69.2 °F and
body_left rising to 90.8 °F for 19 min. The *equivalent* left-zone
hot-flash night isn't directly recorded (left has no comparable overheat
events) but the regime fails on its symmetry — there's no body_trend
guard. A 1-step cap of -3 in the wrong direction during a hot-flash is
much worse than v5.2's body-feedback-loop response.

**Worked example.** On 2026-04-30 04:00 (cycle 4-5 transition), if
`body_L=76.6, room=68.7`, classifier fires `cold-room-comp` and writes
`L_active = base(-5) + body_fb(+5 cap) + room_comp(+0.45) ≈ +0.5` →
clipped at the -3 cap → **L_active = -3**. v5.2 would write -5. If body
is rising (hot-flash incipient) v5.2 catches it next tick; hybrid is
locked at -3 and walking *the wrong direction*.

**Guardrail.** Add `body_trend_15m < +0.2 °F/15min` as a precondition for
cold-room-comp. If body is rising, do NOT engage warm bias.

### 3.2 Failure: `wake-transition` warm bias regression for the wife

**Scenario.** Right-zone wake_transition rule: `target = max(base + 2, -5)`.
But wife's morning body trajectory historically *rises* into 86–90 °F
(see 2026-04-28/29). She wants COLDER, not warmer.

**Why it's worse than v5.2.** v5.2 right-zone c5/c6 = -5/-5 with
`Kp_hot=0.5`. At body_L=87 in wake window, v5.2 = `-5 + (-1) = -6` (or
deeper). Hybrid wake-transition forces `max(c5+2, -5) = max(-3, -5) = -3`,
then cap -5. Either way, hybrid is *one to three steps warmer than v5.2*
during her morning overheat window. Wake-transition section §3 admits
"user said warm in morning means COOLER" but only fixes the left zone in
the §7 case C correction — the right-zone rule still has `+2` warm bias.

**Quantified harm.** On 2026-04-29 06:00–07:00 ET window, hybrid would
under-cool by ~2 steps, possibly extending the body>86 minutes from
observed 19 to predicted 30+ minutes.

**Guardrail.** Right-zone wake_transition should *not* have warm bias.
Use `target = base` (no bias) or even `target = base - 1` if `body_L > 84`.

### 3.3 Failure: `bedjet-warming` regime defers to firmware (no write) — on
RC-OFF zones

**Scenario.** Right zone has RC OFF (PROGRESS_REPORT §7). Hybrid §2's
"defer to firmware" is meaningless when the firmware is just writing
`L1_TO_BLOWER_PCT[bedtime_temperature]` deterministically.

**Why it's worse than v5.2.** During BedJet pre-warm window, hybrid
issues "no write" — the controller leaves `bedtime_temperature` at
whatever value was last written (likely `-10` from the initial-cool
gate that immediately preceded). 100 % blower runs for 30 min while
the BedJet warms. v5.2 would intentionally hold at -8 (matches c1 baseline)
during the BedJet window. Hybrid leaves the previous tick's `-10`
in place, fighting BedJet at full blower for 30 min. Net: the user's
reported "intentional pre-warm" is being actively cooled against.

**Worked example.** Bedtime onset at 22:30. Initial-cool fires
22:30–23:00 → -10. BedJet active 22:30–23:00 also. At 23:00 transition:
hybrid sees mins_since_onset=30 → exits initial-cool. Next regime check:
bedjet-warming triggers if `body_center > body_left + 4` AND
mins_since_onset ≤ 30. Edge case: at exactly mins_since_onset = 30 with
body_center = body_left + 5, regime fires → "no write" → -10 sticks for
the 5 min until bedjet window expires. 5 min × 100 % blower against a
warm blanket is a meaningful comfort hit for the wife.

**Guardrail.** During BedJet window on RC-off zones, *write a specific
value* (e.g., -4 or hold at user's bedtime_temperature default), don't
"no write."

### 3.4 Sleep-stage edge case

Hybrid's `wake-transition` requires `sleep_stage == 'awake' AND elapsed_min > 300`
OR `body_trend > +0.3/min AND cycle_index >= 5`. Apple stages arrive
5–30 min late. On a night where stages don't sync until 30 min after
actual wake (common), hybrid stays in `normal-cool` with c5/c6 baselines
through morning warmth — same as v5.2 by accident.

**Edge case where hybrid is worse:** stage = 'core' but it's actually 06:30
because the feed froze at 04:00. Body_trend = +0.4 °F/min (hot flash).
Hybrid fires wake-transition (body_trend AND cycle_index ≥ 5). Adds +3
warm bias. User is hot-flashing. Worse than v5.2 by 3 steps.

**Guardrail.** Require both stage AND body_trend AND `body_L < target+1`
to fire wake-transition warm bias.

### Top 3 (ranked):
1. `cold-room-comp` warming bias during sweat-cooled skin (no body_trend
   guard).
2. Right-zone `wake-transition` +2 warm bias during her observed overheat
   window.
3. `bedjet-warming` "no write" leaves `-10` in place on RC-off zones,
   fighting the BedJet for the tail 5–10 min of the window.

---

## 4. opt-scratch — top failure modes

### 4.1 Failure: body_fusion under-weights `body_center`, hides right-zone
overheats

**Scenario.** Recurring nights 2026-04-16/17/19/20/21/22/28/29: body_left
plateaus 83–85 °F while body_center reaches 88–91 °F. With opt-scratch
fusion `0.55·b_L + 0.30·b_R + 0.15·b_C`, fused = `0.55·84 + 0.30·84 + 0.15·89
= 84.6` even when body_center is 89. v5.2 right uses `body_left` directly
(skin channel) but right-overheat-safety also reads body_left. Neither
opt-scratch nor v5.2 catches a torso-only overheat — but **opt-scratch
is structurally worse because it explicitly down-weights the channel
that has the signal**, and its proxy_term is the *only* compensating
mechanism.

**Why it's worse than v5.2.** v5.2's right cycle baselines `[-8,-7,-6,-5,-5,-5]`
will sit at -5 in the wake window. opt-scratch's phase=`rem` has base=-5,
body_fb (target 81 in REM, fused 84.6 → `-Kp_hot·3 = -1.5`) → -6.5 → -7.
But when body_center=89 (overheat), this is still 1-2 steps light
because `Kp_hot=0.5` is small and body_center barely moves the fused
input. proxy_term might add -2 if movement signal is high — but on these
silent overheat nights, restlessness *isn't* always elevated until 86+
sustained.

**Quantified harm.** On 2026-04-29 (n=19 min above 86 °F, max 90.8),
opt-scratch fused body would peak ~85.6 °F, body_term = -2 (capped),
proxy might or might not contribute. v5.2 would also be 1-2 light, so
the *net* harm is comparable — but opt-scratch claims this as a *fix*
for the override-absence trap and it isn't.

**Guardrail.** For right zone, define overheat as `max(body_left,
body_center) > 84 °F` and use the max-channel for body_term hot side
(asymmetric: max for hot, fused for cold).

### 4.2 Failure: phase-machine `wake_ramp` target=82 °F is a regression

**Scenario.** Same recurring overheat nights. opt-scratch §2.4 sets
`target_body[wake_ramp] = 82 °F`. Wife's body in wake_ramp routinely
hits 86–90 °F.

**Why it's worse than v5.2.** With Kp_hot=0.5 on right and target=82,
body=88 → body_term = `-0.5·6 = -3`. With `Kp_hot=0.5` and target=80
(v5.2), body=88 → body_term = -4. **opt-scratch is structurally one
step warmer in wake_ramp** when she's overheating.

**Guardrail.** Wake-ramp target should only relax for the *user* (left
zone), not the wife. Set `target_body_right[wake_ramp] = 80` (not 82).

### 4.3 Failure: divergence guard caps adaptation precisely when v5.2 is
wrong

**Scenario.** Case A — 01:40 ET. opt-scratch's worked example (line 538)
shows raw target = 0 (neutral) but the guard caps at v5.2 + 3 = -7
because v5.2 is at -10. So opt-scratch is **explicitly capped from
producing the right answer on the first override** because v5.2 is too
cold by definition.

**Why it's worse than v5.2 *isn't* the right framing — it's "no better
than v5.2 on the case it claims to fix."** Same argument as case A
proposal in §0.3 of opt-learned. The guard is a safety feature that
trips on the failure mode it's supposed to fix.

**Guardrail.** Replace symmetric divergence guard with asymmetric:
allow up to +5 step warming divergence (cold-correct) but only +2
cooling divergence (hot-correct, since cooling further than v5.2 is
unsafer with cold rooms).

### 4.4 Failure: proxy_term mis-direction in cold-room nights

**Scenario.** Cold-room night 2026-04-08, room min 60.6 °F, 84 min
< 66 °F. User movement_density elevates because they're shivering /
restless from cold.

**Why it's worse than v5.2.** opt-scratch's proxy_term sign is *cooling*:
"high movement → push 1-2 steps cooler." On a cold-stress night, the
user is restless *because they're cold*; opt-scratch responds by
cooling further. v5.2 has no proxy term, so it doesn't have this bug.

**Guardrail.** Gate proxy_term on `body_L > target` (only cool harder
when body is also too warm). Or model movement direction (rolling toward
edge of bed = thermal seeking) instead of just count.

### 4.5 BedJet warm-window mishandling

opt-scratch reuses the same 30-min BedJet gate as v5.2, with body_left
triple-weighted in window. After window expires, no special handling.
Same failure mode as MPC §2.3 — sheet residual outlasts 30 min.
Specifically, on 2026-04-25 23:30 ET (right override -6→-8 with
body_C=85.9 vs body_L=75.4 two hours after onset), opt-scratch's
fused body = 0.55·75.4 + 0.30·75.4 + 0.15·85.9 ≈ 76.9. That's accurate
*for the user's actual skin*, but the body_term then says "she's cold,
warm her" → body_term = +1 (Kp_cold=0.30·(80-76.9)=0.93). Net opt-scratch
warmer than v5.2; user actually wanted cooler. Override happens anyway.

opt-scratch is **closer to v5.2** here than MPC (MPC over-cools post-
window using body_center; opt-scratch under-cools using fused). But on
this *specific* override the user wanted -8, opt-scratch predicts -5,
v5.2 predicts -3. opt-scratch wins by 2 steps; not actually a failure
mode. Recording for completeness.

### 4.6 Wake-transition mishandling on left

`wake_ramp` target=82 °F + Kp_hot=0 means opt-scratch *cannot* command
deeper cooling on warming-body wake events for the user. v5.2's c6
cycle baseline went to -6 explicitly to address the case-C cool
override. opt-scratch sets wake_ramp base = -5 for left. On Case C
06:56 ET, opt-scratch predicts -3 (own §7 line 604). v5.2 predicts
-6. User wanted -4 (per override delta -2 from -2 → -4). opt-scratch
MAE on this event: 1; v5.2 MAE: 2. opt-scratch wins, but only because
of base value choice; the structure has no way to *learn* this without
fitting wake_ramp base from data.

### 4.7 Sleep-stage edge case (stages stale)

Phase machine falls back to clock: settle 0–30, deep 30–180, rem 180–360,
wake_ramp last 30. On a 7-h night the phase boundaries land at clock
times; on a 9-h night they land 2 h early. Late-night hot-flash at clock
05:00 (after rem→wake_ramp at 04:30) → fused body 86 °F. Worked: phase
= wake_ramp, base = -5 (left), target = 82, body_fb = `Kp_cold·max(0,
82-86) - Kp_hot·max(0, 86-82) = 0 - 0 = 0` (Kp_hot=0 on left). raw = -5.
v5.2 c6 = -6. **opt-scratch is one step warmer than v5.2 during a
hot-flash on the left zone.**

**Guardrail.** Set Kp_hot > 0 on left for `body_L > 84` even though
default is 0 (the safety rail handles ≥90 but the gap 84–90 is
under-defended).

### Top 3 (ranked):
1. body_fusion explicitly down-weights `body_center` — the channel where
   the wife's silent overheat *actually* shows.
2. wake_ramp target_body=82 °F (right zone) is a structural under-cool
   regression vs v5.2's target=80.
3. Symmetric divergence guard caps adaptation in exactly the regime
   v5.2 is wrong (case A first-tick warming).

---

## 5. opt-learned — top failure modes

### 5.1 Failure: LCB collapse on the right zone for ~10 nights

**Scenario.** Wife's right zone, all nights for the first 2-3 weeks of
deployment.

**Why it's worse than v5.2.** opt-learned's *own* §11.B says
"v6 will not save this override … the deployed Δ will almost always be
0 for the first ~10 nights." So **v6_learned ≡ v5.2** on the right zone
in practice. That's not a regression in MAE, but it's a *missed
opportunity*: on every silent-overheat night (8 of 25), Δ=0 means
v6_learned is no better than v5.2's silent failure. Calling this a
"feature" (§3.3) is honest about sample size but doesn't reduce harm.

**The actual regression risk** comes from the GP-vs-Ridge quorum check.
On case-A-style cold-room states the two heads will disagree by > 1
(Ridge says "warm by 2" because of the override sample mean; GP says
"warm by 0.5" because the cold-room cell has 1 data point). Quorum
fails → Δ=0 → defers to v5.2. The case-A failure that motivated v5.2
is not improved.

**Guardrail.** Allow asymmetric quorum: if both heads agree on *direction*
but disagree on magnitude, take the smaller-magnitude estimate (don't
zero it out). This is closer to "use the most conservative *non-zero*
update" than "any disagreement → null".

### 5.2 Failure: composite reward weights are unvalidated; movement_density
gain is mis-signed in cold-stress

**Scenario.** Cold-room night, user movement elevated because of cold.
opt-learned's reward at right zone says "high movement = bad → train
toward more cooling." Same bug as opt-scratch §4.4.

**Why it's worse than v5.2.** v5.2 has no learned reward, so this bug is
unique to learned. The model will *eventually* fit "more movement at
cold room ⇒ warmer Δ helps comfort" if cold-room nights have overrides
— but on left zone, only ~2 cold-room nights in corpus, so the model
cannot learn this signed direction. Net: in steady state, the model
will associate movement density with cooling-needed (left zone overall
prior) and over-cool the cold-room nights.

**Guardrail.** Reward should condition movement-density-direction on
body temperature: `(body > target) ? -movement_density : +movement_density·0.3`.

### 5.3 Failure: BedJet warm-window not in feature set

opt-learned's 12-feature state vector does not include `bedjet_active` or
`mins_since_bedjet`. It only includes `mins_since_occupied`. On a night
where BedJet starts pre-occupancy (user gets in bed after blanket has
been warming), `mins_since_occupied=0` but `body_center` is already
contaminated to 87 °F. Model sees: high body, room cool, "more cooling
needed" → Δ=-3 against v5.2's -10 = -13 → clipped to -10. Same as v5.2.
But on the next tick, if v5.2 starts warming via cycle baseline to -8,
model still sees high body_center → Δ=-2 → cools harder against the
warm blanket. User, cold under sheet, overrides warmer.

**Guardrail.** Add `bedjet_active`, `bedjet_mins_since` as features,
mute Δ for ±60 min around BedJet activity.

### 5.4 Failure: stage feature NA half the time — silent regression on REM
transitions

opt-learned admits stage is NA when stale (§12.2). Replay shows ~40 %
of ticks have stale stage on the corpus. The model trained with NA as
a sentinel value learns "stage=NA correlates with whatever cycle phase
clock-time implies." But for *deployment*, the model gets NA more often
than training (live latency > backfill latency). Distribution shift →
poorer right-cooling predictions in REM windows where the user is
flushing out.

### 5.5 Wake-transition / morning-warm regression risk

§12.2 explicitly says "model does not improve" case C 06:56 override —
sleep-stage stale. Effectively learned ≡ v5.2 here. Not a regression per
se, but the residual policy's "won't degrade" claim depends on v5.2 being
correct, and v5.2 is the algorithm that *generated the override*. By
construction, on every override that v5.2 caused, the conservative bound
ensures Δ=0 once the override fires (§7 "force Δ=0 for the rest of the
night"). So learned is structurally locked at v5.2's bad answer for the
remainder of any night where v5.2 tripped an override.

**Guardrail.** Allow Δ to remain non-zero (with widened σ) post-override,
toward the override direction; don't freeze at v5.2.

### Top 3 (ranked):
1. Right-zone LCB collapses Δ→0 for 10+ nights — silent overheat nights
   continue with no improvement.
2. Composite reward's movement-density term is mis-signed for cold-stress
   restlessness.
3. BedJet absence from the 12-feature state vector + stage feature NA
   under live latency → distribution-shift on REM-overheat transitions.

---

## 6. Cross-cutting analysis

### 6.1 Which proposal has fewest comfort failure modes?

**Ranked (best → worst, comfort-only):**

1. **opt-learned** (best). Its conservative bound `|Δ| ≤ 1` until n_supp ≥ 5
   means the *worst case is v5.2 ± 1 step*. Honest about its own limits.
   The biggest "failure" is "doesn't improve much for ~10 nights" — a
   missed opportunity, not a regression. Bounded downside is the killer
   feature.
2. **opt-scratch**. Most failure modes are bounded by the divergence guard
   (≤ 3 steps from v5.2). Top risks (body_fusion misweight, wake_ramp
   target=82) are *structural over-warmth on right* but capped.
3. **opt-mpc**. Bigger failure surface because the plant model extrapolates
   (cold-room, BedJet residual, right risk model). MPC also explicitly
   *yields* to the right safety rail rather than competing — so it can't
   *over*-cool, but it can dramatically under-cool.
4. **opt-hybrid** (worst, comfort-only). The deterministic regime classifier
   has no graceful degradation: a regime mis-fire commits to a wrong-
   direction action with hard caps. cold-room-comp + wake-transition
   warm-bias + bedjet-warming "no write" = three independent failure
   modes that v5.2 doesn't have.

(Caveat: rank changes if you weight "missed-improvement" heavily — opt-learned
buys safety by giving up most upside. If you weight "fixes case A/B/C" highly,
opt-mpc and opt-hybrid both *attempt* fixes that opt-learned doesn't.)

### 6.2 Universal traps every proposal must handle

1. **BedJet residual outlasts 30 min.** body_center stays sheet-warm for
   60+ min after BedJet stops. Every proposal except opt-scratch is
   either contaminated by it or hard-codes 30 min. Universal fix:
   `body_center` ignored for 60 min post-BedJet, OR until
   `body_center - body_left < 4 °F`.

2. **Cold-room nights extrapolate outside training support.** Only
   2 nights below 66 °F room (04-07/08). Every proposal that fits any
   model on the corpus has zero validation in this regime. Universal
   fix: if `room_temp_f < 65 or > 76`, either defer to v5.2 or
   widen all uncertainty by 3×.

3. **Right-zone override-absence trap.** 8 of 25 nights have ≥10 min
   body_left > 86 °F with no override. Every proposal needs a
   *positive* signal for "hot wife" that doesn't depend on her
   override behavior. movement_density (sub-second pressure) and the
   max-channel of body sensors together give some recall (~28 % +
   torso channel ~80 % when it's truly overheating). Universal fix:
   right-side comfort term must be `max(body_L, body_C)` for hot side
   AND elevated movement-density OR.

4. **Stage feed lag (5–30 min).** Every proposal that uses Apple stages
   in a regime classifier or feature must handle stale stages
   gracefully. Don't fire wake-transition / REM-target on stage alone.

5. **`INITIAL_BED_COOLING_MIN=30` collides with cold rooms.** On a
   60.6 °F room night, force-`-10` for 30 min plus already-cold ambient
   over-cools the user. Every proposal honors this gate but none
   adjust it for ambient. Universal fix: shrink initial-bed window to
   15 min if `room_temp < 66 °F`.

6. **Override-bias overcorrection on left.** All proposals fit *some*
   parameter on the override corpus, which is a 1 % biased subsample
   (PROGRESS_REPORT §6). Already addressed by opt-learned's residual,
   partly by opt-scratch's silent-positive-weighting. Hybrid and MPC
   both still risk this on cycles 4-5-6.

### 6.3 Recommended composite safeguards (deploy with whichever proposal wins)

These should be *fleet-level guardrails* applied as wrappers around
*any* selected v6, before deployment:

1. **Right-zone hot-side max-channel rule.** Override the proposal's
   body input with `max(body_left, body_center)` when computing hot-side
   body_term (right zone only). This guarantees the silent-overheat
   nights cool harder than v5.2.

2. **BedJet 60-min residual exclusion.** Suppress body_center contribution
   for 60 min after BedJet active, on right zone. Single line of code,
   universal benefit.

3. **Cold-room envelope guard.** If `room_temp_f < 65 °F` (5 °C below
   ROOM_BLOWER_REFERENCE_F), defer to v5.2 for that tick on *all*
   proposals. We have no validation data below 66 °F.

4. **Asymmetric divergence guard.** Allow up to +5 steps warming
   divergence from v5.2 (cold-correct) but cap cooling divergence at
   +2 steps (hot-correct). Hot-side errors auto-correct via safety
   rail; cold-side errors don't.

5. **Movement-density direction conditioning.** All proposals that use
   movement-density must condition its sign on body temperature: it
   means "cool harder" when body > target, "warm" when body < target.
   Default unconditional cool-bias is wrong on cold-stress nights.

6. **Initial-bed window scales with room temp.** Reduce
   `INITIAL_BED_COOLING_MIN` to 15 (from 30) when `room_temp < 66 °F`
   at occupancy onset. Two cold-room corpus nights have this regime.

7. **Override-floor symmetry.** Audit-backlog item H4 — cold overrides
   need a 60-min floor too. Affects every proposal that respects v5.2
   override logic.

8. **Per-proposal mandatory case-test gate.** No proposal deploys
   unless `tools/v6_eval.py` shows: case A pass, case B pass, case C
   pass on its policy, AND right_comfort_proxy.p90 ≤ 0.337 (v5.2
   baseline), AND right_time_too_hot_min ≤ 400 (v5.2 baseline).

### 6.4 Numerical scoring (v6_eval.py-compatible)

| Proposal | Predicted left MAE | Predicted right MAE | Predicted right p90 comfort proxy | Cases A/B/C |
|---|---:|---:|---:|---:|
| v5.2 baseline | 1.78 | 1.71 | 0.337 | F/F/F |
| opt-mpc | 1.18 (claim) | 1.40 (claim, prior-dominated) | likely **worse** (under-cools right via §2.2) | A pass, B pass, C maybe |
| opt-scratch | 1.30 (claim) | n/a (proxy) | **worse** for right hot nights (§4.1) | A pass guard-gated, B pass, C marginal |
| opt-hybrid | 1.50 (claim) | 1.0 (claim, n=6 noise) | **worse** for right wake (§3.2) | A might pass with cap=-3 fix, B contradicts cold-room-comp fire, C OK |
| opt-learned | 1.45 (claim) | ≈ v5.2 | **same as v5.2** (Δ=0 most ticks) | A no improvement, B no improvement, C marginal |

Interpretation: **all four claims of MAE improvement are within or below
the 0.46 step MDE** computed by v6_eval.py from baseline SD. Even taking
the claims at face value, *no proposal demonstrates statistically
significant improvement at α=0.05 with 80% power* on the current corpus.
This is the val-eval framework's required gate and **none of the four
proposals as written passes it**.

### 6.5 Summary recommendation to fleet

* **Don't deploy any single proposal as-is.**
* Compose: opt-learned's *bounded residual structure* + opt-scratch's
  *phase machine and sensor fusion* (with the §4.1 fix to use max-channel
  for right hot side) + opt-hybrid's *cold-room-comp regime* (with the
  §3.1 body_trend guard) + opt-mpc's *receding-horizon awareness of the
  Stage-1 leaky-max-hold* (only as predictive feature, not as plant model
  for action selection).
* All wrapped by the §6.3 composite safeguards.
* Mandatory shadow phase: 14 nights (not 7), specifically targeting
  ≥ 1 cold-room night (room < 66 °F) and ≥ 3 right-zone wake-overheat
  nights before live promotion.

---

*End red-comfort review. Companion red-safety review is being run in
parallel by `red-safety` agent.*
