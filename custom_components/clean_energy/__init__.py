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

import voluptuous as vol
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import SOURCE_DISCOVERY, ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
    Platform,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device import async_entity_id_to_device_id
from homeassistant.helpers.helper_integration import (
    async_handle_source_entity_changes,
    async_remove_helper_devices,
)
from homeassistant.helpers.service import async_register_platform_entity_service
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_FORCE,
    ATTR_OFFSET,
    CONF_ENTITY_ID,
    DOMAIN,
    HUB_UNIQUE_ID,
    SERVICE_MONITOR_SENSOR,
    SERVICE_REBUILD_HISTORY,
    CleanEnergyFeature,
)
from .entries import managed_entity_ids, max_power_kw
from .spike import (
    confirmed_at,
    implied_kw,
    is_energy_sensor,
    is_spike,
    reading,
    to_kwh,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REBUILD_HISTORY_SCHEMA: dict = {
    vol.Optional(ATTR_OFFSET): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional(ATTR_FORCE, default=False): cv.boolean,
}


# ---------------------------------------------------------------------------
# Hub: passive background watcher (one per HA instance)
# ---------------------------------------------------------------------------


class CleanEnergyHub:
    """Passively watches all energy sensors for spikes.

    Sensors with a monitoring entry are filtered by their own clean sensor;
    for the rest, a spike offers a discovery flow so the user can approve.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the hub for a given Home Assistant instance."""
        self.hass = hass
        self._unsub: list = []
        # entity_id -> (last_good_value_native, timestamp)
        self._last_readings: dict[str, tuple[float, datetime]] = {}
        # entity_ids we've already fired a discovery for (avoid spamming)
        self._discovered: set[str] = set()

    @property
    def max_power_kw(self) -> float:
        """Global threshold, read from the hub entry."""
        return max_power_kw(self.hass)

    def start(self) -> None:
        """Begin listening to all energy sensors.

        We subscribe to EVENT_STATE_CHANGED on the bus rather than to a fixed
        list of entity_ids. This way, energy sensors created *after* the hub
        starts (yaml reloads, late-loading integrations, newly added template
        sensors) are picked up automatically without resubscribing.
        """
        # Seed last_readings with currently-known energy sensors so we have a
        # baseline for spike detection from the first observed change.
        seeded = 0
        for state in self.hass.states.async_all():
            if not is_energy_sensor(state):
                continue
            if (current := reading(state)) is None:
                continue
            self._last_readings[state.entity_id] = (current[0], state.last_changed)
            seeded += 1

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_change)
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
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Evaluate a state change for spike."""
        new_state = event.data["new_state"]

        # Filter to total_increasing energy sensors only. Doing this here
        # (rather than at subscription time) means newly created sensors are
        # picked up automatically.
        if not is_energy_sensor(new_state) or (current := reading(new_state)) is None:
            return
        assert new_state is not None  # is_energy_sensor() checked
        new_val, unit = current
        entity_id = event.data["entity_id"]

        # Never monitor our own diagnostic sensors. Without this guard, the
        # "total energy corrected" sensor (a TOTAL_INCREASING energy sensor)
        # could itself be flagged for discovery / correction, which would
        # cause feedback loops.
        registry = er.async_get(self.hass)
        registered = registry.async_get(entity_id)
        if registered is not None and registered.platform == DOMAIN:
            return

        now = new_state.last_changed

        prev = self._last_readings.get(entity_id)
        if prev is None:
            self._last_readings[entity_id] = (new_val, now)
            return

        prev_val, prev_time = prev
        jump = new_val - prev_val

        if jump <= 0:
            self._last_readings[entity_id] = (new_val, now)
            return

        since = confirmed_at(event.data["old_state"], prev_val, unit, prev_time)
        elapsed = (now - since).total_seconds()
        jump_kwh = to_kwh(jump, unit)
        if not is_spike(jump_kwh, elapsed, self.max_power_kw):
            self._last_readings[entity_id] = (new_val, now)
            return

        # --- Spike detected ---
        if entity_id in managed_entity_ids(self.hass):
            # Approved sensor: its clean sensor filters it. The hub does
            # nothing — we deliberately do NOT touch the source's LTS, which
            # keeps the integration non-destructive.
            return
        # Unmanaged sensor: offer discovery (once per entity per session)
        if entity_id not in self._discovered:
            self._discovered.add(entity_id)
            implied = implied_kw(jump_kwh, elapsed)
            _LOGGER.info(
                "Clean Energy: spike detected on unmanaged sensor %s "
                "(%.3f → %.3f %s, implied %.1f kW). "
                "Creating discovery flow.",
                entity_id,
                prev_val,
                new_val,
                unit,
                implied,
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
                        "implied_power_kw": round(implied, 1),
                    },
                )
            )

        # Do NOT update last_reading - keep pre-spike baseline

    def clear_discovery(self, entity_id: str) -> None:
        """Allow re-discovery if user ignores/dismisses the flow."""
        self._discovered.discard(entity_id)


# ---------------------------------------------------------------------------
# Service: monitor a sensor on demand (no need to wait for a spike)
# ---------------------------------------------------------------------------

MONITOR_SENSOR_SCHEMA = vol.Schema({vol.Required(CONF_ENTITY_ID): cv.entity_id})


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
        raise ServiceValidationError(f"Entity {entity_id} does not exist")
    if not is_energy_sensor(state):
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
    if entity_id in managed_entity_ids(hass):
        raise ServiceValidationError(
            f"Entity {entity_id} is already being monitored by Clean Energy"
        )

    # Trigger the dedicated service-source flow with the entity_id pre-filled.
    # The flow's ``async_step_monitor_service`` validates, sets the unique_id,
    # and creates the per-sensor config entry.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "monitor_service"},
        data={CONF_ENTITY_ID: entity_id},
    )

    if result.get("type") == "create_entry":
        _LOGGER.info("Clean Energy: now monitoring %s (added via service)", entity_id)
        return {"entity_id": entity_id, "status": "added"}

    # Flow returned a form or aborted - surface a useful error.
    reason = result.get("reason") or result.get("errors") or result.get("type")
    raise ServiceValidationError(f"Could not start monitoring {entity_id}: {reason}")


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
# Service: rebuild a clean sensor's history
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the entity services."""
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_REBUILD_HISTORY,
        entity_domain=SENSOR_DOMAIN,
        schema=REBUILD_HISTORY_SCHEMA,
        func="async_rebuild_history",
        required_features=[CleanEnergyFeature.REBUILD_HISTORY],
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


# ---------------------------------------------------------------------------
# Entry setup / teardown
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Clean Energy config entry.

    The first entry loaded also starts the hub (background watcher).
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

    if entry.unique_id == HUB_UNIQUE_ID:
        # Only the hub's options affect the hub. Sensor entries update their
        # data as they go (history markers), which mustn't restart it.
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    else:
        entry.async_on_unload(entry.add_update_listener(_async_sensor_updated))

    entity_id = entry.data.get(CONF_ENTITY_ID)
    if entity_id:
        _LOGGER.info("Clean Energy: now managing %s", entity_id)
        source_device_id = async_entity_id_to_device_id(hass, entity_id)

        # A device now belongs to a single config entry. Versions before
        # 0.4.0 joined the source's device instead of linking to it, and Home
        # Assistant 2026.8 split each such device in two, leaving this entry
        # owning a duplicate with our entities stranded off it. This removes
        # the duplicate and puts the entities back on the source's device.
        async_remove_helper_devices(
            hass,
            helper_config_entry_id=entry.entry_id,
            source_device_id=source_device_id,
            remove_all_devices=True,
        )

        @callback
        def _set_source_entity_id(new_entity_id: str) -> None:
            """Follow the source when it is renamed."""
            _LOGGER.info(
                "Clean Energy: %s was renamed to %s; following it",
                entry.data[CONF_ENTITY_ID],
                new_entity_id,
            )
            taken = any(
                other.unique_id == new_entity_id
                for other in hass.config_entries.async_entries(DOMAIN)
                if other.entry_id != entry.entry_id
            )
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_ENTITY_ID: new_entity_id},
                unique_id=entry.unique_id if taken else new_entity_id,
            )
            hass.config_entries.async_schedule_reload(entry.entry_id)

        entry.async_on_unload(
            async_handle_source_entity_changes(
                hass,
                helper_config_entry_id=entry.entry_id,
                set_source_entity_id_or_uuid=_set_source_entity_id,
                source_device_id=source_device_id,
                source_entity_id_or_uuid=entity_id,
            )
        )

        # Forward the sensor platform: this creates the CleanFilterSensor
        # (the user-facing replacement for the source) and the diagnostic
        # sensors.
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


async def _async_sensor_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Show a changed per-sensor threshold without waiting for a reading.

    The filter reads the threshold on every reading, so this is display only.
    """
    clean = getattr(entry, "runtime_data", None)
    if clean is not None and clean.hass is not None:
        clean.async_write_ha_state()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update - restart the hub to pick up new threshold."""
    hub: CleanEnergyHub | None = hass.data.get(DOMAIN, {}).get("hub")
    if hub:
        hub.stop()
        hub.start()
