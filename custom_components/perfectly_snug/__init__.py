"""The Perfectly Snug Smart Topper integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .client import TopperClient
from .const import CONF_LEFT_IP, CONF_RIGHT_IP, CONF_SINGLE_ZONE, DOMAIN, PLATFORMS
from .coordinator import PerfectlySnugCoordinator

_LOGGER = logging.getLogger(__name__)

type PerfectlySnugConfigEntry = ConfigEntry[PerfectlySnugCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: PerfectlySnugConfigEntry
) -> bool:
    """Set up Perfectly Snug from a config entry."""
    left_ip = entry.data[CONF_LEFT_IP]
    single = entry.data.get(CONF_SINGLE_ZONE, False)

    clients: dict[str, TopperClient] = {"left": TopperClient(left_ip)}

    if not single:
        right_ip = entry.data.get(CONF_RIGHT_IP)
        if right_ip:
            clients["right"] = TopperClient(right_ip)

    coordinator = PerfectlySnugCoordinator(hass, clients)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PerfectlySnugConfigEntry
) -> bool:
    """Unload a config entry."""
    coordinator: PerfectlySnugCoordinator = entry.runtime_data
    coordinator.cancel_pending_refreshes()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
