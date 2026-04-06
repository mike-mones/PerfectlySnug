"""Number entities for Perfectly Snug Smart Topper."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SETTING_FOOT_WARMER,
    SETTING_L1,
    SETTING_L2,
    SETTING_L3,
    SETTING_T1,
    SETTING_T3,
    SETTING_VOLUME,
)
from .coordinator import PerfectlySnugCoordinator

import logging

_LOGGER = logging.getLogger(__name__)

# L1/L2/L3 use an offset: raw 0-20 on device = display -10 to +10 in the app.
# Negative = cooling, zero = neutral, positive = warming.
_TEMP_OFFSET = 10

NUMBER_CONFIGS = {
    SETTING_L1: {
        "name": "Bedtime Temperature",
        "icon": "mdi:weather-night",
        "min": -10, "max": 10, "step": 1, "mode": NumberMode.SLIDER,
        "offset": _TEMP_OFFSET,
    },
    SETTING_L2: {
        "name": "Sleep Temperature",
        "icon": "mdi:sleep",
        "min": -10, "max": 10, "step": 1, "mode": NumberMode.SLIDER,
        "offset": _TEMP_OFFSET,
    },
    SETTING_L3: {
        "name": "Wake Temperature",
        "icon": "mdi:weather-sunny",
        "min": -10, "max": 10, "step": 1, "mode": NumberMode.SLIDER,
        "offset": _TEMP_OFFSET,
    },
    SETTING_FOOT_WARMER: {
        "name": "Foot Warmer",
        "icon": "mdi:shoe-print",
        "min": 0, "max": 3, "step": 1, "mode": NumberMode.SLIDER,
    },
    SETTING_VOLUME: {
        "name": "Speaker Volume",
        "icon": "mdi:volume-medium",
        "min": 0, "max": 10, "step": 1, "mode": NumberMode.SLIDER,
    },
    SETTING_T1: {
        "name": "Start Length (minutes)",
        "icon": "mdi:timer-outline",
        "min": 0, "max": 240, "step": 5, "mode": NumberMode.BOX,
        "unit": "min",
    },
    SETTING_T3: {
        "name": "Wake Length (minutes)",
        "icon": "mdi:timer-outline",
        "min": 0, "max": 240, "step": 5, "mode": NumberMode.BOX,
        "unit": "min",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities."""
    coordinator: PerfectlySnugCoordinator = entry.runtime_data
    entities = []
    for zone in coordinator.clients:
        for sid, cfg in NUMBER_CONFIGS.items():
            entities.append(
                PerfectlySnugNumber(coordinator, zone, entry, sid, cfg)
            )
    async_add_entities(entities)


class PerfectlySnugNumber(
    CoordinatorEntity[PerfectlySnugCoordinator], NumberEntity
):
    """Number entity for adjustable topper settings."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PerfectlySnugCoordinator,
        zone: str,
        entry: ConfigEntry,
        setting_id: int,
        cfg: dict,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone = zone
        self._setting_id = setting_id
        self._attr_unique_id = f"{entry.entry_id}_{zone}_number_{setting_id}"
        self._attr_name = cfg["name"]
        self._attr_icon = cfg["icon"]
        self._attr_native_min_value = cfg["min"]
        self._attr_native_max_value = cfg["max"]
        self._attr_native_step = cfg["step"]
        self._attr_mode = cfg["mode"]
        self._offset = cfg.get("offset", 0)
        if "unit" in cfg:
            self._attr_native_unit_of_measurement = cfg["unit"]
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
    def native_value(self) -> float | None:
        """Return current value (with offset applied for L1/L2/L3)."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            val = self.coordinator.data[self._zone].get(self._setting_id)
            if val is not None:
                return val - self._offset
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set new value (convert display value back to raw for L1/L2/L3)."""
        int_val = int(value) + self._offset
        # For foot warmer, also control heater limit
        if self._setting_id == SETTING_FOOT_WARMER:
            from .const import SETTING_HEATER_LIMIT
            if int_val == 0:
                _LOGGER.info("Foot warmer off → also setting HEATER_LIMIT=0")
                await self.coordinator.async_set_settings(
                    self._zone,
                    {SETTING_HEATER_LIMIT: 0, SETTING_FOOT_WARMER: 0},
                )
            else:
                _LOGGER.info("Foot warmer=%d → setting HEATER_LIMIT=100", int_val)
                await self.coordinator.async_set_settings(
                    self._zone,
                    {SETTING_FOOT_WARMER: int_val, SETTING_HEATER_LIMIT: 100},
                )
        else:
            if not await self.coordinator.async_set_setting(
                self._zone, self._setting_id, int_val
            ):
                raise HomeAssistantError("Could not reach Smart Topper")

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
