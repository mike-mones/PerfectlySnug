"""Data update coordinator for Perfectly Snug."""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import TopperClient
from .const import (
    DOMAIN,
    POLL_SETTINGS,
    SETTING_L1,
    SETTING_L2,
    SETTING_L3,
    SETTING_FOOT_WARMER,
    SETTING_COOLING_MODE,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Settings that count as "user adjustable" for override detection
OVERRIDE_SETTINGS = {SETTING_L1, SETTING_L2, SETTING_L3, SETTING_FOOT_WARMER, SETTING_COOLING_MODE}

# Grace period after a set command: skip override detection for this many poll
# cycles so the device has time to apply our value and report it back.
_SET_GRACE_CYCLES = 2

# Max age (seconds) before a zone's data is considered stale and entities go
# unavailable instead of showing frozen values.
ZONE_STALE_TIMEOUT = 120  # 2 minutes (4 missed polls at 30s interval)


class PerfectlySnugCoordinator(DataUpdateCoordinator[dict[str, dict[int, int]]]):
    """Coordinator to poll both topper zones."""

    def __init__(
        self,
        hass: HomeAssistant,
        clients: dict[str, TopperClient],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Perfectly Snug",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.clients = clients
        self._last_set: dict[str, dict[int, tuple[int, int]]] = {}
        self.override_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._zone_last_success: dict[str, datetime] = {}
        self._last_polled: dict[str, dict[int, int]] = {}
        # Timer handles for delayed refreshes — cancelled on unload
        self._pending_refreshes: list[asyncio.TimerHandle] = []

    def is_zone_available(self, zone: str) -> bool:
        """Return True if a zone's data is fresh enough to be considered available."""
        last = self._zone_last_success.get(zone)
        if last is None:
            return False
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < ZONE_STALE_TIMEOUT

    async def _async_update_data(self) -> dict[str, dict[int, int]]:
        """Fetch data from both zones and detect manual overrides."""
        data: dict[str, dict[int, int]] = {}

        async def fetch_zone(zone: str, client: TopperClient) -> None:
            try:
                settings = await client.async_get_settings(POLL_SETTINGS)
                data[zone] = settings
                self._zone_last_success[zone] = datetime.now(timezone.utc)
            except ConnectionError as err:
                _LOGGER.warning("Failed to update %s zone: %s", zone, err)
                # Do NOT reuse stale data — leave zone out of data so
                # entities report unavailable instead of frozen values.

        tasks = [fetch_zone(z, c) for z, c in self.clients.items()]
        await asyncio.gather(*tasks)

        if not data:
            raise UpdateFailed("Could not reach any topper zone")

        # Detect manual overrides (only on zones we successfully polled)
        for zone, settings in data.items():
            if zone not in self._last_set:
                self._last_set[zone] = {}

            prev = self._last_polled.get(zone, {})
            for sid in OVERRIDE_SETTINGS:
                if sid not in settings or sid not in prev:
                    continue

                new_val = settings[sid]
                old_val = prev[sid]

                # Decrement grace cycles for tracked settings
                tracked = self._last_set[zone].get(sid)
                if tracked is not None:
                    expected_val, grace_remaining = tracked
                    if grace_remaining > 0:
                        # Still in grace period — don't detect overrides
                        self._last_set[zone][sid] = (expected_val, grace_remaining - 1)
                        continue
                    if new_val == expected_val:
                        # Device confirmed our value — stop tracking
                        self._last_set[zone].pop(sid, None)
                        continue
                    # Grace expired and device has a different value — clear
                    self._last_set[zone].pop(sid, None)

                if new_val != old_val:
                    delta = new_val - old_val
                    override = {
                        "zone": zone,
                        "setting_id": sid,
                        "old_value": old_val,
                        "new_value": new_val,
                        "delta": delta,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    self.override_log.append(override)
                    _LOGGER.info(
                        "Manual override detected: %s setting %d: %d → %d (delta %+d)",
                        zone, sid, old_val, new_val, delta,
                    )
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_manual_override",
                        override,
                    )

        # Update _last_polled AFTER override detection so next poll can
        # compare against this poll's values (not against itself).
        for zone, settings in data.items():
            self._last_polled[zone] = dict(settings)

        return data

    async def async_set_setting(
        self, zone: str, setting_id: int, value: int
    ) -> bool:
        """Set a setting on a specific zone."""
        client = self.clients.get(zone)
        if not client:
            return False
        # Track that we're setting this value, with grace cycles
        self._last_set.setdefault(zone, {})[setting_id] = (value, _SET_GRACE_CYCLES)
        try:
            await client.async_set_setting(setting_id, value)
        except ConnectionError:
            self._last_set.get(zone, {}).pop(setting_id, None)
            return False
        # Schedule a delayed refresh so the device has time to apply the value
        # before we poll. This prevents the false-override detection race.
        handle = self.hass.loop.call_later(2.0, lambda: self.hass.async_create_task(
            self.async_request_refresh()
        ))
        self._pending_refreshes.append(handle)
        return True

    async def async_set_settings(
        self, zone: str, settings: dict[int, int]
    ) -> bool:
        """Set multiple settings on a specific zone."""
        client = self.clients.get(zone)
        if not client:
            return False
        for sid, val in settings.items():
            self._last_set.setdefault(zone, {})[sid] = (val, _SET_GRACE_CYCLES)
        try:
            await client.async_set_settings(settings)
        except ConnectionError:
            for sid in settings:
                self._last_set.get(zone, {}).pop(sid, None)
            return False
        handle = self.hass.loop.call_later(2.0, lambda: self.hass.async_create_task(
            self.async_request_refresh()
        ))
        self._pending_refreshes.append(handle)
        return True

    def cancel_pending_refreshes(self):
        """Cancel any pending delayed refresh timers."""
        for handle in self._pending_refreshes:
            handle.cancel()
        self._pending_refreshes.clear()
