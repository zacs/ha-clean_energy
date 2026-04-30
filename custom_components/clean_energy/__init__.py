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

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, SOURCE_DISCOVERY
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
    Platform,
    UnitOfEnergy,
)
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTITY_ID,
    CONF_MAX_POWER_KW,
    DEFAULT_MAX_POWER_KW,
    DOMAIN,
    MIN_ELAPSED_SECONDS,
    SERVICE_MONITOR_SENSOR,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

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
        """Begin listening to all energy sensors.

        We subscribe to EVENT_STATE_CHANGED on the bus rather than to a fixed
        list of entity_ids. This way, energy sensors created *after* the hub
        starts (yaml reloads, late-loading integrations, newly added template
        sensors) are picked up automatically without resubscribing.
        """
        # Seed last_readings with currently-known energy sensors so we have a
        # baseline for spike detection from the first observed change.
        now = dt_util.utcnow()
        seeded = 0
        for state in self.hass.states.async_all():
            if not _is_energy_sensor(state):
                continue
            if state.state in ("unknown", "unavailable", None):
                continue
            try:
                self._last_readings[state.entity_id] = (
                    float(state.state),
                    state.last_changed or now,
                )
                seeded += 1
            except (ValueError, TypeError):
                continue

        self._unsub.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._handle_state_change
            )
        )
        _LOGGER.info(
            "Clean Energy hub: watching all energy sensors passively "
            "(seeded %d known at startup)",
            seeded,
        )

    def stop(self) -> None:
        """Stop listening."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Evaluate a state change for spike."""
        new_state = event.data.get("new_state")

        # Filter to total_increasing energy sensors only. Doing this here
        # (rather than at subscription time) means newly created sensors are
        # picked up automatically.
        if not _is_energy_sensor(new_state):
            return

        if new_state.state in ("unknown", "unavailable"):
            return

        entity_id = event.data["entity_id"]

        # Never monitor our own diagnostic sensors. Without this guard, the
        # "total energy corrected" sensor (a TOTAL_INCREASING energy sensor)
        # could itself be flagged for discovery / correction, which would
        # cause feedback loops.
        registry = er.async_get(self.hass)
        registered = registry.async_get(entity_id)
        if registered is not None and registered.platform == DOMAIN:
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
            # Approved sensor: the per-entity CleanFilterSensor handles
            # filtering at the entity level. The hub does nothing here
            # — we deliberately do NOT touch the source's LTS, which keeps
            # the integration non-destructive.
            return
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
                        },
                    )
                )

        # Do NOT update last_reading - keep pre-spike baseline

    def clear_discovery(self, entity_id: str) -> None:
        """Allow re-discovery if user ignores/dismisses the flow."""
        self._discovered.discard(entity_id)


# ---------------------------------------------------------------------------
# Statistics correction (deferred + persisted)
#
# `recorder.adjust_statistics` runs a single SQL UPDATE of the form
#    UPDATE statistics SET sum = sum + adj WHERE start_ts >= start_time_ts
# It does NOT persist the adjustment for rows written in the future. So if we
# call it the moment we detect a spike, the 5-minute short-term row that
# *contains* the spike has not been compiled yet (compilation runs at the
# next 5-min boundary), so the UPDATE matches no rows, and when the row is
# later inserted it carries the spike value untouched.
#
# Fortunately, hourly rows are not a separate problem: the recorder's hourly
# compilation derives `sum` directly from the *last* short-term row in the
# hour (see _compile_hourly_statistics in recorder/statistics.py). So if we
# fix the spike's short-term row before the top-of-next-hour compilation
# runs, the hourly row is born correct — no extra hour-long wait needed.
#
# So the fix is to defer the adjustment only until just after the next
# 5-minute boundary (when the spike's short-term row is guaranteed to
# exist), and pass `start_time = floor(spike_time, 5min)` so the spike's
# own row is included in the UPDATE while pre-spike rows in the same hour
# are not (otherwise the diff between rows would still show a spike).
# Worst-case visible-error window in the Energy Dashboard: ~5½ minutes.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Statistics correction
#
# (Removed.) We used to issue `recorder.adjust_statistics` calls to patch
# spikes out of the source sensor's Long-Term Statistics, but the recorder's
# own `total_increasing` reset detection treats every spike-and-revert as a
# completed cycle, baking the spike's value into the cumulative sum on the
# next compilation pass and undoing any adjustment we make.
#
# Instead, the per-entity setup (see sensor.py) now creates a
# CleanFilterSensor that mirrors the source's value with spikes held flat,
# and copies the source's existing hourly LTS history into it. The Energy
# Dashboard should be pointed at the clean entity. The source entity is
# left completely untouched.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Service: monitor a sensor on demand (no need to wait for a spike)
# ---------------------------------------------------------------------------

MONITOR_SENSOR_SCHEMA = vol.Schema(
    {vol.Required(CONF_ENTITY_ID): cv.entity_id}
)


async def _async_handle_monitor_sensor(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Service: register an energy sensor for monitoring without waiting for a spike.

    Useful when you already know a sensor is flaky but it spikes too rarely
    for the passive watcher to discover quickly.
    """
    entity_id: str = call.data[CONF_ENTITY_ID]

    # Validate the entity is a total_increasing energy sensor.
    state = hass.states.get(entity_id)
    if state is None:
        raise ServiceValidationError(
            f"Entity {entity_id} does not exist"
        )
    if not _is_energy_sensor(state):
        raise ServiceValidationError(
            f"Entity {entity_id} is not a total_increasing energy sensor "
            f"(state_class={state.attributes.get('state_class')!r}, "
            f"unit={state.attributes.get('unit_of_measurement')!r})"
        )

    # Don't allow monitoring our own diagnostic entities (would cause loops).
    registry = er.async_get(hass)
    registered = registry.async_get(entity_id)
    if registered is not None and registered.platform == DOMAIN:
        raise ServiceValidationError(
            f"Entity {entity_id} is a Clean Energy diagnostic sensor and "
            "cannot be monitored"
        )

    # Already monitored?
    if entity_id in _get_managed_entity_ids(hass):
        raise ServiceValidationError(
            f"Entity {entity_id} is already being monitored by Clean Energy"
        )

    # Trigger a user-source flow with the entity_id pre-filled. The flow's
    # _async_step_add_sensor will validate again, set the unique_id, and
    # create the config entry.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_ENTITY_ID: entity_id},
    )

    if result.get("type") == "create_entry":
        _LOGGER.info(
            "Clean Energy: now monitoring %s (added via service)", entity_id
        )
        return {"entity_id": entity_id, "status": "added"}

    # Flow returned a form or aborted - surface a useful error.
    reason = result.get("reason") or result.get("errors") or result.get("type")
    raise ServiceValidationError(
        f"Could not start monitoring {entity_id}: {reason}"
    )


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_MONITOR_SENSOR):
        return

    async def _service(call: ServiceCall) -> ServiceResponse:
        return await _async_handle_monitor_sensor(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_MONITOR_SENSOR,
        _service,
        schema=MONITOR_SENSOR_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
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

        # Register the public service alongside the hub.
        _async_register_services(hass)

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

        # Forward the sensor platform: this creates the CleanFilterSensor
        # (the user-facing replacement for the source) and the diagnostic
        # sensors. Backfill of historical LTS happens inside that platform's
        # async_setup_entry.
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entity_id = entry.data.get(CONF_ENTITY_ID)
    if entity_id:
        if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
            return False

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
        # Last entry gone - tear down our services too.
        if hass.services.has_service(DOMAIN, SERVICE_MONITOR_SENSOR):
            hass.services.async_remove(DOMAIN, SERVICE_MONITOR_SENSOR)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update - restart the hub to pick up new threshold."""
    hub: CleanEnergyHub | None = hass.data.get(DOMAIN, {}).get("hub")
    if hub:
        hub.stop()
        hub.start()
