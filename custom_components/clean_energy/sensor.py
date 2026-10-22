"""Clean Energy entities.

For each monitored source sensor we expose:

* ``CleanFilterSensor`` — a parallel ``total_increasing`` energy entity that
  counts the source's real consumption and drops impossible jumps. The Energy
  Dashboard should be pointed at this entity instead of the original. The
  source entity is left completely untouched (no LTS adjustments, no history
  rewrites), so the integration is non-destructive.

* Diagnostic sensors (``Last Spike``, ``Last Spike Size``, ``Energy
  Removed``, ``Spike Count``) — these listen to a dispatcher signal that the
  filter sensor fires whenever it suppresses a spike.

A new clean sensor derives both its starting value and its long-term
statistics from the source's history, with spikes removed (see history.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.recorder import is_entity_recorded
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import ServiceValidationError
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

from . import history
from .const import (
    CONF_ENTITY_ID,
    CONF_INITIAL_OFFSET,
    HISTORY_DONE_KEY,
    LEGACY_BACKFILL_DONE_KEY,
    RESEED_TOLERANCE_KWH,
    SIGNAL_SPIKE_CORRECTED,
    STORE_LAST_SOURCE,
    STORE_LAST_SOURCE_TS,
    STORE_NATIVE,
    STORE_SUPPRESSED,
    STORE_UNIT,
    CleanEnergyFeature,
)
from .entries import max_power_kw
from .spike import (
    CleanHistory,
    confirmed_at,
    continue_history,
    convert,
    derive_clean_history,
    implied_kw,
    is_spike,
    reading,
    to_kwh,
)

_LOGGER = logging.getLogger(__name__)

_INVALID_STATES = (STATE_UNKNOWN, STATE_UNAVAILABLE, None)


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

    # A device belongs to a single config entry, so a helper links its
    # entities to the source's device rather than joining that device. The
    # entity platform honours ``device_entry`` when ``device_info`` is None.
    device_entry = async_entity_id_to_device(hass, entity_id)
    parent_friendly = _parent_friendly_name(hass, entity_id)

    clean = CleanFilterSensor(entry, entity_id, device_entry, parent_friendly)
    entry.runtime_data = clean
    async_add_entities(
        [
            clean,
            LastSpikeTimeSensor(entry, entity_id, device_entry, parent_friendly),
            LastSpikeSizeSensor(entry, entity_id, device_entry, parent_friendly),
            TotalCorrectedSensor(entry, entity_id, device_entry, parent_friendly),
            SpikeCountSensor(entry, entity_id, device_entry, parent_friendly),
        ]
    )


# ---------------------------------------------------------------------------
# Filter sensor: a clean parallel of the source
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Rebuild:
    """What a history rebuild found, and what it will do."""

    history: CleanHistory | None
    # The source reading the plan was made against.
    source: float
    unit: str
    at: datetime
    value_before: float | None
    # The running total to adopt; None when the source has no history to
    # derive one from, in which case discovery's recorded offset applies.
    value_after: float | None

    @property
    def changes_value(self) -> bool:
        """Whether adopting the plan moves the running total."""
        if self.value_after is None:
            return self.value_before is None
        if self.value_before is None:
            return True
        delta = to_kwh(abs(self.value_after - self.value_before), self.unit)
        return delta >= RESEED_TOLERANCE_KWH

    def summary(self, entity_id: str, source_id: str) -> dict[str, Any]:
        """Describe the rebuild for a service response."""
        hist = self.history
        corrected = self.changes_value and self.value_before is not None
        after = self.value_after if corrected else self.value_before
        return {
            "entity_id": entity_id,
            "source": source_id,
            "unit": self.unit,
            "history_hours": len(hist.rows) if hist else 0,
            "spike_hours": [h.isoformat() for h in hist.spike_hours] if hist else [],
            "energy_removed": round(hist.suppressed, 3) if hist else 0.0,
            "value_before": self.value_before,
            "value_after": None if after is None else round(after, 3),
            "value_corrected": corrected,
        }


class CleanFilterSensor(RestoreEntity, SensorEntity):
    """A ``total_increasing`` energy sensor that counts the source, sans spikes.

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
    cannot go negative and cannot double-count.

    The state is restored across restarts. Rebuilding it from the config
    entry alone would re-apply a one-time offset to a source that has since
    moved on.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3
    _attr_supported_features = CleanEnergyFeature.REBUILD_HISTORY

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
        # Suggest sensor.<source>_clean. Home Assistant otherwise prefixes the
        # device name, and an existing entity keeps whatever id it already has.
        self.entity_id = f"sensor.{split_entity_id(source_id)[1]}_clean"
        if device_entry:
            self.device_entry = device_entry
        self._native: float | None = None
        self._last_source: float | None = None
        self._last_source_time: datetime | None = None
        # Cumulative energy we have refused to count, for diagnostics.
        self._suppressed: float = 0.0
        self._unit: str = UnitOfEnergy.KILO_WATT_HOUR
        # Set while a new sensor waits for its history-derived starting value,
        # so a reading arriving meanwhile doesn't seed it from a spiked source.
        self._awaiting_seed = False
        self._rebuild_task: asyncio.Task[None] | None = None

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
            "max_power_kw": max_power_kw(self.hass, self._entry),
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
            }
        )

    async def async_added_to_hass(self) -> None:
        """Restore prior state (or seed) and start tracking the source."""
        await super().async_added_to_hass()

        restored = await self.async_get_last_extra_data()
        restored_ok = self._restore_previous_state(
            restored.as_dict() if restored else None
        )

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source_id], self._handle_source_change
            )
        )
        self.async_on_remove(self._cancel_rebuild)

        data = self._entry.data
        if data.get(HISTORY_DONE_KEY) or data.get(LEGACY_BACKFILL_DONE_KEY):
            # Up and running, or created before history was derived; the
            # latter's statistics are only rewritten when the user asks.
            if not restored_ok:
                self._seed_from_source()
            return

        # A new sensor (or one whose first rebuild was interrupted): derive the
        # starting value and statistics from the source's history.
        self._awaiting_seed = not restored_ok
        self._start_rebuild()

    def _restore_previous_state(self, stored: dict | None) -> bool:
        """Adopt the accumulator state saved before the last restart."""
        if stored is None:
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
        except ValueError, TypeError:
            return False
        self._last_source_time = last_source_time
        self._unit = stored.get(STORE_UNIT) or UnitOfEnergy.KILO_WATT_HOUR
        return True

    # -- Seeding without history ---------------------------------------------

    def _seed_from_source(self) -> None:
        """Adopt the source's current reading as our starting point."""
        state = self.hass.states.get(self._source_id)
        if (current := reading(state)) is None:
            # Stay unknown; the first real reading will seed us.
            return
        assert state is not None  # reading() returned a value
        self._seed(*current, state.last_changed)

    def _seed(self, value: float, unit: str, when: datetime) -> None:
        """Seed from a reading, less the offset discovery recorded."""
        offset = float(self._entry.data.get(CONF_INITIAL_OFFSET, 0.0) or 0.0)
        if offset > value:
            # The offset was captured while the source was spiked, but the
            # source now reads *below* it — the spike reverted, so there is no
            # longer anything in the source for the offset to cancel out.
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
            self.hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, CONF_INITIAL_OFFSET: 0.0}
            )
        self._set_running_total(max(0.0, value - offset), value, unit, when)

    def _set_running_total(
        self, native: float, source: float, unit: str, when: datetime
    ) -> None:
        self._native = native
        self._last_source = source
        self._last_source_time = when
        self._unit = unit
        self._awaiting_seed = False

    # -- History rebuild -----------------------------------------------------

    @callback
    def _start_rebuild(self) -> None:
        self._rebuild_task = self._entry.async_create_background_task(
            self.hass,
            self._async_first_rebuild(),
            name=f"clean_energy rebuild {self._source_id}",
        )

    @callback
    def _cancel_rebuild(self) -> None:
        if self._rebuild_task is not None and not self._rebuild_task.done():
            self._rebuild_task.cancel()

    def _rebuilding(self) -> bool:
        return self._rebuild_task is not None and not self._rebuild_task.done()

    async def async_rebuild_history(
        self, offset: float | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Re-derive this sensor's statistics from its source, sans spikes.

        Returns what was found and changed. The statistics themselves are
        replaced in the background over the next few minutes, because the
        recorder has to compile in between (see history.py).
        """
        if self._rebuilding():
            raise ServiceValidationError(
                f"A history rebuild for {self.entity_id} is already in progress"
            )
        plan = await self._async_plan(offset=offset, force=force)
        if plan is None:
            raise ServiceValidationError(
                f"{self._source_id} has no current reading to rebuild from"
            )
        self._apply(plan)
        self._rebuild_task = self._entry.async_create_background_task(
            self.hass,
            self._async_replace_statistics(plan),
            name=f"clean_energy rebuild {self._source_id}",
        )
        return plan.summary(self.entity_id, self._source_id)

    async def _async_first_rebuild(self) -> None:
        """Derive a new sensor's starting value and statistics."""
        try:
            plan = await self._async_plan(offset=None, force=True)
        except Exception:
            _LOGGER.exception(
                "Clean Energy: couldn't read the history of %s; seeding from its "
                "current reading instead",
                self._source_id,
            )
            if self._native is None:
                self._awaiting_seed = False
                self._seed_from_source()
                self.async_write_ha_state()
            return
        if plan is None:
            # No reading yet (its integration is still starting). The first
            # reading to arrive restarts this; see _handle_source_change.
            return
        self._apply(plan)
        await self._async_replace_statistics(plan)

    async def _async_plan(
        self, *, offset: float | None, force: bool
    ) -> _Rebuild | None:
        """Fetch the source's history and work out the clean running total.

        Returns None if the source has no reading to work from.
        """
        if (current := reading(self.hass.states.get(self._source_id))) is None:
            return None
        unit = current[1]
        limit = max_power_kw(self.hass, self._entry)

        hist = None
        if is_entity_recorded(self.hass, self._source_id):
            rows = await history.async_source_hours(self.hass, self._source_id, unit)
            hist = derive_clean_history(rows, unit, limit)

        if (
            hist is not None
            and not force
            and await history.async_has_hours_before(
                self.hass, self.entity_id, hist.rows[0]["start"]
            )
        ):
            raise ServiceValidationError(
                f"{self.entity_id} has statistics older than any recorded for "
                f"{self._source_id}; rebuilding would discard them. Pass force: "
                "true to rebuild anyway."
            )

        # Read the source again: a reading may have arrived during the fetch.
        # Keep working in the unit the history was fetched in.
        state = self.hass.states.get(self._source_id)
        if (current := reading(state)) is None:
            return None
        assert state is not None  # reading() returned a value
        value = convert(current[0], current[1], unit)
        now = state.last_changed

        if offset is not None:
            target: float | None = max(0.0, value - offset)
        elif hist is not None:
            target, _ = continue_history(hist, value, now, unit, limit)
        else:
            target = None

        before = self._native
        if before is not None and self._unit != unit:
            before = convert(before, self._unit, unit)
        return _Rebuild(
            history=hist,
            source=value,
            unit=unit,
            at=now,
            value_before=None if before is None else round(before, 3),
            value_after=target,
        )

    def _apply(self, plan: _Rebuild) -> None:
        """Adopt the planned running total, if it changes anything."""
        if not plan.changes_value:
            return
        if plan.value_after is None:
            # No history to derive from: fall back to discovery's offset.
            self._seed(plan.source, plan.unit, plan.at)
        else:
            self._set_running_total(plan.value_after, plan.source, plan.unit, plan.at)
        self.async_write_ha_state()

    async def _async_replace_statistics(self, plan: _Rebuild) -> None:
        """Swap our statistics for the derived ones (see history.py)."""
        entity_id = self.entity_id
        if plan.history is None or not is_entity_recorded(self.hass, entity_id):
            self._mark_history_done()
            return

        if plan.changes_value:
            # Step 1: let the window holding the new value compile first.
            window = history.five_minute_floor(dt_util.utcnow())
            if not await history.async_wait_for_compile(self.hass, entity_id, window):
                self._warn_compile_timeout()
                return

        # Step 2: replace. Refetch, in case an hour closed while waiting.
        await history.async_wait_out_compile_boundary(self.hass)
        limit = max_power_kw(self.hass, self._entry)
        rows = await history.async_source_hours(self.hass, self._source_id, plan.unit)
        hist = derive_clean_history(rows, plan.unit, limit) or plan.history
        await history.async_wait_out_compile_boundary(self.hass)
        cleared_at = dt_util.utcnow()
        history.async_replace_statistics(
            self.hass,
            entity_id,
            f"{self._parent_friendly} (Clean)",
            plan.unit,
            hist.rows,
        )

        # Step 3: once the recorder restarts the sum at 0, lift it onto history.
        window = history.five_minute_floor(cleared_at)
        if not await history.async_wait_for_compile(self.hass, entity_id, window):
            self._warn_compile_timeout()
            return
        history.async_shift_statistics(
            self.hass, entity_id, window, hist.total, plan.unit
        )
        self._mark_history_done()
        _LOGGER.info(
            "Clean Energy: rebuilt %s from %d hours of %s, removing %.3f %s across "
            "%d spike hour(s)",
            entity_id,
            len(hist.rows),
            self._source_id,
            hist.suppressed,
            plan.unit,
            len(hist.spike_hours),
        )

    def _warn_compile_timeout(self) -> None:
        _LOGGER.warning(
            "Clean Energy: the recorder didn't compile statistics for %s in time; "
            "its history will be rebuilt after the next restart",
            self.entity_id,
        )

    def _mark_history_done(self) -> None:
        if not self._entry.data.get(HISTORY_DONE_KEY):
            self.hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, HISTORY_DONE_KEY: True}
            )

    # -- Live filtering ------------------------------------------------------

    def _rescale(self, unit: str) -> None:
        """Convert our running total if the source changed units under us."""
        if unit == self._unit:
            return
        if self._native is not None:
            self._native = convert(self._native, self._unit, unit)
        if self._last_source is not None:
            self._last_source = convert(self._last_source, self._unit, unit)
        self._unit = unit

    @callback
    def _handle_source_change(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if (current := reading(new_state)) is None:
            return
        assert new_state is not None  # reading() returned a value
        value, unit = current
        now = new_state.last_changed

        if (
            self._native is None
            or self._last_source is None
            or self._last_source_time is None
        ):
            if self._awaiting_seed:
                # The first rebuild seeds from the latest reading. If it has
                # already given up for lack of one, this reading restarts it.
                if not self._rebuilding():
                    self._start_rebuild()
                return
            self._seed(value, unit, now)
            self.async_write_ha_state()
            return

        self._rescale(unit)
        increase = value - self._last_source

        if increase < 0:
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
                value,
                unit,
                self._native,
                unit,
            )
            self._last_source = value
            self._last_source_time = now
            self.async_write_ha_state()
            return

        if increase == 0:
            return

        since = confirmed_at(
            event.data["old_state"], self._last_source, unit, self._last_source_time
        )
        elapsed = (now - since).total_seconds()
        jump_kwh = to_kwh(increase, unit)
        limit = max_power_kw(self.hass, self._entry)

        if is_spike(jump_kwh, elapsed, limit):
            # Spike: refuse the increment. Our total stays put, and future
            # increments on top of the (now elevated) source still pass
            # through, because we re-anchor _last_source to the spiked value.
            _LOGGER.warning(
                "Clean Energy: filtered spike on %s "
                "(%.3f -> %.3f %s, implied %.1f kW > %.1f kW limit); "
                "not counting %.3f kWh (total suppressed now %.3f kWh).",
                self._source_id,
                self._last_source,
                value,
                unit,
                implied_kw(jump_kwh, elapsed),
                limit,
                jump_kwh,
                self._suppressed + jump_kwh,
            )
            self._suppressed += jump_kwh
            self._last_source = value
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
        self._native += increase
        self._last_source = value
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
        self.entity_id = (
            f"sensor.{split_entity_id(monitored_entity_id)[1]}_{self._id_suffix}"
        )
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
        except ValueError, TypeError:
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
        except ValueError, TypeError:
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
        except ValueError, TypeError:
            self._attr_native_value = 0

    @callback
    def _handle_spike(self, spike_kwh: float, timestamp: datetime) -> None:
        self._attr_native_value = (self._attr_native_value or 0) + 1
        self.async_write_ha_state()
