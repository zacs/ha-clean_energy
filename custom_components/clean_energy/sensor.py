"""Diagnostic sensor entities for Clean Energy."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.helpers.device import async_device_info_to_link_from_entity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENTITY_ID, SIGNAL_SPIKE_CORRECTED


def _parent_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Best-effort friendly name for the monitored sensor."""
    state = hass.states.get(entity_id)
    if state and state.attributes.get("friendly_name"):
        return state.attributes["friendly_name"]
    # Fall back to the object_id with underscores converted to spaces.
    return split_entity_id(entity_id)[1].replace("_", " ").title()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up diagnostic sensors for a per-entity config entry."""
    entity_id = entry.data.get(CONF_ENTITY_ID)
    if not entity_id:
        # Hub entry - no sensors
        return

    device_info = async_device_info_to_link_from_entity(hass, entity_id)
    parent_object_id = split_entity_id(entity_id)[1]
    parent_friendly = _parent_friendly_name(hass, entity_id)

    async_add_entities(
        [
            LastSpikeTimeSensor(entry, entity_id, device_info, parent_object_id, parent_friendly),
            LastSpikeSizeSensor(entry, entity_id, device_info, parent_object_id, parent_friendly),
            TotalCorrectedSensor(entry, entity_id, device_info, parent_object_id, parent_friendly),
            SpikeCountSensor(entry, entity_id, device_info, parent_object_id, parent_friendly),
        ]
    )


class CleanEnergyDiagnosticSensor(SensorEntity):
    """Base class for Clean Energy diagnostic sensors."""

    # We compose names manually so the entity_id is derived from the parent
    # sensor (e.g. sensor.<parent>_corrected) rather than from a device name.
    # Many energy sources (template sensors, integration sensors) have no
    # device, which would otherwise leave us with bare names like
    # "sensor.total_energy_corrected".
    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    # Subclass overrides these:
    _name_suffix: str = ""  # human-readable, e.g. "Corrected"
    _id_suffix: str = ""  # entity_id suffix, e.g. "corrected"

    def __init__(
        self,
        entry: ConfigEntry,
        monitored_entity_id: str,
        device_info: DeviceInfo | None,
        parent_object_id: str,
        parent_friendly: str,
    ) -> None:
        self._monitored_entity_id = monitored_entity_id
        self._attr_unique_id = f"{entry.entry_id}_{self._id_suffix}"
        self._attr_name = f"{parent_friendly} {self._name_suffix}"
        self._attr_suggested_object_id = f"{parent_object_id}_{self._id_suffix}"
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register for spike correction signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_SPIKE_CORRECTED}_{self._monitored_entity_id}",
                self._handle_spike,
            )
        )

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        """Handle a spike correction event. Override in subclasses."""
        raise NotImplementedError


class LastSpikeTimeSensor(CleanEnergyDiagnosticSensor):
    """When the last spike was detected and corrected."""

    _name_suffix = "Last Spike"
    _id_suffix = "last_spike"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry, entity_id, device_info, parent_object_id, parent_friendly):
        super().__init__(entry, entity_id, device_info, parent_object_id, parent_friendly)
        self._attr_native_value: datetime | None = None

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = timestamp
        self.async_write_ha_state()


class LastSpikeSizeSensor(CleanEnergyDiagnosticSensor):
    """Size of the last corrected spike."""

    _name_suffix = "Last Spike Size"
    _id_suffix = "last_spike_size"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_suggested_display_precision = 3

    def __init__(self, entry, entity_id, device_info, parent_object_id, parent_friendly):
        super().__init__(entry, entity_id, device_info, parent_object_id, parent_friendly)
        self._attr_native_value: float | None = None

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = spike_kwh
        self.async_write_ha_state()


class TotalCorrectedSensor(CleanEnergyDiagnosticSensor):
    """Cumulative energy removed by corrections."""

    _name_suffix = "Energy Removed"
    _id_suffix = "energy_removed"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, entry, entity_id, device_info, parent_object_id, parent_friendly):
        super().__init__(entry, entity_id, device_info, parent_object_id, parent_friendly)
        self._attr_native_value: float = 0.0

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = (self._attr_native_value or 0.0) + spike_kwh
        self.async_write_ha_state()


class SpikeCountSensor(CleanEnergyDiagnosticSensor):
    """Number of spikes corrected."""

    _name_suffix = "Spike Count"
    _id_suffix = "spike_count"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, entry, entity_id, device_info, parent_object_id, parent_friendly):
        super().__init__(entry, entity_id, device_info, parent_object_id, parent_friendly)
        self._attr_native_value: int = 0

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = (self._attr_native_value or 0) + 1
        self.async_write_ha_state()
