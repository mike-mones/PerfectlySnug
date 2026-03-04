"""Sensor entities for Perfectly Snug Smart Topper."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import raw_to_celsius
from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SETTING_RUN_PROGRESS,
    SETTING_TEMP_AMBIENT,
    SETTING_TEMP_HEATER_FOOT,
    SETTING_TEMP_HEATER_HEAD,
    SETTING_TEMP_SENSOR_CENTER,
    SETTING_TEMP_SENSOR_LEFT,
    SETTING_TEMP_SENSOR_RIGHT,
    SETTING_TEMP_SETPOINT,
)
from .coordinator import PerfectlySnugCoordinator

TEMP_SENSORS = {
    SETTING_TEMP_AMBIENT: "Ambient Temperature",
    SETTING_TEMP_SETPOINT: "Temperature Setpoint",
    SETTING_TEMP_SENSOR_RIGHT: "Body Sensor Right",
    SETTING_TEMP_SENSOR_CENTER: "Body Sensor Center",
    SETTING_TEMP_SENSOR_LEFT: "Body Sensor Left",
    SETTING_TEMP_HEATER_HEAD: "Heater Head Temperature",
    SETTING_TEMP_HEATER_FOOT: "Heater Foot Temperature",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities."""
    coordinator: PerfectlySnugCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    for zone in coordinator.clients:
        for sid, name in TEMP_SENSORS.items():
            entities.append(
                PerfectlySnugTempSensor(coordinator, zone, entry, sid, name)
            )
        entities.append(
            PerfectlySnugProgressSensor(coordinator, zone, entry)
        )

    async_add_entities(entities)


class PerfectlySnugTempSensor(
    CoordinatorEntity[PerfectlySnugCoordinator], SensorEntity
):
    """Temperature sensor entity."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: PerfectlySnugCoordinator,
        zone: str,
        entry: ConfigEntry,
        setting_id: int,
        name: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone = zone
        self._setting_id = setting_id
        self._attr_unique_id = f"{entry.entry_id}_{zone}_temp_{setting_id}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{zone}")},
            "name": f"Smart Topper {zone.title()} Side",
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            raw = self.coordinator.data[self._zone].get(self._setting_id)
            if raw is not None:
                return raw_to_celsius(raw)
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class PerfectlySnugProgressSensor(
    CoordinatorEntity[PerfectlySnugCoordinator], SensorEntity
):
    """Run progress sensor."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:progress-clock"

    def __init__(
        self,
        coordinator: PerfectlySnugCoordinator,
        zone: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_{zone}_progress"
        self._attr_name = "Run Progress"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{zone}")},
            "name": f"Smart Topper {zone.title()} Side",
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    @property
    def native_value(self) -> int | None:
        """Return progress value."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            return self.coordinator.data[self._zone].get(SETTING_RUN_PROGRESS)
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
