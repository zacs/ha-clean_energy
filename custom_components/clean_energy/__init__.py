"""Clean Energy - detect and correct anomalous energy sensor spikes.

Architecture:
- One "hub" background listener watches ALL energy sensors passively.
- When a spike is detected on an un-managed sensor, a discovery flow is created
  so the user can approve monitoring for that specific sensor.
- Only sensors with their own config entry get corrections applied.
- Users can also manually add sensors via the config flow.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry, SOURCE_DISCOVERY
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, UnitOfEnergy
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTITY_ID,
    CONF_MAX_POWER_KW,
    DEFAULT_MAX_POWER_KW,
    DOMAIN,
    MIN_ELAPSED_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Conversion factors to kWh
_TO_KWH: dict[str, float] = {
    UnitOfEnergy.KILO_WATT_HOUR: 1.0,
    UnitOfEnergy.WATT_HOUR: 0.001,
    UnitOfEnergy.MEGA_WATT_HOUR: 1000.0,
    "kWh": 1.0,
    "Wh": 0.001,
    "MWh": 1000.0,
    "GJ": 277.778,
}

ENERGY_UNITS = set(_TO_KWH.keys())


def _is_energy_sensor(state) -> bool:
    """Check if a state object represents a total_increasing energy sensor."""
    if state is None:
        return False
    attrs = state.attributes
    return (
        attrs.get("state_class") == "total_increasing"
        and attrs.get("unit_of_measurement", "") in ENERGY_UNITS
    )


def _get_managed_entity_ids(hass: HomeAssistant) -> set[str]:
    """Return entity_ids that have an approved config entry."""
    managed = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        eid = entry.data.get(CONF_ENTITY_ID)
        if eid:
            managed.add(eid)
    return managed


# ---------------------------------------------------------------------------
# Hub: passive background watcher (one per HA instance)
# ---------------------------------------------------------------------------

class CleanEnergyHub:
    """Passively watches all energy sensors for spikes.

    - For managed sensors (have a config entry): apply statistics correction.
    - For unmanaged sensors: fire a discovery flow so the user can approve.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._unsub: list = []
        # entity_id -> (last_good_value_native, timestamp)
        self._last_readings: dict[str, tuple[float, datetime]] = {}
        # entity_ids we've already fired a discovery for (avoid spamming)
        self._discovered: set[str] = set()

    @property
    def max_power_kw(self) -> float:
        """Global threshold - uses the first config entry's value, or default."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            return entry.options.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW)
        return DEFAULT_MAX_POWER_KW

    def start(self) -> None:
        """Begin listening to all energy sensors."""
        entities = [
            s.entity_id
            for s in self.hass.states.async_all()
            if _is_energy_sensor(s)
        ]

        if not entities:
            _LOGGER.debug("Clean Energy hub: no energy sensors found yet")
            return

        now = dt_util.utcnow()
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable", None):
                try:
                    self._last_readings[entity_id] = (
                        float(state.state),
                        state.last_changed or now,
                    )
                except (ValueError, TypeError):
                    pass

        self._unsub.append(
            async_track_state_change_event(
                self.hass, entities, self._handle_state_change
            )
        )
        _LOGGER.info(
            "Clean Energy hub: watching %d energy sensors passively", len(entities)
        )

    def stop(self) -> None:
        """Stop listening."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Evaluate a state change for spike."""
        entity_id = event.data["entity_id"]
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

        now = new_state.last_changed or dt_util.utcnow()

        prev = self._last_readings.get(entity_id)
        if prev is None:
            self._last_readings[entity_id] = (new_val, now)
            return

        prev_val, prev_time = prev
        jump = new_val - prev_val

        if jump <= 0:
            self._last_readings[entity_id] = (new_val, now)
            return

        jump_kwh = jump * factor
        elapsed = max((now - prev_time).total_seconds(), MIN_ELAPSED_SECONDS)
        implied_power_kw = jump_kwh / (elapsed / 3600.0)

        if implied_power_kw <= self.max_power_kw:
            # Normal reading
            self._last_readings[entity_id] = (new_val, now)
            return

        # --- Spike detected ---
        managed = _get_managed_entity_ids(self.hass)

        if entity_id in managed:
            # Approved sensor: correct statistics
            _LOGGER.warning(
                "Clean Energy: SPIKE on %s: %.3f → %.3f %s over %.0fs "
                "(implied %.1f kW, limit %.0f kW). Correcting by -%.3f kWh.",
                entity_id,
                prev_val,
                new_val,
                unit,
                elapsed,
                implied_power_kw,
                self.max_power_kw,
                jump_kwh,
            )
            self.hass.async_create_task(
                _adjust_statistics(self.hass, entity_id, -jump_kwh)
            )
        else:
            # Unmanaged sensor: offer discovery (once per entity per session)
            if entity_id not in self._discovered:
                self._discovered.add(entity_id)
                _LOGGER.info(
                    "Clean Energy: spike detected on unmanaged sensor %s "
                    "(%.3f → %.3f %s, implied %.1f kW). "
                    "Creating discovery flow.",
                    entity_id,
                    prev_val,
                    new_val,
                    unit,
                    implied_power_kw,
                )
                self.hass.async_create_task(
                    self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": SOURCE_DISCOVERY},
                        data={
                            CONF_ENTITY_ID: entity_id,
                            "spike_from": prev_val,
                            "spike_to": new_val,
                            "spike_unit": unit,
                            "implied_power_kw": round(implied_power_kw, 1),
                            "spike_jump_kwh": round(jump_kwh, 3),
                        },
                    )
                )

        # Do NOT update last_reading - keep pre-spike baseline

    def clear_discovery(self, entity_id: str) -> None:
        """Allow re-discovery if user ignores/dismisses the flow."""
        self._discovered.discard(entity_id)


# ---------------------------------------------------------------------------
# Statistics correction
# ---------------------------------------------------------------------------

async def _adjust_statistics(
    hass: HomeAssistant, entity_id: str, adjustment_kwh: float
) -> None:
    """Adjust the long-term statistics sum for an entity."""
    try:
        await get_instance(hass).async_add_executor_job(
            _do_adjust_statistics, hass, entity_id, adjustment_kwh
        )
        _LOGGER.info(
            "Clean Energy: statistics for %s adjusted by %.3f kWh",
            entity_id,
            adjustment_kwh,
        )
    except Exception:
        _LOGGER.exception(
            "Clean Energy: failed to adjust statistics for %s", entity_id
        )


def _do_adjust_statistics(
    hass: HomeAssistant, entity_id: str, adjustment_kwh: float
) -> None:
    """Call recorder to adjust statistics (runs in executor)."""
    from homeassistant.components.recorder.statistics import adjust_statistics

    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    adjust_statistics(
        get_instance(hass),
        statistic_id=entity_id,
        start_time=now,
        sum_adjustment=adjustment_kwh,
        adjustment_unit_of_measurement="kWh",
    )


# ---------------------------------------------------------------------------
# Entry setup / teardown
# ---------------------------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Clean Energy config entry.

    The first entry loaded also starts the hub (background watcher).
    Per-entity entries just register themselves; the hub handles the rest.
    """
    hub: CleanEnergyHub | None = hass.data.get(DOMAIN, {}).get("hub")

    if hub is None:
        hub = CleanEnergyHub(hass)
        hass.data.setdefault(DOMAIN, {})["hub"] = hub

        # Start after HA is fully loaded so all entities exist
        if hass.is_running:
            hub.start()
        else:
            async def _start_hub(event: Event) -> None:
                hub.start()

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_hub)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    entity_id = entry.data.get(CONF_ENTITY_ID)
    if entity_id:
        _LOGGER.info("Clean Energy: now managing %s", entity_id)

        # If this entry was created from discovery, correct the triggering spike
        pending_kwh = entry.data.get("spike_jump_kwh")
        if pending_kwh and pending_kwh > 0:
            _LOGGER.info(
                "Clean Energy: correcting triggering spike on %s (%.3f kWh)",
                entity_id,
                pending_kwh,
            )
            hass.async_create_task(
                _adjust_statistics(hass, entity_id, -pending_kwh)
            )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]

    if not remaining:
        hub: CleanEnergyHub | None = hass.data.get(DOMAIN, {}).get("hub")
        if hub:
            hub.stop()
        hass.data.pop(DOMAIN, None)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update - restart the hub to pick up new threshold."""
    hub: CleanEnergyHub | None = hass.data.get(DOMAIN, {}).get("hub")
    if hub:
        hub.stop()
        hub.start()
