"""Config flow for Clean Energy.

Entry points:

1. User adds the integration via the UI - creates the singleton hub entry
   (one per HA instance, enforced via ``unique_id``). The hub then watches
   all energy sensors passively for spikes.
2. ``clean_energy.monitor_sensor`` service - manually adds a per-sensor
   entry (used when you already know a sensor is flaky and don't want to
   wait for the passive watcher to catch a spike).
3. Discovery flow - the hub auto-proposes a per-sensor entry when it
   observes a spike on an unmanaged sensor.

Per-sensor entries are intentionally never offered from the "Add
Integration" UI button. This avoids the trap where clicking the integration
tile a second time forces the user into a sensor picker with no escape; the
modern HA pattern (used by e.g. Battery Notes) is to keep the hub-add path
hub-only and surface device adds through discovery or a dedicated flow.
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

from .const import (
    CONF_ENTITY_ID,
    CONF_INITIAL_OFFSET,
    CONF_MAX_POWER_KW,
    DEFAULT_MAX_POWER_KW,
    DOMAIN,
    HUB_UNIQUE_ID,
)
from .entries import max_power_kw
from .spike import is_energy_sensor


def _is_energy_sensor(hass, entity_id: str) -> bool:
    """Check if an entity is a total_increasing energy sensor."""
    return is_energy_sensor(hass.states.get(entity_id))


class CleanEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Clean Energy."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._discovery_data: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # User flow: first-time setup or manual sensor addition
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated setup.

        Clean Energy is a singleton hub: the "Add Integration" entry point
        only ever creates the hub. Per-sensor monitoring is added via the
        discovery flow (automatic on spike) or the
        ``clean_energy.monitor_sensor`` service (manual).
        """
        # Enforce singleton hub via unique_id.
        await self.async_set_unique_id(HUB_UNIQUE_ID)
        self._abort_if_unique_id_configured()

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

    # ------------------------------------------------------------------
    # Service-initiated flow: manual sensor add via ``monitor_sensor``
    # ------------------------------------------------------------------

    async def async_step_monitor_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a sensor entry from the ``clean_energy.monitor_sensor`` service.

        ``user_input`` is always supplied by the service caller; no form is
        shown. We still validate and set a unique_id so duplicates abort
        cleanly.
        """
        assert user_input is not None
        entity_id = user_input[CONF_ENTITY_ID]

        if not _is_energy_sensor(self.hass, entity_id):
            return self.async_abort(reason="not_energy_sensor")

        await self.async_set_unique_id(entity_id)
        self._abort_if_unique_id_configured()

        name = self._friendly_name(entity_id)
        return self.async_create_entry(
            title=name,
            data={CONF_ENTITY_ID: entity_id},
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
            # Compute the initial offset to bake into the entry: the
            # difference between the source's current value and the
            # pre-spike baseline the hub captured. This is what the filter
            # sensor needs to subtract on seed so the Clean value starts at
            # the correct (pre-spike) level instead of mirroring the spike.
            pre_spike = self._discovery_data.get("spike_from")
            current_state = self.hass.states.get(entity_id)
            initial_offset = 0.0
            if pre_spike is not None and current_state is not None:
                try:
                    current_val = float(current_state.state)
                    initial_offset = max(0.0, current_val - float(pre_spike))
                except ValueError, TypeError:
                    initial_offset = 0.0

            name = self._friendly_name(entity_id)
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ENTITY_ID: entity_id,
                    CONF_INITIAL_OFFSET: initial_offset,
                },
            )

        spike_from = self._discovery_data.get("spike_from", "?")
        spike_to = self._discovery_data.get("spike_to", "?")
        spike_unit = self._discovery_data.get("spike_unit", "kWh")
        implied_kw = self._discovery_data.get("implied_power_kw", "?")

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._friendly_name(entity_id),
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
    """Options flow.

    * Hub entry: the global max-power threshold, used for discovery and for
      every sensor without its own.
    * Per-sensor entry: an optional max-power threshold for just that sensor.
      Leaving it empty inherits the hub's. A low ceiling suits small devices:
      a smart plug can't draw more than a few kW, so a jump implying more is
      a glitch even when it is far below a whole-home meter's ceiling.

    Sensors no longer take a manual offset here. Applying one meant changing
    a running total in place, which the recorder counts as consumption; the
    ``clean_energy.rebuild_history`` action corrects it safely instead.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if self.config_entry.data.get(CONF_ENTITY_ID):
            return await self.async_step_sensor()

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAX_POWER_KW, default=current): vol.All(
                        vol.Coerce(float), vol.Range(min=0.1)
                    ),
                }
            ),
        )

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit per-sensor options."""
        if user_input is not None:
            # An empty field means "inherit the hub's", so drop the key.
            return self.async_create_entry(
                title="",
                data={k: v for k, v in user_input.items() if v is not None},
            )

        current = self.config_entry.options.get(CONF_MAX_POWER_KW)
        return self.async_show_form(
            step_id="sensor",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_MAX_POWER_KW,
                        description={"suggested_value": current},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
                }
            ),
            description_placeholders={
                "entity_id": self.config_entry.data.get(CONF_ENTITY_ID, ""),
                "hub_max_power_kw": f"{max_power_kw(self.hass):g}",
            },
        )
