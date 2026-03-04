"""Data update coordinator for Perfectly Snug."""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import TopperClient
from .const import POLL_SETTINGS, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


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

    async def _async_update_data(self) -> dict[str, dict[int, int]]:
        """Fetch data from both zones."""
        data: dict[str, dict[int, int]] = {}

        async def fetch_zone(zone: str, client: TopperClient) -> None:
            try:
                settings = await client.async_get_settings(POLL_SETTINGS)
                data[zone] = settings
            except ConnectionError as err:
                _LOGGER.warning("Failed to update %s zone: %s", zone, err)
                # Keep previous data if available
                if self.data and zone in self.data:
                    data[zone] = self.data[zone]

        tasks = [fetch_zone(z, c) for z, c in self.clients.items()]
        await asyncio.gather(*tasks)

        if not data:
            raise UpdateFailed("Could not reach any topper zone")

        return data

    async def async_set_setting(
        self, zone: str, setting_id: int, value: int
    ) -> bool:
        """Set a setting on a specific zone."""
        client = self.clients.get(zone)
        if not client:
            return False
        result = await client.async_set_setting(setting_id, value)
        if result:
            # Update local data immediately
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
        result = await client.async_set_settings(settings)
        if result:
            if self.data and zone in self.data:
                self.data[zone].update(settings)
            await self.async_request_refresh()
        return result
