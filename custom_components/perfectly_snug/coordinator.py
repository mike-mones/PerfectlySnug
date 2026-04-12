"""Data update coordinator for Perfectly Snug."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import TopperClient
from .const import (
    CONF_ROOM_TEMP_ENTITY,
    POLL_SETTINGS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Max age (seconds) before a zone's data is considered stale and entities go
# unavailable instead of showing frozen values.
ZONE_STALE_TIMEOUT = 120  # 2 minutes (4 missed polls at 30s interval)


class PerfectlySnugCoordinator(DataUpdateCoordinator[dict[str, dict[int, int]]]):
    """Coordinator to poll both topper zones."""

    def __init__(
        self,
        hass: HomeAssistant,
        clients: dict[str, TopperClient],
        room_temp_entity: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Perfectly Snug",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.clients = clients
        self.room_temp_entity = room_temp_entity
        self.room_temp: float | None = None
        self._zone_last_success: dict[str, datetime] = {}
        self._pending_refresh: asyncio.TimerHandle | None = None

    def is_zone_available(self, zone: str) -> bool:
        """Return True if a zone's data is fresh enough to be considered available."""
        last = self._zone_last_success.get(zone)
        if last is None:
            return False
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < ZONE_STALE_TIMEOUT

    async def _async_update_data(self) -> dict[str, dict[int, int]]:
        """Fetch data from both zones."""
        # Carry forward previous data so partial failures don't cause KeyError
        data: dict[str, dict[int, int]] = dict(self.data) if self.data else {}

        # Read external room temperature sensor if configured
        if self.room_temp_entity:
            state = self.hass.states.get(self.room_temp_entity)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    self.room_temp = float(state.state)
                except (ValueError, TypeError):
                    self.room_temp = None
            else:
                self.room_temp = None

        async def fetch_zone(zone: str, client: TopperClient) -> None:
            try:
                settings = await client.async_get_settings(POLL_SETTINGS)
                data[zone] = settings
                self._zone_last_success[zone] = datetime.now(timezone.utc)
            except ConnectionError as err:
                _LOGGER.warning("Failed to update %s zone: %s", zone, err)

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
        try:
            await client.async_set_setting(setting_id, value)
        except ConnectionError:
            return False
        self._schedule_delayed_refresh()
        return True

    async def async_set_settings(
        self, zone: str, settings: dict[int, int]
    ) -> bool:
        """Set multiple settings on a specific zone."""
        client = self.clients.get(zone)
        if not client:
            return False
        try:
            await client.async_set_settings(settings)
        except ConnectionError:
            return False
        self._schedule_delayed_refresh()
        return True

    def _schedule_delayed_refresh(self):
        """Schedule a single delayed refresh, cancelling any prior pending one."""
        if self._pending_refresh is not None:
            self._pending_refresh.cancel()
        self._pending_refresh = self.hass.loop.call_later(
            2.0, lambda: self.hass.async_create_task(self.async_request_refresh())
        )

    def cancel_pending_refreshes(self):
        """Cancel any pending delayed refresh timer."""
        if self._pending_refresh is not None:
            self._pending_refresh.cancel()
            self._pending_refresh = None
