"""Switch entities for Perfectly Snug Smart Topper."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SETTING_COOLING_MODE,
    SETTING_PROFILE_ENABLE,
    SETTING_QUIET_ENABLE,
    SETTING_RUNNING,
    SETTING_SCHEDULE_ENABLE,
)
from .coordinator import PerfectlySnugCoordinator

SWITCHES = {
    SETTING_RUNNING: ("Running", "mdi:power"),
    SETTING_SCHEDULE_ENABLE: ("Schedule", "mdi:calendar-clock"),
    SETTING_COOLING_MODE: ("Responsive Cooling", "mdi:snowflake-thermometer"),
    SETTING_QUIET_ENABLE: ("Quiet Mode", "mdi:volume-off"),
    SETTING_PROFILE_ENABLE: ("3-Level Mode", "mdi:chart-timeline-variant"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: PerfectlySnugCoordinator = entry.runtime_data
    entities = []
    for zone in coordinator.clients:
        for sid, (name, icon) in SWITCHES.items():
            entities.append(
                PerfectlySnugSwitch(coordinator, zone, entry, sid, name, icon)
            )
    async_add_entities(entities)


class PerfectlySnugSwitch(
    CoordinatorEntity[PerfectlySnugCoordinator], SwitchEntity
):
    """Switch entity for boolean topper settings."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PerfectlySnugCoordinator,
        zone: str,
        entry: ConfigEntry,
        setting_id: int,
        name: str,
        icon: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone = zone
        self._setting_id = setting_id
        self._attr_unique_id = f"{entry.entry_id}_{zone}_switch_{setting_id}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{zone}")},
            "name": f"Smart Topper {zone.title()} Side",
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    @property
    def available(self) -> bool:
        """Return True only if this zone's data is fresh."""
        if not super().available:
            return False
        return self.coordinator.is_zone_available(self._zone)

    @property
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            val = self.coordinator.data[self._zone].get(self._setting_id)
            return val == 1 if val is not None else None
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on."""
        if not await self.coordinator.async_set_setting(self._zone, self._setting_id, 1):
            raise HomeAssistantError("Could not reach Smart Topper")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        if not await self.coordinator.async_set_setting(self._zone, self._setting_id, 0):
            raise HomeAssistantError("Could not reach Smart Topper")

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
