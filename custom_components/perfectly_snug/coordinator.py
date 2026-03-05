"""Data update coordinator for Perfectly Snug."""

import asyncio
import logging
from datetime import datetime, timedelta
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
        # Track what we last set, so we can detect external changes
        self._last_set: dict[str, dict[int, int]] = {}
        # Override event log: list of {zone, setting_id, old, new, delta, timestamp}
        self.override_log: list[dict[str, Any]] = []

    async def _async_update_data(self) -> dict[str, dict[int, int]]:
        """Fetch data from both zones and detect manual overrides."""
        data: dict[str, dict[int, int]] = {}

        async def fetch_zone(zone: str, client: TopperClient) -> None:
            try:
                settings = await client.async_get_settings(POLL_SETTINGS)
                data[zone] = settings
            except ConnectionError as err:
                _LOGGER.warning("Failed to update %s zone: %s", zone, err)
                if self.data and zone in self.data:
                    data[zone] = self.data[zone]

        tasks = [fetch_zone(z, c) for z, c in self.clients.items()]
        await asyncio.gather(*tasks)

        if not data:
            raise UpdateFailed("Could not reach any topper zone")

        # Detect manual overrides
        for zone, settings in data.items():
            if zone not in self._last_set:
                # First poll — initialize tracking, no override detection yet
                self._last_set[zone] = {}
                continue

            prev = self.data.get(zone, {}) if self.data else {}
            for sid in OVERRIDE_SETTINGS:
                if sid not in settings or sid not in prev:
                    continue
                new_val = settings[sid]
                old_val = prev[sid]
                expected = self._last_set[zone].get(sid)

                if new_val != old_val:
                    # Value changed. Did WE change it?
                    if expected is not None and new_val == expected:
                        # We set this value — not an override
                        self._last_set[zone].pop(sid, None)
                    else:
                        # External change — manual override!
                        delta = new_val - old_val
                        override = {
                            "zone": zone,
                            "setting_id": sid,
                            "old_value": old_val,
                            "new_value": new_val,
                            "delta": delta,
                            "timestamp": datetime.now().isoformat(),
                        }
                        self.override_log.append(override)
                        _LOGGER.info(
                            "Manual override detected: %s setting %d: %d → %d (delta %+d)",
                            zone, sid, old_val, new_val, delta,
                        )
                        # Fire an HA event so automations can react
                        self.hass.bus.async_fire(
                            f"{DOMAIN}_manual_override",
                            override,
                        )

        return data

    async def async_set_setting(
        self, zone: str, setting_id: int, value: int
    ) -> bool:
        """Set a setting on a specific zone."""
        client = self.clients.get(zone)
        if not client:
            return False
        # Track that we're the ones setting this
        self._last_set.setdefault(zone, {})[setting_id] = value
        result = await client.async_set_setting(setting_id, value)
        if result:
            if self.data and zone in self.data:
                self.data[zone][setting_id] = value
            await self.async_request_refresh()
        return result

    async def async_set_settings(
        self, zone: str, settings: dict[int, int]
    ) -> bool:
        """Set multiple settings on a specific zone."""
        client = self.clients.get(zone)
        if not client:
            return False
        # Track all values we're setting
        for sid, val in settings.items():
            self._last_set.setdefault(zone, {})[sid] = val
        result = await client.async_set_settings(settings)
        if result:
            if self.data and zone in self.data:
                self.data[zone].update(settings)
            await self.async_request_refresh()
        return result
