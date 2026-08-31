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
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
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
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import Event, HomeAssistant, callback, split_entity_id
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_DONE_KEY,
    CONF_ENTITY_ID,
    CONF_INITIAL_OFFSET,
    CONF_MAX_POWER_KW,
    DEFAULT_MAX_POWER_KW,
    DOMAIN,
    MIN_ELAPSED_SECONDS,
    MIN_SPIKE_KWH,
    SIGNAL_SPIKE_CORRECTED,
    STORE_APPLIED_OFFSET,
    STORE_LAST_SOURCE,
    STORE_LAST_SOURCE_TS,
    STORE_NATIVE,
    STORE_SUPPRESSED,
    STORE_UNIT,
)

_LOGGER = logging.getLogger(__name__)

_INVALID_STATES = (STATE_UNKNOWN, STATE_UNAVAILABLE, None)

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

    # Resolve the source's device directly. ``async_device_info_to_link_from_entity``
    # is deprecated and now always returns None: a DeviceInfo carrying another
    # device's identifiers implicitly adopted that device into our config entry,
    # which a single-config-entry device cannot represent. The supported way to
    # put an entity on someone else's device is to assign ``device_entry``, which
    # the entity platform honours when ``device_info`` is None.
    device_entry = async_entity_id_to_device(hass, entity_id)
    parent_friendly = _parent_friendly_name(hass, entity_id)

    async_add_entities(
        [
            CleanFilterSensor(entry, entity_id, device_entry, parent_friendly),
            LastSpikeTimeSensor(entry, entity_id, device_entry, parent_friendly),
            LastSpikeSizeSensor(entry, entity_id, device_entry, parent_friendly),
            TotalCorrectedSensor(entry, entity_id, device_entry, parent_friendly),
            SpikeCountSensor(entry, entity_id, device_entry, parent_friendly),
        ]
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
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"{parent_friendly} (Clean)",
        "source": "recorder",
        "statistic_id": target_id,
        "unit_class": "energy",
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


class CleanFilterSensor(RestoreEntity, SensorEntity):
    """A ``total_increasing`` energy sensor that mirrors the source, sans spikes.

    Real flaky meters typically spike *permanently*: the meter reports a
    bogus value once, then keeps reporting from that bogus value forever
    (monotonically increasing on top of it). Some spike *transiently* and
    fall back to the true reading a few minutes later. Both have to work.

    The filter is therefore an **increment accumulator**, not a subtraction:

    * We hold our own running total (``_native``) and remember where the
      source was when we last looked (``_last_source``).
    * Every plausible increase in the source is added to our total. An
      increase that implies an impossible power draw is dropped on the floor
      — our total simply doesn't advance for it.
    * If the source falls (a transient spike reverting, or a genuine meter
      reset), we re-anchor to the new source level and keep our total where
      it is. We never counted the bogus energy, so there is nothing to undo.

    Because the total only ever moves by increments we chose to accept, it
    cannot go negative and cannot double-count — which matters, because the
    recorder discards *every* statistics row for a ``total_increasing``
    sensor whose state is negative, leaving the Energy Dashboard blank.

    The state is restored across restarts. Rebuilding it from the config
    entry alone would re-apply the entry's one-time ``initial_offset`` to a
    source that has since moved on.
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
        device_entry: DeviceEntry | None,
        parent_friendly: str,
    ) -> None:
        """Initialise the clean filter sensor."""
        self._entry = entry
        self._source_id = source_id
        self._parent_friendly = parent_friendly
        self._attr_unique_id = f"{entry.entry_id}_clean"
        self._attr_name = f"{parent_friendly} (Clean)"
        if device_entry:
            self.device_entry = device_entry
        self._native: float | None = None
        self._last_source: float | None = None
        self._last_source_time: datetime | None = None
        # Cumulative energy we have refused to count, for diagnostics.
        self._suppressed: float = 0.0
        self._unit: str = UnitOfEnergy.KILO_WATT_HOUR
        # One-time correction captured at entry-creation time. Without it, a
        # sensor whose spike *triggered* discovery would be seeded from the
        # already-spiked source and mirror the spike one-for-one. It applies
        # only when seeding; from then on the accumulator carries the state.
        self._configured_offset: float = float(
            entry.data.get(CONF_INITIAL_OFFSET, 0.0) or 0.0
        )

    @property
    def native_value(self) -> float | None:
        """Return the filtered energy value."""
        return self._native

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the unit of measurement, mirrored from the source."""
        return self._unit

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose internal filter state for debugging."""
        return {
            "source_entity": self._source_id,
            "last_source_value": self._last_source,
            "offset_kwh": round(self._suppressed, 3),
            "last_source_time": (
                self._last_source_time.isoformat() if self._last_source_time else None
            ),
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Persist the accumulator so a restart doesn't rewind the filter."""
        return RestoredExtraData(
            {
                STORE_NATIVE: self._native,
                STORE_LAST_SOURCE: self._last_source,
                STORE_LAST_SOURCE_TS: (
                    self._last_source_time.isoformat()
                    if self._last_source_time
                    else None
                ),
                STORE_SUPPRESSED: self._suppressed,
                STORE_UNIT: self._unit,
                STORE_APPLIED_OFFSET: self._configured_offset,
            }
        )

    async def async_added_to_hass(self) -> None:
        """Restore prior state (or seed from the source) and start tracking."""
        await super().async_added_to_hass()

        restored = await self.async_get_last_extra_data()
        if not self._restore_previous_state(restored.as_dict() if restored else None):
            self._seed_from_source()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source_id], self._handle_source_change
            )
        )

        if not self._entry.data.get(BACKFILL_DONE_KEY):
            # Use our real entity_id — it is only assigned once the platform
            # has added us, and guessing it from the source's object_id writes
            # statistics under a statistic_id that may not exist.
            self.hass.async_create_task(
                _backfill_history(
                    self.hass,
                    self._entry,
                    self._source_id,
                    self.entity_id,
                    self._parent_friendly,
                )
            )

    def _restore_previous_state(self, stored: dict | None) -> bool:
        """Adopt the accumulator state saved before the last restart.

        Returns False when there is nothing usable to restore, or when the
        entry's ``initial_offset`` has been edited since — that edit is the
        user asking for a re-seed, so the stored total is deliberately
        discarded.
        """
        if stored is None:
            return False
        if stored.get(STORE_APPLIED_OFFSET) != self._configured_offset:
            _LOGGER.info(
                "Clean Energy: initial offset for %s changed to %.3f; "
                "re-seeding the clean value from the source.",
                self._source_id,
                self._configured_offset,
            )
            return False

        native = stored.get(STORE_NATIVE)
        last_source = stored.get(STORE_LAST_SOURCE)
        ts = stored.get(STORE_LAST_SOURCE_TS)
        last_source_time = dt_util.parse_datetime(ts) if ts else None
        if native is None or last_source is None or last_source_time is None:
            # A partial payload is no use as an anchor — fall back to seeding
            # rather than half-restoring.
            return False

        try:
            self._native = max(0.0, float(native))
            self._last_source = float(last_source)
            self._suppressed = float(stored.get(STORE_SUPPRESSED) or 0.0)
        except (ValueError, TypeError):
            return False
        self._last_source_time = last_source_time
        self._unit = stored.get(STORE_UNIT) or UnitOfEnergy.KILO_WATT_HOUR
        return True

    def _seed_from_source(self) -> None:
        """Adopt the source's current reading as our starting point."""
        state = self.hass.states.get(self._source_id)
        if state is None or state.state in _INVALID_STATES:
            # Stay unknown; the first real reading will seed us.
            return
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return
        self._seed(
            value,
            state.attributes.get("unit_of_measurement", UnitOfEnergy.KILO_WATT_HOUR),
            state.last_changed or dt_util.utcnow(),
        )

    def _seed(self, value: float, unit: str, when: datetime) -> None:
        """Seed the accumulator, applying (and validating) the initial offset."""
        offset = self._configured_offset
        if offset > value:
            # The offset was captured while the source was spiked, but the
            # source now reads *below* it — the spike reverted, so there is no
            # longer anything in the source for the offset to cancel out.
            # Applying it anyway drives us negative, and the recorder throws
            # away every statistics row for a negative total_increasing
            # sensor, which is what leaves the Energy Dashboard empty.
            _LOGGER.warning(
                "Clean Energy: stored initial offset of %.3f %s for %s exceeds the "
                "source's current value of %.3f %s — the spike it was meant to "
                "cancel is no longer present. Discarding the offset and seeding "
                "from the source.",
                offset,
                unit,
                self._source_id,
                value,
                unit,
            )
            offset = 0.0
            self._clear_configured_offset()

        self._native = max(0.0, value - offset)
        self._last_source = value
        self._last_source_time = when
        self._unit = unit

    def _clear_configured_offset(self) -> None:
        """Drop a stale initial offset from the config entry, permanently."""
        self._configured_offset = 0.0
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_INITIAL_OFFSET: 0.0},
        )

    def _rescale(self, unit: str) -> None:
        """Convert our running total if the source changed units under us."""
        if unit == self._unit:
            return
        old_factor = _TO_KWH.get(self._unit)
        new_factor = _TO_KWH.get(unit)
        if old_factor is not None and new_factor:
            scale = old_factor / new_factor
            if self._native is not None:
                self._native *= scale
            if self._last_source is not None:
                self._last_source *= scale
        self._unit = unit

    @callback
    def _handle_source_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _INVALID_STATES:
            return
        try:
            new_val = float(new_state.state)
        except (ValueError, TypeError):
            return

        unit = new_state.attributes.get("unit_of_measurement", "kWh")
        factor = _TO_KWH.get(unit)
        if factor is None:
            return

        now = new_state.last_changed or dt_util.utcnow()
        self._rescale(unit)

        # First reading: just seed.
        if (
            self._native is None
            or self._last_source is None
            or self._last_source_time is None
        ):
            self._seed(new_val, unit, now)
            self.async_write_ha_state()
            return

        diff = new_val - self._last_source

        if diff < 0:
            # The source fell. Either a transient spike reverting, or a
            # genuine total_increasing reset (e.g. a manual Z-Wave meter
            # reset). Our total only ever moved by increments we accepted, so
            # there is nothing to unwind either way: re-anchor to the new
            # source level and hold our total steady.
            _LOGGER.info(
                "Clean Energy: source %s fell (%.3f -> %.3f %s); "
                "re-anchoring, clean total stays at %.3f %s.",
                self._source_id,
                self._last_source,
                new_val,
                unit,
                self._native,
                unit,
            )
            self._last_source = new_val
            self._last_source_time = now
            self.async_write_ha_state()
            return

        if diff == 0:
            return

        jump_kwh = diff * factor
        elapsed = max(
            (now - self._last_source_time).total_seconds(), MIN_ELAPSED_SECONDS
        )
        implied_kw = jump_kwh / (elapsed / 3600.0)
        threshold_kw = _hub_max_power_kw(self.hass)

        if jump_kwh >= MIN_SPIKE_KWH and implied_kw > threshold_kw:
            # Spike: refuse the increment. Our total stays put, and future
            # increments on top of the (now elevated) source still pass
            # through, because we re-anchor _last_source to the spiked value.
            _LOGGER.warning(
                "Clean Energy: filtered spike on %s "
                "(%.3f -> %.3f %s, implied %.1f kW > %.0f kW limit); "
                "not counting %.3f kWh (total suppressed now %.3f kWh).",
                self._source_id,
                self._last_source,
                new_val,
                unit,
                implied_kw,
                threshold_kw,
                jump_kwh,
                self._suppressed + jump_kwh,
            )
            self._suppressed += jump_kwh
            self._last_source = new_val
            self._last_source_time = now
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_SPIKE_CORRECTED}_{self._source_id}",
                jump_kwh,
                now,
            )
            self.async_write_ha_state()
            return

        # Normal reading: count it.
        self._native += diff
        self._last_source = new_val
        self._last_source_time = now
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Diagnostic sensors
# ---------------------------------------------------------------------------


class CleanEnergyDiagnosticSensor(RestoreEntity, SensorEntity):
    """Base class for Clean Energy diagnostic sensors.

    These are running tallies of what the filter has done, so they restore
    across restarts — otherwise the spike count and the energy-removed total
    silently reset to zero every time Home Assistant is restarted.
    """

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _name_suffix: str = ""
    _id_suffix: str = ""

    def __init__(
        self,
        entry: ConfigEntry,
        monitored_entity_id: str,
        device_entry: DeviceEntry | None,
        parent_friendly: str,
    ) -> None:
        """Initialise the diagnostic sensor."""
        self._monitored_entity_id = monitored_entity_id
        self._attr_unique_id = f"{entry.entry_id}_{self._id_suffix}"
        self._attr_name = f"{parent_friendly} {self._name_suffix}"
        if device_entry:
            self.device_entry = device_entry

    async def async_added_to_hass(self) -> None:
        """Restore the previous tally and subscribe to spike signals."""
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            if last.state not in _INVALID_STATES:
                self._restore_native_value(last.state)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_SPIKE_CORRECTED}_{self._monitored_entity_id}",
                self._handle_spike,
            )
        )

    def _restore_native_value(self, state: str) -> None:
        """Adopt a previously recorded state string. Best effort."""
        raise NotImplementedError

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        raise NotImplementedError


class LastSpikeTimeSensor(CleanEnergyDiagnosticSensor):
    """Records the timestamp of the most recent filtered spike."""

    _name_suffix = "Last Spike"
    _id_suffix = "last_spike"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry, entity_id, device_entry, parent_friendly):
        """Initialise with a ``None`` initial value."""
        super().__init__(entry, entity_id, device_entry, parent_friendly)
        self._attr_native_value: datetime | None = None

    def _restore_native_value(self, state: str) -> None:
        self._attr_native_value = dt_util.parse_datetime(state)

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

    def __init__(self, entry, entity_id, device_entry, parent_friendly):
        """Initialise with a ``None`` initial value."""
        super().__init__(entry, entity_id, device_entry, parent_friendly)
        self._attr_native_value: float | None = None

    def _restore_native_value(self, state: str) -> None:
        try:
            self._attr_native_value = float(state)
        except (ValueError, TypeError):
            self._attr_native_value = None

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

    def __init__(self, entry, entity_id, device_entry, parent_friendly):
        """Initialise the running total at zero."""
        super().__init__(entry, entity_id, device_entry, parent_friendly)
        self._attr_native_value: float = 0.0

    def _restore_native_value(self, state: str) -> None:
        try:
            self._attr_native_value = max(0.0, float(state))
        except (ValueError, TypeError):
            self._attr_native_value = 0.0

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

    def __init__(self, entry, entity_id, device_entry, parent_friendly):
        """Initialise the counter at zero."""
        super().__init__(entry, entity_id, device_entry, parent_friendly)
        self._attr_native_value: int = 0

    def _restore_native_value(self, state: str) -> None:
        try:
            self._attr_native_value = max(0, int(float(state)))
        except (ValueError, TypeError):
            self._attr_native_value = 0

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = (self._attr_native_value or 0) + 1
        self.async_write_ha_state()
