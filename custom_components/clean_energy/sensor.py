"""Clean Energy entities.

For each monitored source sensor we expose:

* ``CleanFilterSensor`` — a parallel ``total_increasing`` energy entity that
  mirrors the source's value but holds flat across spikes. The Energy
  Dashboard should be pointed at this entity instead of the original. The
  source entity is left completely untouched (no LTS adjustments, no history
  rewrites), so the integration is non-destructive.

* Diagnostic sensors (``Last Spike``, ``Last Spike Size``, ``Energy
  Removed``, ``Spike Count``) — these listen to a dispatcher signal that the
  filter sensor fires whenever it suppresses a spike.

On first setup we copy the source sensor's existing hourly Long-Term
Statistics history over to the new clean entity (best effort) so that the
dashboard retains continuity when the user swaps over.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import Event, HomeAssistant, callback, split_entity_id
from homeassistant.helpers.device import async_device_info_to_link_from_entity
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_DONE_KEY,
    CONF_ENTITY_ID,
    CONF_MAX_POWER_KW,
    DEFAULT_MAX_POWER_KW,
    DOMAIN,
    MIN_ELAPSED_SECONDS,
    SIGNAL_SPIKE_CORRECTED,
)

_LOGGER = logging.getLogger(__name__)

# Conversion factors to kWh (kept local to avoid an import cycle with __init__).
_TO_KWH: dict[str, float] = {
    UnitOfEnergy.KILO_WATT_HOUR: 1.0,
    UnitOfEnergy.WATT_HOUR: 0.001,
    UnitOfEnergy.MEGA_WATT_HOUR: 1000.0,
    "kWh": 1.0,
    "Wh": 0.001,
    "MWh": 1000.0,
}


def _hub_max_power_kw(hass: HomeAssistant) -> float:
    """Read the global max-power threshold from the hub config entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not entry.data.get(CONF_ENTITY_ID):
            return entry.options.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW)
    return DEFAULT_MAX_POWER_KW


def _parent_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Best-effort friendly name for the monitored sensor."""
    state = hass.states.get(entity_id)
    if state and state.attributes.get("friendly_name"):
        return state.attributes["friendly_name"]
    return split_entity_id(entity_id)[1].replace("_", " ").title()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the clean filter sensor and diagnostics for a per-entity entry."""
    entity_id = entry.data.get(CONF_ENTITY_ID)
    if not entity_id:
        # Hub entry — no sensors.
        return

    device_info = async_device_info_to_link_from_entity(hass, entity_id)
    parent_object_id = split_entity_id(entity_id)[1]
    parent_friendly = _parent_friendly_name(hass, entity_id)

    async_add_entities(
        [
            CleanFilterSensor(
                entry, entity_id, device_info, parent_object_id, parent_friendly
            ),
            LastSpikeTimeSensor(
                entry, entity_id, device_info, parent_object_id, parent_friendly
            ),
            LastSpikeSizeSensor(
                entry, entity_id, device_info, parent_object_id, parent_friendly
            ),
            TotalCorrectedSensor(
                entry, entity_id, device_info, parent_object_id, parent_friendly
            ),
            SpikeCountSensor(
                entry, entity_id, device_info, parent_object_id, parent_friendly
            ),
        ]
    )

    if not entry.data.get(BACKFILL_DONE_KEY):
        target_id = f"sensor.{parent_object_id}_clean"
        hass.async_create_task(
            _backfill_history(hass, entry, entity_id, target_id, parent_friendly)
        )


# ---------------------------------------------------------------------------
# History backfill
# ---------------------------------------------------------------------------


async def _backfill_history(
    hass: HomeAssistant,
    entry: ConfigEntry,
    source_id: str,
    target_id: str,
    parent_friendly: str,
) -> None:
    """Copy the source sensor's hourly LTS over to the new clean entity.

    Best effort: failures don't block setup. We only copy hours strictly
    before the current hour to avoid stepping on the recorder's live
    compilation of the in-progress hour for the new entity.
    """
    source_state = hass.states.get(source_id)
    unit = (
        source_state.attributes.get("unit_of_measurement", "kWh")
        if source_state
        else "kWh"
    )

    now = dt_util.utcnow()
    end_time = now.replace(minute=0, second=0, microsecond=0)
    start_time = dt_util.utc_from_timestamp(0)

    def _fetch() -> dict:
        return statistics_during_period(
            hass,
            start_time,
            end_time,
            {source_id},
            "hour",
            None,
            {"sum", "state"},
        )

    try:
        rows = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception:
        _LOGGER.exception("Clean Energy: failed to fetch LTS history for %s", source_id)
        return

    series = rows.get(source_id) or []
    if not series:
        _LOGGER.info("Clean Energy: no prior LTS history to backfill for %s", source_id)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, BACKFILL_DONE_KEY: True}
        )
        return

    statistics: list[StatisticData] = []
    for row in series:
        start = row.get("start")
        if isinstance(start, (int, float)):
            # statistics_during_period returns timestamps in seconds when
            # called from Python (the WS layer multiplies by 1000 itself).
            start_dt = dt_util.utc_from_timestamp(float(start))
        elif isinstance(start, datetime):
            start_dt = start
        else:
            continue
        start_dt = start_dt.replace(minute=0, second=0, microsecond=0)
        stat: StatisticData = {"start": start_dt}
        if (s := row.get("sum")) is not None:
            stat["sum"] = float(s)
        if (st := row.get("state")) is not None:
            stat["state"] = float(st)
        if "sum" not in stat and "state" not in stat:
            continue
        statistics.append(stat)

    if not statistics:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, BACKFILL_DONE_KEY: True}
        )
        return

    metadata: StatisticMetaData = {
        "has_mean": False,
        "has_sum": True,
        "name": f"{parent_friendly} (Clean)",
        "source": "recorder",
        "statistic_id": target_id,
        "unit_of_measurement": unit,
    }

    try:
        async_import_statistics(hass, metadata, statistics)
    except Exception:
        _LOGGER.exception(
            "Clean Energy: failed to import backfilled history into %s", target_id
        )
        return

    _LOGGER.info(
        "Clean Energy: backfilled %d hourly LTS rows from %s into %s",
        len(statistics),
        source_id,
        target_id,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, BACKFILL_DONE_KEY: True}
    )


# ---------------------------------------------------------------------------
# Filter sensor: a clean parallel of the source
# ---------------------------------------------------------------------------


class CleanFilterSensor(SensorEntity):
    """A ``total_increasing`` energy sensor that mirrors the source, sans spikes.

    Algorithm per source state change:
      * If the source dropped (likely a real ``total_increasing`` reset, e.g.
        a meter rollover or device restart), adopt the new value as our
        baseline so the recorder will detect our cycle reset cleanly too.
      * If the implied power between the previous good value and the new
        value exceeds the configured threshold, hold our emitted state at
        the previous good value (don't update). Subsequent ticks compare
        against that same baseline, so a multi-tick spike-and-revert is
        absorbed in full.
      * Otherwise, emit the new value and advance the baseline.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        entry: ConfigEntry,
        source_id: str,
        device_info: DeviceInfo | None,
        parent_object_id: str,
        parent_friendly: str,
    ) -> None:
        """Initialise the clean filter sensor."""
        self._source_id = source_id
        self._attr_unique_id = f"{entry.entry_id}_clean"
        self._attr_suggested_object_id = f"{parent_object_id}_clean"
        self._attr_name = f"{parent_friendly} (Clean)"
        if device_info:
            self._attr_device_info = device_info
        self._last_good_value: float | None = None
        self._last_good_time: datetime | None = None
        self._native: float | None = None
        self._unit: str = UnitOfEnergy.KILO_WATT_HOUR
        self._spike_active: bool = False

    @property
    def native_value(self) -> float | None:
        """Return the filtered energy value."""
        return self._native

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the unit of measurement, mirrored from the source."""
        return self._unit

    async def async_added_to_hass(self) -> None:
        """Seed from the source's current state and start tracking changes."""
        source = self.hass.states.get(self._source_id)
        if source is not None and source.state not in ("unknown", "unavailable", None):
            try:
                value = float(source.state)
            except (ValueError, TypeError):
                value = None
            if value is not None:
                self._last_good_value = value
                self._last_good_time = source.last_changed or dt_util.utcnow()
                self._native = value
                self._unit = source.attributes.get(
                    "unit_of_measurement", UnitOfEnergy.KILO_WATT_HOUR
                )

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source_id], self._handle_source_change
            )
        )

    @callback
    def _handle_source_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            new_val = float(new_state.state)
        except (ValueError, TypeError):
            return

        unit = new_state.attributes.get("unit_of_measurement", "kWh")
        factor = _TO_KWH.get(unit)
        if factor is None:
            return
        self._unit = unit

        now = new_state.last_changed or dt_util.utcnow()

        if self._last_good_value is None or self._last_good_time is None:
            self._last_good_value = new_val
            self._last_good_time = now
            self._native = new_val
            self.async_write_ha_state()
            return

        diff = new_val - self._last_good_value

        if diff < 0:
            # Genuine total_increasing reset on the source. Adopt the new
            # baseline and let the recorder treat it as our reset too.
            self._last_good_value = new_val
            self._last_good_time = now
            self._native = new_val
            self._spike_active = False
            self.async_write_ha_state()
            return

        elapsed = max((now - self._last_good_time).total_seconds(), MIN_ELAPSED_SECONDS)
        jump_kwh = diff * factor
        implied_kw = jump_kwh / (elapsed / 3600.0)
        threshold_kw = _hub_max_power_kw(self.hass)

        if implied_kw > threshold_kw:
            # Spike: hold our emitted state. Fire the diagnostic signal once
            # per spike *event* (not once per tick during a multi-tick spike).
            if not self._spike_active:
                self._spike_active = True
                _LOGGER.info(
                    "Clean Energy: filtered spike on %s (%.3f → %.3f %s, "
                    "implied %.1f kW > %.0f kW limit)",
                    self._source_id,
                    self._last_good_value,
                    new_val,
                    unit,
                    implied_kw,
                    threshold_kw,
                )
                async_dispatcher_send(
                    self.hass,
                    f"{SIGNAL_SPIKE_CORRECTED}_{self._source_id}",
                    jump_kwh,
                    now,
                )
            return

        # Normal reading.
        self._last_good_value = new_val
        self._last_good_time = now
        self._native = new_val
        self._spike_active = False
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Diagnostic sensors
# ---------------------------------------------------------------------------


class CleanEnergyDiagnosticSensor(SensorEntity):
    """Base class for Clean Energy diagnostic sensors."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _name_suffix: str = ""
    _id_suffix: str = ""

    def __init__(
        self,
        entry: ConfigEntry,
        monitored_entity_id: str,
        device_info: DeviceInfo | None,
        parent_object_id: str,
        parent_friendly: str,
    ) -> None:
        """Initialise the diagnostic sensor."""
        self._monitored_entity_id = monitored_entity_id
        self._attr_unique_id = f"{entry.entry_id}_{self._id_suffix}"
        self._attr_name = f"{parent_friendly} {self._name_suffix}"
        self._attr_suggested_object_id = f"{parent_object_id}_{self._id_suffix}"
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Subscribe to the spike-corrected dispatcher signal."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_SPIKE_CORRECTED}_{self._monitored_entity_id}",
                self._handle_spike,
            )
        )

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        raise NotImplementedError


class LastSpikeTimeSensor(CleanEnergyDiagnosticSensor):
    """Records the timestamp of the most recent filtered spike."""

    _name_suffix = "Last Spike"
    _id_suffix = "last_spike"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, entry, entity_id, device_info, parent_object_id, parent_friendly
    ):
        """Initialise with a ``None`` initial value."""
        super().__init__(
            entry, entity_id, device_info, parent_object_id, parent_friendly
        )
        self._attr_native_value: datetime | None = None

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = timestamp
        self.async_write_ha_state()


class LastSpikeSizeSensor(CleanEnergyDiagnosticSensor):
    """Records the size (kWh) of the most recent filtered spike."""

    _name_suffix = "Last Spike Size"
    _id_suffix = "last_spike_size"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_suggested_display_precision = 3

    def __init__(
        self, entry, entity_id, device_info, parent_object_id, parent_friendly
    ):
        """Initialise with a ``None`` initial value."""
        super().__init__(
            entry, entity_id, device_info, parent_object_id, parent_friendly
        )
        self._attr_native_value: float | None = None

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = spike_kwh
        self.async_write_ha_state()


class TotalCorrectedSensor(CleanEnergyDiagnosticSensor):
    """Cumulative kWh suppressed by all filtered spikes."""

    _name_suffix = "Energy Removed"
    _id_suffix = "energy_removed"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self, entry, entity_id, device_info, parent_object_id, parent_friendly
    ):
        """Initialise the running total at zero."""
        super().__init__(
            entry, entity_id, device_info, parent_object_id, parent_friendly
        )
        self._attr_native_value: float = 0.0

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = (self._attr_native_value or 0.0) + spike_kwh
        self.async_write_ha_state()


class SpikeCountSensor(CleanEnergyDiagnosticSensor):
    """Counts the number of spike events filtered for this source."""

    _name_suffix = "Spike Count"
    _id_suffix = "spike_count"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self, entry, entity_id, device_info, parent_object_id, parent_friendly
    ):
        """Initialise the counter at zero."""
        super().__init__(
            entry, entity_id, device_info, parent_object_id, parent_friendly
        )
        self._attr_native_value: int = 0

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = (self._attr_native_value or 0) + 1
        self.async_write_ha_state()
