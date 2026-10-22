"""Config and options flows."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_DISCOVERY, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.clean_energy.const import (
    CONF_ENTITY_ID,
    CONF_INITIAL_OFFSET,
    CONF_MAX_POWER_KW,
    DOMAIN,
    HUB_UNIQUE_ID,
)

SOURCE = "sensor.lamp_energy"
ATTRS = {"unit_of_measurement": "kWh", "state_class": "total_increasing"}


@pytest.fixture(autouse=True)
def _no_setup(recorder_mock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flows only: don't set the created entries up (the recorder is a
    dependency the flows still load)."""

    async def _ok(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr("custom_components.clean_energy.async_setup_entry", _ok)


async def test_hub_is_created_once(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MAX_POWER_KW: 30.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_MAX_POWER_KW: 30.0}

    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert again["type"] is FlowResultType.ABORT


async def test_discovery_records_the_spike_it_saw(hass: HomeAssistant) -> None:
    """The offset is only a fallback for sources with no recorded history."""
    hass.states.async_set(SOURCE, "693.35", ATTRS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DISCOVERY},
        data={
            CONF_ENTITY_ID: SOURCE,
            "spike_from": 38.0,
            "spike_to": 693.35,
            "spike_unit": "kWh",
            "implied_power_kw": 46.8,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENTITY_ID] == SOURCE
    assert result["data"][CONF_INITIAL_OFFSET] == pytest.approx(655.35)


async def test_service_flow_rejects_non_energy_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.temperature", "21", {"unit_of_measurement": "°C"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "monitor_service"},
        data={CONF_ENTITY_ID: "sensor.temperature"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_energy_sensor"


async def test_hub_options(hass: HomeAssistant) -> None:
    hub = MockConfigEntry(
        domain=DOMAIN, options={CONF_MAX_POWER_KW: 50.0}, unique_id=HUB_UNIQUE_ID
    )
    hub.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(hub.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_POWER_KW: 25.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hub.options == {CONF_MAX_POWER_KW: 25.0}


async def test_sensor_options_set_and_clear_a_ceiling(hass: HomeAssistant) -> None:
    """A sensor gets its own ceiling, or inherits the hub's when left empty.

    There's deliberately no offset field any more: applying one changed the
    running total in place, which the recorder counted as consumption.
    """
    MockConfigEntry(
        domain=DOMAIN, options={CONF_MAX_POWER_KW: 50.0}, unique_id=HUB_UNIQUE_ID
    ).add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTITY_ID: SOURCE}, unique_id=SOURCE
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "sensor"
    assert list(result["data_schema"].schema) == [CONF_MAX_POWER_KW]
    assert result["description_placeholders"]["hub_max_power_kw"] == "50"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_POWER_KW: 3.0}
    )
    assert entry.options == {CONF_MAX_POWER_KW: 3.0}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.options == {}
