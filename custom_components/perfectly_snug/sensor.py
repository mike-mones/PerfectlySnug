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
    SETTING_BL_OUT,
    SETTING_CTRL_ITERM,
    SETTING_CTRL_OUT,
    SETTING_CTRL_PTERM,
    SETTING_FH_OUT,
    SETTING_HH_OUT,
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
}

# Heater element temps use unknown encoding - expose as raw values only
HEATER_RAW_SENSORS = {
    SETTING_TEMP_HEATER_HEAD: ("Heater Head Raw", "mdi:radiator"),
    SETTING_TEMP_HEATER_FOOT: ("Heater Foot Raw", "mdi:radiator"),
}

PID_SENSORS = {
    SETTING_CTRL_OUT: ("PID Control Output", "mdi:tune-vertical"),
    SETTING_CTRL_ITERM: ("PID Integral Term", "mdi:sigma"),
    SETTING_CTRL_PTERM: ("PID Proportional Term", "mdi:delta"),
}

OUTPUT_SENSORS = {
    SETTING_BL_OUT: ("Blower Output", "mdi:fan", "%"),
    SETTING_HH_OUT: ("Heater Head Output", "mdi:radiator", "%"),
    SETTING_FH_OUT: ("Heater Foot Output", "mdi:radiator", "%"),
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
        for sid, (name, icon) in HEATER_RAW_SENSORS.items():
            entities.append(
                PerfectlySnugOutputSensor(coordinator, zone, entry, sid, name, icon, None)
            )
        for sid, (name, icon) in PID_SENSORS.items():
            entities.append(
                PerfectlySnugPIDSensor(coordinator, zone, entry, sid, name, icon)
            )
        for sid, (name, icon, unit) in OUTPUT_SENSORS.items():
            entities.append(
                PerfectlySnugOutputSensor(coordinator, zone, entry, sid, name, icon, unit)
            )
        entities.append(
            PerfectlySnugProgressSensor(coordinator, zone, entry)
        )

    # Add room temperature sensor if an external entity is configured
    if coordinator.room_temp_entity:
        first_zone = next(iter(coordinator.clients))
        entities.append(
            PerfectlySnugRoomTempSensor(coordinator, first_zone, entry)
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
    def available(self) -> bool:
        """Return True only if this zone's data is fresh."""
        if not super().available:
            return False
        return self.coordinator.is_zone_available(self._zone)

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
    def available(self) -> bool:
        """Return True only if this zone's data is fresh."""
        if not super().available:
            return False
        return self.coordinator.is_zone_available(self._zone)

    @property
    def native_value(self) -> int | None:
        """Return progress value."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            return self.coordinator.data[self._zone].get(SETTING_RUN_PROGRESS)
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class PerfectlySnugPIDSensor(
    CoordinatorEntity[PerfectlySnugCoordinator], SensorEntity
):
    """PID controller value sensor (signed fixed-point, offset 32768)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

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
        self._attr_unique_id = f"{entry.entry_id}_{zone}_pid_{setting_id}"
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
    def native_value(self) -> float | None:
        """Return PID value as signed value (raw - 32768) / 100."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            raw = self.coordinator.data[self._zone].get(self._setting_id)
            if raw is not None:
                return round((raw - 32768) / 100, 2)
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class PerfectlySnugOutputSensor(
    CoordinatorEntity[PerfectlySnugCoordinator], SensorEntity
):
    """Fan/heater output percentage sensor."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: PerfectlySnugCoordinator,
        zone: str,
        entry: ConfigEntry,
        setting_id: int,
        name: str,
        icon: str,
        unit: str | None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone = zone
        self._setting_id = setting_id
        self._attr_unique_id = f"{entry.entry_id}_{zone}_output_{setting_id}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
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
    def native_value(self) -> int | None:
        """Return output value. Returns 0 when topper is off (firmware freezes last value)."""
        if self.coordinator.data and self._zone in self.coordinator.data:
            from .const import SETTING_RUNNING
            zone_data = self.coordinator.data[self._zone]
            if not zone_data.get(SETTING_RUNNING):
                return 0
            return zone_data.get(self._setting_id)
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class PerfectlySnugRoomTempSensor(
    CoordinatorEntity[PerfectlySnugCoordinator], SensorEntity
):
    """Room temperature sensor sourced from an external HA entity.

    The topper's onboard ambient sensor reads 5-10°F higher than actual room
    temperature because it picks up radiated body heat. This sensor exposes
    the real room temperature from a user-configured external sensor.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_icon = "mdi:home-thermometer"

    def __init__(
        self,
        coordinator: PerfectlySnugCoordinator,
        zone: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_room_temperature"
        self._attr_name = "Room Temperature"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{zone}")},
            "name": f"Smart Topper {zone.title()} Side",
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    @property
    def native_value(self) -> float | None:
        """Return room temperature from the external sensor."""
        return self.coordinator.room_temp

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
