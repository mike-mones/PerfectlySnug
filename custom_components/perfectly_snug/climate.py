"""Climate entities for Perfectly Snug Smart Topper."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import raw_to_celsius
from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SETTING_COOLING_MODE,
    SETTING_HEATER_LIMIT,
    SETTING_L1,
    SETTING_RUNNING,
    SETTING_TEMP_AMBIENT,
    SETTING_TEMP_SENSOR_CENTER,
)
from .coordinator import PerfectlySnugCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entities."""
    coordinator: PerfectlySnugCoordinator = entry.runtime_data
    entities = []
    for zone in coordinator.clients:
        entities.append(PerfectlySnugClimate(coordinator, zone, entry))
    async_add_entities(entities)


class PerfectlySnugClimate(CoordinatorEntity[PerfectlySnugCoordinator], ClimateEntity):
    """Climate entity for a Perfectly Snug zone."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL, HVACMode.COOL, HVACMode.HEAT]
    _attr_min_temp = -10
    _attr_max_temp = 10
    _attr_target_temperature_step = 1

    def __init__(
        self,
        coordinator: PerfectlySnugCoordinator,
        zone: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_{zone}_climate"
        self._attr_name = f"{zone.title()} Side"
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
    def _data(self) -> dict[int, int]:
        """Get zone data."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            return self.coordinator.data[self._zone]
        return {}

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature (center sensor)."""
        raw = self._data.get(SETTING_TEMP_SENSOR_CENTER)
        if raw is not None:
            return raw_to_celsius(raw)
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return target temp as the L1 setting on -10 to +10 scale."""
        val = self._data.get(SETTING_L1)
        if val is not None:
            return val - 10  # Convert 0-20 to -10 to +10
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        running = self._data.get(SETTING_RUNNING, 0)
        if not running:
            return HVACMode.OFF

        l1 = self._data.get(SETTING_L1, 10)
        display = l1 - 10
        if display < 0:
            return HVACMode.COOL
        if display > 0:
            return HVACMode.HEAT
        return HVACMode.HEAT_COOL

    @property
    def hvac_action(self) -> HVACAction:
        """Return current HVAC action."""
        running = self._data.get(SETTING_RUNNING, 0)
        if not running:
            return HVACAction.OFF

        heater_limit = self._data.get(SETTING_HEATER_LIMIT, 0)
        cooling = self._data.get(SETTING_COOLING_MODE, 0)

        l1 = self._data.get(SETTING_L1, 10)
        if l1 < 10:
            return HVACAction.COOLING
        if l1 > 10 or heater_limit > 0:
            return HVACAction.HEATING
        return HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature (-10 to +10 scale)."""
        temp = kwargs.get("temperature")
        if temp is not None:
            value = int(temp) + 10  # Convert -10..+10 to 0..20
            value = max(0, min(20, value))
            if not await self.coordinator.async_set_setting(
                self._zone, SETTING_L1, value
            ):
                raise HomeAssistantError("Could not reach Smart Topper")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode. Only toggles SETTING_RUNNING; preserves user's L1 value."""
        if hvac_mode == HVACMode.OFF:
            if not await self.coordinator.async_set_setting(self._zone, SETTING_RUNNING, 0):
                raise HomeAssistantError("Could not reach Smart Topper")
        else:
            running = self._data.get(SETTING_RUNNING, 0)
            if not running:
                if not await self.coordinator.async_set_setting(self._zone, SETTING_RUNNING, 1):
                    raise HomeAssistantError("Could not reach Smart Topper")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
