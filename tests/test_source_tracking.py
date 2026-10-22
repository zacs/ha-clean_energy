"""Following the source sensor when it is renamed or moves device."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.clean_energy.const import CONF_ENTITY_ID, DOMAIN
from custom_components.clean_energy.entries import managed_entity_ids

from .test_setup import _hub, _setup

SOURCE = "sensor.lamp_energy"
RENAMED = "sensor.boys_room_lamp_energy"
CLEAN = "sensor.lamp_energy_clean"
ATTRS = {"unit_of_measurement": "kWh", "state_class": "total_increasing"}


async def _monitored_source(
    hass: HomeAssistant,
) -> tuple[MockConfigEntry, dr.DeviceEntry]:
    source_entry = MockConfigEntry(domain="zwave_js")
    source_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("zwave_js", "lamp")},
        name="Lamp",
    )
    er.async_get(hass).async_get_or_create(
        "sensor",
        "zwave_js",
        "lamp-energy",
        suggested_object_id="lamp_energy",
        device_id=device.id,
        config_entry=source_entry,
    )
    hass.states.async_set(SOURCE, "38.0", ATTRS)
    await _setup(hass, _hub(hass))
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTITY_ID: SOURCE}, unique_id=SOURCE
    )
    await _setup(hass, entry)
    return entry, device


async def test_renaming_the_source_is_followed(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Renaming a device renames its entities; the clean sensor used to go deaf.

    It kept listening to an entity_id that no longer existed, while the hub,
    no longer recognising the renamed sensor as managed, would offer it for
    discovery all over again.
    """
    entry, _ = await _monitored_source(hass)
    assert float(hass.states.get(CLEAN).state) == pytest.approx(38.0)

    er.async_get(hass).async_update_entity(SOURCE, new_entity_id=RENAMED)
    await hass.async_block_till_done()
    hass.states.async_set(RENAMED, "38.5", ATTRS)
    await hass.async_block_till_done()

    assert entry.data[CONF_ENTITY_ID] == RENAMED
    assert entry.unique_id == RENAMED
    assert managed_entity_ids(hass) == {RENAMED}
    # The clean sensor keeps its own id, and counts the renamed source.
    assert float(hass.states.get(CLEAN).state) == pytest.approx(38.5)


async def test_moving_the_source_to_another_device_is_followed(
    recorder_mock, hass: HomeAssistant
) -> None:
    entry, device = await _monitored_source(hass)
    device_registry, entity_registry = dr.async_get(hass), er.async_get(hass)
    assert entity_registry.async_get(CLEAN).device_id == device.id

    other = device_registry.async_get_or_create(
        config_entry_id=device.config_entry_id,
        identifiers={("zwave_js", "replacement-plug")},
        name="Replacement Plug",
    )
    entity_registry.async_update_entity(SOURCE, device_id=other.id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(CLEAN).device_id == other.id
    assert dr.async_entries_for_config_entry(device_registry, entry.entry_id) == []
