"""The Perfectly Snug Smart Topper integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .client import TopperClient
from .const import CONF_LEFT_IP, CONF_RIGHT_IP, CONF_ROOM_TEMP_ENTITY, CONF_SINGLE_ZONE, PLATFORMS
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

    room_temp_entity = entry.options.get(CONF_ROOM_TEMP_ENTITY, "")
    coordinator = PerfectlySnugCoordinator(
        hass, clients, room_temp_entity=room_temp_entity or None
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: PerfectlySnugConfigEntry
) -> None:
    """Handle options update — update room temp entity on the coordinator."""
    coordinator: PerfectlySnugCoordinator = entry.runtime_data
    new_entity = entry.options.get(CONF_ROOM_TEMP_ENTITY, "")
    coordinator.room_temp_entity = new_entity or None
    if not new_entity:
        coordinator.room_temp = None
    _LOGGER.info("Room temperature sensor updated to: %s", new_entity or "(disabled)")


async def async_unload_entry(
    hass: HomeAssistant, entry: PerfectlySnugConfigEntry
) -> bool:
    """Unload a config entry."""
    coordinator: PerfectlySnugCoordinator = entry.runtime_data
    coordinator.cancel_pending_refreshes()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
