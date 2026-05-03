"""End-to-end v6 plan computation (policy layer).

Wires together:
  1. regime.classify → regime label + base_setting
  2. Body feedback + room comp → adjusted setting
  3. firmware_plant.predict_setpoint_f → divergence sanity
  4. (optional) residual_head.predict_lcb → bounded learned delta

Used by:
  - tools/v6_eval.py (V6SynthPolicy wraps compute_v6_plan for replay)
  - tests/test_v6_integration.py (golden case assertions)
  - (future) appdaemon/sleep_controller_v6.py

Design ref: 2026-05-01_recommendation.md §6 (pseudocode), §8 (gains table)
"""

from __future__ import annotations

import math
from typing import Any, Optional

from ml.v6 import regime as regime_mod
from ml.v6.regime import RegimeConfig, DEFAULT_CONFIG
from ml.v6.firmware_plant import FirmwarePlant

# Lazy-loaded optional modules
_residual_head_mod = None
_comfort_proxy_mod = None


def _get_residual_head():
    global _residual_head_mod
    if _residual_head_mod is None:
        try:
            from ml.v6 import residual_head as rh
            _residual_head_mod = rh
        except ImportError:
            pass
    return _residual_head_mod


def _get_comfort_proxy():
    global _comfort_proxy_mod
    if _comfort_proxy_mod is None:
        try:
            from ml.v6 import right_comfort_proxy as rcp
            _comfort_proxy_mod = rcp
        except ImportError:
            pass
    return _comfort_proxy_mod


# Default firmware plant (empirical anchors, no cap table file required)
_DEFAULT_PLANT = FirmwarePlant()


def compute_v6_plan(
    zone: str,
    snapshot: dict,
    *,
    config: Optional[RegimeConfig] = None,
    residual_enabled: bool = False,
    plant: Optional[FirmwarePlant] = None,
) -> dict:
    """End-to-end v6 plan computation.

    Args:
        zone: "left" or "right"
        snapshot: dict with all observable state at the current tick
        config: RegimeConfig (defaults to DEFAULT_CONFIG)
        residual_enabled: if True, apply residual_head delta
        plant: FirmwarePlant instance (defaults to empirical-anchor plant)

    Returns:
        dict with keys:
            target: int setting [-10, 0]
            regime: str regime label
            reason: str human-readable reason
            base_setting: int from regime classifier
            plant_setpoint_f: float predicted firmware setpoint
            divergence_steps: float |predicted - observed| if setpoint available
            body_fb: int body feedback delta
            room_fb: int room feedback delta
            proxy_term: int movement-density proxy term
            residual_delta: int (0 if disabled)
            debug: dict of intermediate values
    """
    if config is None:
        config = DEFAULT_CONFIG
    if plant is None:
        plant = _DEFAULT_PLANT

    # ── Step 1: Regime classification ─────────────────────────────────
    regime_result = regime_mod.classify(
        zone,
        elapsed_min=snapshot.get("elapsed_min", 0.0),
        mins_since_onset=snapshot.get("mins_since_onset"),
        post_bedjet_min=snapshot.get("post_bedjet_min"),
        sleep_stage=snapshot.get("sleep_stage"),
        bed_occupied=snapshot.get("bed_occupied", True),
        room_f=snapshot.get("room_f"),
        body_skin_f=snapshot.get("body_skin_f"),
        body_hot_f=snapshot.get("body_hot_f"),
        body_avg_f=snapshot.get("body_avg_f"),
        override_freeze_active=snapshot.get("override_freeze_active", False),
        right_rail_engaged=snapshot.get("right_rail_engaged", False),
        pre_sleep_active=snapshot.get("pre_sleep_active", False),
        three_level_off=snapshot.get("three_level_off", True),
        movement_density_15m=snapshot.get("movement_density_15m"),
        config=config,
    )

    regime_label = regime_result["regime"]
    base_setting = regime_result["base_setting"]
    reason = regime_result["reason"]

    # For WAKE_COOL: use cycle-aware base (§9 table shows base follows cycle
    # baseline, not the flat -2 from regime.py). Policy layer overrides.
    if regime_label == "WAKE_COOL":
        elapsed = snapshot.get("elapsed_min", 0.0)
        cycle_idx = min(int(elapsed // 90), 5)
        if zone == "left":
            base_setting = config.cycle_baseline_left[cycle_idx]
        else:
            base_setting = config.cycle_baseline_right[cycle_idx]

    # For regimes that defer entirely (OVERRIDE, UNOCCUPIED, PRE_BED, INITIAL_COOL,
    # SAFETY_YIELD), return base directly without body/room feedback.
    passthrough_regimes = {"OVERRIDE", "UNOCCUPIED", "PRE_BED", "INITIAL_COOL", "SAFETY_YIELD"}
    if regime_label in passthrough_regimes:
        target = base_setting if base_setting is not None else snapshot.get("current_setting", 0)
        return _build_result(
            target=_clamp(target),
            regime=regime_label,
            reason=reason,
            base_setting=base_setting,
            plant=plant,
            snapshot=snapshot,
            body_fb=0,
            room_fb=0,
            proxy_term=0,
            residual_delta=0,
        )

    # ── Step 2: Body feedback (§8 gains table) ────────────────────────
    body_fb = _compute_body_fb(zone, snapshot, config)

    # ── Step 3: Room compensation feedback ────────────────────────────
    room_fb = _compute_room_fb(zone, snapshot, config)

    # ── Step 4: Movement-density proxy term (§3.4) ────────────────────
    proxy_term = _compute_proxy_term(zone, snapshot, config)

    # ── Step 5: Combine ───────────────────────────────────────────────
    combined = base_setting + body_fb + room_fb + proxy_term

    # ── Step 5b: Per-regime caps (§8) ─────────────────────────────────
    if regime_label == "COLD_ROOM_COMP":
        cap = config.cold_room_comp_cap_left if zone == "left" else config.cold_room_comp_cap_right
        combined = min(combined, cap)

    # ── Step 6: Residual head (if enabled) ────────────────────────────
    residual_delta = 0
    residual_meta = {}
    if residual_enabled:
        rh_mod = _get_residual_head()
        if rh_mod is not None:
            # Build feature dict for residual head
            elapsed = snapshot.get("elapsed_min", 0.0)
            features = {
                "cycle_phase": min(elapsed / 90.0, 6.0),
                "room_f": snapshot.get("room_f", 72.0),
                "body_skin_f": snapshot.get("body_skin_f", 80.0),
                "pre_sleep_min": 0,
                "post_bedjet_min": snapshot.get("post_bedjet_min") or 0,
                "bedjet_active": False,
                "body_hot_f": snapshot.get("body_hot_f", 80.0),
            }
            try:
                head = rh_mod.ResidualHead(zone=zone, cap_steps=1 if zone == "right" else 2)
                residual_delta, residual_meta = head.predict_lcb(features, k=config.residual_lcb_k)
            except Exception:
                residual_delta = 0

    combined += residual_delta

    # ── Step 7: Safety clamp — no positive writes ever ────────────────
    target = _clamp(combined)

    return _build_result(
        target=target,
        regime=regime_label,
        reason=reason,
        base_setting=base_setting,
        plant=plant,
        snapshot=snapshot,
        body_fb=body_fb,
        room_fb=room_fb,
        proxy_term=proxy_term,
        residual_delta=residual_delta,
    )


def _compute_body_fb(zone: str, snapshot: dict, config: RegimeConfig) -> int:
    """Body-temperature feedback per §8 gains table.

    For COLD_ROOM_COMP on LEFT: warm-bias when body is cold relative to
    ambient. Per §9 table: Kp_cold=1.25, max=+5.
    For RIGHT: cool-bias when body_hot exceeds proactive threshold.
    """
    body_skin_f = snapshot.get("body_skin_f")
    room_f = snapshot.get("room_f")
    if body_skin_f is None or room_f is None:
        return 0

    if zone == "left":
        # §8: body_fb = Kp_cold * (reference - body_skin_f) steps
        # Reference: 80°F (comfortable body-skin target)
        # If body_skin < 80 → positive (warm) bias
        body_ref = 80.0
        if body_skin_f < body_ref:
            deficit = body_ref - body_skin_f
            raw_fb = int(round(config.body_fb_kp_cold_left * deficit))
            return min(raw_fb, config.body_fb_max_delta_left)
        return 0
    else:
        # Right zone: hot-side bias when body_hot exceeds threshold
        body_hot_f = snapshot.get("body_hot_f")
        if body_hot_f is not None and body_hot_f > config.right_proactive_hot_f:
            excess = body_hot_f - config.right_proactive_hot_f
            raw_fb = -int(round(config.body_fb_kp_hot_right * excess))
            return max(raw_fb, -config.body_fb_max_delta_right)
        return 0


def _compute_room_fb(zone: str, snapshot: dict, config: RegimeConfig) -> int:
    """Room-temperature feedback (blower reference compensation)."""
    room_f = snapshot.get("room_f")
    if room_f is None:
        return 0

    # Room below reference → cold room → warm bias (+1 per 3°F below 72)
    if room_f < config.room_blower_reference_f:
        delta = config.room_blower_reference_f - room_f
        return min(int(delta / 3.0), 2)  # cap at +2
    return 0


def _compute_proxy_term(zone: str, snapshot: dict, config: RegimeConfig) -> int:
    """Movement-density proxy term per §3.4.

    On right zone: elevated movement → cooler bias (user restless → too warm).
    On left zone: minimal effect.
    """
    md = snapshot.get("movement_density_15m")
    if md is None:
        return 0

    # Baseline p75 ~0.05; elevated if > 2× p75 = 0.10
    baseline_p75 = 0.05
    if md <= baseline_p75:
        return 0

    if zone == "right":
        # Cool bias proportional to excess movement
        excess = md - baseline_p75
        raw = -int(round(config.movement_kproxy_right * excess / baseline_p75))
        return max(raw, -3)  # cap at -3 steps
    else:
        # Left: minimal proxy effect (§8 movement_kproxy_left=1.0)
        excess = md - baseline_p75
        raw = -int(round(config.movement_kproxy_left * excess / baseline_p75))
        return max(raw, -1)


def _clamp(setting: int | float) -> int:
    """Clamp setting to [-10, 0] — no positive writes ever (§11.3 #6)."""
    return max(-10, min(0, int(round(setting))))


def _build_result(
    *,
    target: int,
    regime: str,
    reason: str,
    base_setting: int | None,
    plant: FirmwarePlant,
    snapshot: dict,
    body_fb: int,
    room_fb: int,
    proxy_term: int,
    residual_delta: int,
) -> dict:
    """Construct the full plan result dict."""
    room_f = snapshot.get("room_f", 72.0)
    plant_setpoint_f = plant.predict_setpoint_f(target, ambient_f=room_f or 72.0)

    # Divergence: compare predicted vs observed setpoint if available
    observed_setpoint = snapshot.get("setpoint_f")
    if observed_setpoint is not None and not math.isnan(observed_setpoint):
        divergence_steps = abs(plant_setpoint_f - observed_setpoint)
    else:
        divergence_steps = 0.0

    return {
        "target": target,
        "regime": regime,
        "reason": reason,
        "base_setting": base_setting,
        "plant_setpoint_f": plant_setpoint_f,
        "divergence_steps": divergence_steps,
        "body_fb": body_fb,
        "room_fb": room_fb,
        "proxy_term": proxy_term,
        "residual_delta": residual_delta,
        "debug": {
            "zone": snapshot.get("zone", "left"),
            "elapsed_min": snapshot.get("elapsed_min", 0),
            "room_f": room_f,
            "body_skin_f": snapshot.get("body_skin_f"),
            "body_hot_f": snapshot.get("body_hot_f"),
            "movement_density_15m": snapshot.get("movement_density_15m"),
        },
    }


# ── V6SynthPolicy: adapter for v6_eval.py replay harness ─────────────

class V6SynthPolicy:
    """Policy adapter that wraps compute_v6_plan for v6_eval.py replay.

    Implements the Policy protocol: decide(state, history) -> int
    """

    name = "v6_synth"

    def decide(self, state: dict[str, Any], history: list[dict[str, Any]]) -> int:
        """Return v6 recommended L_active setting for this tick."""
        zone = state.get("zone", "left")
        # Map v6_eval state dict to snapshot format
        snapshot = self._state_to_snapshot(state, history)
        plan = compute_v6_plan(zone, snapshot)
        return plan["target"]

    @staticmethod
    def _state_to_snapshot(state: dict, history: list[dict]) -> dict:
        """Convert v6_eval replay state to compute_v6_plan snapshot format."""
        zone = state.get("zone", "left")
        elapsed = state.get("elapsed_min", 0.0) or 0.0

        # Determine bed_occupied from zone-specific field
        if zone == "left":
            bed_occ = state.get("bed_occupied_left", True)
        else:
            bed_occ = state.get("bed_occupied_right", True)

        # body_skin_f: use body_left for left zone primary
        body_left = state.get("body_left_f")
        body_center = state.get("body_center_f")
        body_avg = state.get("body_avg_f")
        room = state.get("room_temp_f") or state.get("ambient_f")

        # body_hot: max-channel (left for right zone post-BedJet)
        body_hot = body_left
        if zone == "right" and body_center is not None:
            body_hot = max(body_left or 0, body_center)

        # Estimate movement density from history (approximation)
        movement_density = _estimate_movement_from_history(history)

        return {
            "zone": zone,
            "elapsed_min": elapsed,
            "mins_since_onset": elapsed,  # approximation
            "post_bedjet_min": None,  # not available in baseline replay
            "sleep_stage": state.get("sleep_stage"),
            "bed_occupied": bed_occ if bed_occ is not None else True,
            "room_f": room,
            "body_skin_f": body_left,
            "body_hot_f": body_hot,
            "body_avg_f": body_avg,
            "body_center_f": body_center,
            "override_freeze_active": False,
            "right_rail_engaged": False,
            "pre_sleep_active": False,
            "three_level_off": True,
            "movement_density_15m": movement_density,
            "current_setting": state.get("current_setting", 0),
            "setpoint_f": state.get("setpoint_f"),
        }


def _estimate_movement_from_history(history: list[dict]) -> float | None:
    """Rough movement-density proxy from replay history.

    Uses body temperature volatility over last 3 ticks as a proxy for
    movement density (actual pressure data not available in replay).
    """
    if len(history) < 3:
        return None
    recent = history[-3:]
    temps = [h.get("body_left_f") or h.get("body_skin_f") for h in recent
             if h.get("body_left_f") or h.get("body_skin_f")]
    if len(temps) < 2:
        return None
    # Standard deviation as proxy (scaled to 0-1 range)
    mean_t = sum(temps) / len(temps)
    var = sum((t - mean_t) ** 2 for t in temps) / len(temps)
    sd = var ** 0.5
    # Scale: 0.5°F sd → 0.05 density; 2°F sd → 0.20 density
    return min(0.5, sd * 0.10)
