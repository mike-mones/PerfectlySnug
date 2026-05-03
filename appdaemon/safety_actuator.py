"""SafetyActuator — single chokepoint for v6 dial writes (proposal §7).

Wraps every v6 attempt to write `bedtime_temperature` (or sleep/wake/3-level)
with the unconditional safety chain:

    1. Cooling-only clip: target = min(target, 0); never positive.
    2. Master arm: input_boolean.snug_v6_enabled must be on.
    3. Per-zone live arm: snug_v6_<zone>_live must be on
       (else BLOCKED reason='shadow_only').
    4. CAS lease: input_text.snug_writer_owner_<zone> must equal 'v6'.
    5. Mutex with rail: if input_boolean.snug_right_rail_engaged on AND
       zone='right' AND target > -10, block reason='rail_engaged_right'.
    6. Rate limit: |target - last_v6_write| <= max_step_per_tick.
    7. Dead-man: if last successful tick > dead_man_sec ago, BLOCK and
       force fallback to 'v5' lease.
    8. Write via call_service number/set_value.
    9. Update last_v6_write_ts and snug_writer_owner_<zone>.

A `dry_run=True` instance (or `DummySafetyActuator`) returns
`{'written': None, 'blocked': True, 'reason': 'dry_run'}` for every write
without touching any HA state — used by unit tests and the shadow-only
controller in R2A.

This module imports nothing AppDaemon-specific; the `hass_app` argument is
duck-typed (any object with `.get_state()`, `.call_service()`, `.log()`),
so unit tests can pass a fake.
"""
from __future__ import annotations

import time
from typing import Optional


# ── Defaults ──────────────────────────────────────────────────────────
DEFAULT_MAX_STEP_PER_TICK = 2
DEFAULT_DEAD_MAN_SEC = 5 * 60          # 5 minutes
DEFAULT_RAIL_HOLD_TARGET = -10         # only allowed write when rail engaged

E_MASTER_ARM = "input_boolean.snug_v6_enabled"
E_RAIL_ENGAGED = "input_boolean.snug_right_rail_engaged"


def _live_flag(zone: str) -> str:
    return f"input_boolean.snug_v6_{zone}_live"


def _lease_helper(zone: str) -> str:
    return f"input_text.snug_writer_owner_{zone}"


def _bedtime_entity(zone: str) -> str:
    return f"number.smart_topper_{zone}_side_bedtime_temperature"


class SafetyActuator:
    """The §7 safety wrapper. One instance per zone."""

    def __init__(
        self,
        hass_app,
        zone: str,
        *,
        max_step_per_tick: int = DEFAULT_MAX_STEP_PER_TICK,
        dead_man_sec: float = DEFAULT_DEAD_MAN_SEC,
        dry_run: bool = False,
    ):
        if zone not in ("left", "right"):
            raise ValueError(f"zone must be 'left' or 'right', got {zone!r}")
        self.hass = hass_app
        self.zone = zone
        self.max_step_per_tick = int(max_step_per_tick)
        self.dead_man_sec = float(dead_man_sec)
        self.dry_run = bool(dry_run)
        self.last_v6_write: Optional[int] = None
        self.last_v6_write_ts: Optional[float] = None  # monotonic seconds

    # ── Public API ─────────────────────────────────────────────────────

    def write(self, target: int, *, regime: str, reason: str) -> dict:
        """Attempt to write a v6 target. Returns {written, blocked, reason}."""
        if self.dry_run:
            return self._result(None, True, "dry_run")

        # 1. Cooling-only clip
        try:
            target = int(target)
        except (TypeError, ValueError):
            return self._result(None, True, "invalid_target_type")
        clipped = min(target, 0)
        if clipped < -10:
            clipped = -10
        clip_changed = (clipped != target)
        target = clipped

        # 2. Master arm
        if self._read(E_MASTER_ARM) != "on":
            return self._result(None, True, "master_arm_off")

        # 3. Per-zone live
        if self._read(_live_flag(self.zone)) != "on":
            return self._result(None, True, "shadow_only")

        # 4. CAS lease
        owner = self._read(_lease_helper(self.zone))
        if owner != "v6":
            return self._result(None, True, "lease_held_by_v5")

        # 5. Mutex with rail (right zone only — and only block "warmer than -10")
        if self.zone == "right":
            if self._read(E_RAIL_ENGAGED) == "on" and target > DEFAULT_RAIL_HOLD_TARGET:
                return self._result(None, True, "rail_engaged_right")

        # 6. Rate limit
        if self.last_v6_write is not None:
            if abs(target - self.last_v6_write) > self.max_step_per_tick:
                return self._result(None, True, "rate_limit")

        # 7. Dead-man — if a previous v6 write exists, ensure we have ticked
        # recently. (On first-ever write last_v6_write_ts is None and we
        # allow it; the dead-man only protects continuous operation.)
        if self.last_v6_write_ts is not None:
            elapsed = time.monotonic() - self.last_v6_write_ts
            if elapsed > self.dead_man_sec:
                self.fallback_to_v5(reason="dead_man")
                return self._result(None, True, "dead_man")

        # 8. Write
        try:
            self.hass.call_service(
                "number/set_value",
                entity_id=_bedtime_entity(self.zone),
                value=target,
            )
        except Exception as e:
            self._log(f"safety_actuator write failed: {e}", level="ERROR")
            return self._result(None, True, "write_exception")

        # 9. Update last_v6_write_ts and lease (already 'v6' per CAS, but
        # rewrite to assert continued ownership).
        self.last_v6_write = target
        self.last_v6_write_ts = time.monotonic()
        self._set_lease("v6")

        note = f"regime={regime} reason={reason}"
        if clip_changed:
            note += " clipped_to_cool_only"
        self._log(f"safety_actuator wrote zone={self.zone} target={target} {note}")
        return self._result(target, False, "ok")

    # ── Lease management ───────────────────────────────────────────────

    def take_lease(self) -> bool:
        """Acquire the v6 writer lease. Returns True iff successful."""
        if self.dry_run:
            return False
        try:
            self._set_lease("v6")
            return self._read(_lease_helper(self.zone)) == "v6"
        except Exception as e:  # pragma: no cover
            self._log(f"safety_actuator take_lease failed: {e}", level="WARNING")
            return False

    def release_lease(self):
        """Release the lease back to v5."""
        if self.dry_run:
            return
        try:
            self._set_lease("v5")
        except Exception as e:  # pragma: no cover
            self._log(f"safety_actuator release_lease failed: {e}",
                      level="WARNING")

    def fallback_to_v5(self, reason: str):
        """Dead-man trigger: release lease and log."""
        self._log(f"safety_actuator FALLBACK to v5 (zone={self.zone}, "
                  f"reason={reason})", level="WARNING")
        self.release_lease()
        self.last_v6_write = None
        self.last_v6_write_ts = None
        # Optional notify (best-effort)
        try:
            if self.hass is not None:
                self.hass.call_service(
                    "persistent_notification/create",
                    title="PerfectlySnug v6 fallback",
                    message=f"v6 actuator fell back to v5 ({reason}, "
                            f"zone={self.zone})",
                )
        except Exception:  # pragma: no cover
            pass

    # ── Internals ──────────────────────────────────────────────────────

    def _read(self, entity_id: str) -> Optional[str]:
        if self.hass is None:
            return None
        try:
            return self.hass.get_state(entity_id)
        except Exception:  # pragma: no cover
            return None

    def _set_lease(self, value: str):
        if self.hass is None:
            return
        self.hass.call_service(
            "input_text/set_value",
            entity_id=_lease_helper(self.zone),
            value=value,
        )

    def _log(self, msg: str, *, level: str = "INFO"):
        if self.hass is None:
            return
        try:
            self.hass.log(msg, level=level)
        except Exception:  # pragma: no cover
            pass

    @staticmethod
    def _result(written: Optional[int], blocked: bool, reason: str) -> dict:
        return {"written": written, "blocked": blocked, "reason": reason}


class DummySafetyActuator(SafetyActuator):
    """A SafetyActuator that always blocks with reason='dry_run'.

    Convenience subclass for unit tests and shadow-only mode (R2A's
    SleepControllerV6 instantiates this and never calls write, but the
    instance is here for symmetry / future swap-in).
    """

    def __init__(self, zone: str = "left"):
        super().__init__(hass_app=None, zone=zone, dry_run=True)
