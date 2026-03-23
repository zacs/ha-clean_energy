"""Config flow for Clean Energy.

Supports three entry points:
1. User manually adds the integration (first time) - sets global threshold.
2. User manually adds a specific sensor to monitor.
3. Discovery flow when the hub detects a spike on an unmanaged sensor.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import CONF_ENTITY_ID, CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW, DOMAIN


def _is_energy_sensor(hass, entity_id: str) -> bool:
    """Check if an entity is a total_increasing energy sensor."""
    from . import ENERGY_UNITS

    state = hass.states.get(entity_id)
    if state is None:
        return False
    attrs = state.attributes
    return (
        attrs.get("state_class") == "total_increasing"
        and attrs.get("unit_of_measurement", "") in ENERGY_UNITS
    )


def _managed_entity_ids(hass) -> set[str]:
    """Entity IDs that already have a config entry."""
    managed = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        eid = entry.data.get(CONF_ENTITY_ID)
        if eid:
            managed.add(eid)
    return managed


class CleanEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Clean Energy."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_data: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # User flow: first-time setup or manual sensor addition
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated setup."""
        existing = self.hass.config_entries.async_entries(DOMAIN)
        if not existing:
            # First time: create the "hub" entry with global threshold
            return await self._async_step_setup_hub(user_input)
        # Already set up: let user add a specific sensor
        return await self._async_step_add_sensor(user_input)

    async def _async_step_setup_hub(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Create the initial hub entry."""
        if user_input is not None:
            return self.async_create_entry(
                title="Clean Energy",
                data={},
                options={CONF_MAX_POWER_KW: user_input[CONF_MAX_POWER_KW]},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAX_POWER_KW, default=DEFAULT_MAX_POWER_KW
                    ): vol.Coerce(float),
                }
            ),
            description_placeholders={},
        )

    async def _async_step_add_sensor(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Let user pick an energy sensor to monitor."""
        errors = {}
        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            if entity_id in _managed_entity_ids(self.hass):
                errors[CONF_ENTITY_ID] = "already_monitored"
            elif not _is_energy_sensor(self.hass, entity_id):
                errors[CONF_ENTITY_ID] = "not_energy_sensor"
            else:
                # Use entity_id as unique_id to prevent duplicates
                await self.async_set_unique_id(entity_id)
                self._abort_if_unique_id_configured()

                name = self._friendly_name(entity_id)
                return self.async_create_entry(
                    title=name,
                    data={CONF_ENTITY_ID: entity_id},
                )

        # Build list of available energy sensors (not already managed)
        managed = _managed_entity_ids(self.hass)
        from . import ENERGY_UNITS

        available = sorted(
            s.entity_id
            for s in self.hass.states.async_all()
            if (
                s.attributes.get("state_class") == "total_increasing"
                and s.attributes.get("unit_of_measurement", "") in ENERGY_UNITS
                and s.entity_id not in managed
            )
        )

        if not available:
            return self.async_abort(reason="no_sensors_available")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ENTITY_ID): vol.In(available)}
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Discovery flow: hub detected a spike on an unmanaged sensor
    # ------------------------------------------------------------------

    async def async_step_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle discovery from the hub."""
        entity_id = discovery_info[CONF_ENTITY_ID]

        await self.async_set_unique_id(entity_id)
        self._abort_if_unique_id_configured()

        self._discovery_data = discovery_info
        self.context["title_placeholders"] = {
            "name": self._friendly_name(entity_id),
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask user to confirm monitoring a discovered sensor."""
        assert self._discovery_data is not None
        entity_id = self._discovery_data[CONF_ENTITY_ID]

        if user_input is not None:
            name = self._friendly_name(entity_id)
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ENTITY_ID: entity_id,
                    "spike_jump_kwh": self._discovery_data.get("spike_jump_kwh", 0),
                },
            )

        spike_from = self._discovery_data.get("spike_from", "?")
        spike_to = self._discovery_data.get("spike_to", "?")
        spike_unit = self._discovery_data.get("spike_unit", "kWh")
        implied_kw = self._discovery_data.get("implied_power_kw", "?")

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "entity_id": entity_id,
                "spike_from": str(spike_from),
                "spike_to": str(spike_to),
                "spike_unit": spike_unit,
                "implied_power_kw": str(implied_kw),
            },
        )

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow."""
        return CleanEnergyOptionsFlow()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _friendly_name(self, entity_id: str) -> str:
        """Get a friendly name for an entity."""
        state = self.hass.states.get(entity_id)
        if state and state.attributes.get("friendly_name"):
            return state.attributes["friendly_name"]
        return entity_id


class CleanEnergyOptionsFlow(OptionsFlow):
    """Options flow - only shown on the hub entry (no entity_id in data)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        # Only the hub entry (no entity_id) has configurable options
        if self.config_entry.data.get(CONF_ENTITY_ID):
            return self.async_abort(reason="no_options")

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAX_POWER_KW, default=current): vol.Coerce(
                        float
                    ),
                }
            ),
        )
