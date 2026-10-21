"""End-to-end setup tests through the real config-entry pipeline.

These go through ``async_setup_entry`` rather than poking the entity
directly, so they cover the parts the unit tests mock out: the entity
platform, the device link, and writing back to the config entry.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.clean_energy import entries
from custom_components.clean_energy import sensor as sensor_module
from custom_components.clean_energy.const import (
    CONF_ENTITY_ID,
    CONF_INITIAL_OFFSET,
    CONF_MAX_POWER_KW,
    DOMAIN,
    HISTORY_DONE_KEY,
    HUB_UNIQUE_ID,
    LEGACY_BACKFILL_DONE_KEY,
)

SOURCE_ID = "sensor.flaky_energy"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry, letting a new sensor's rebuild settle.

    A new sensor derives its starting value from the source's history in a
    background task, which async_block_till_done deliberately doesn't wait
    for, and whose fetch runs on the recorder's thread. These sources have
    no statistics yet, so it finishes promptly; wait for it rather than race.
    """
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if not entry.data.get(CONF_ENTITY_ID) or entry.data.get(LEGACY_BACKFILL_DONE_KEY):
        return
    for _ in range(100):
        if entry.data.get(HISTORY_DONE_KEY):
            return
        await async_wait_recording_done(hass)
        await hass.async_block_till_done()
    raise AssertionError(f"{entry.title}: first rebuild never finished")


def _hub(hass: HomeAssistant, max_power_kw: float = 50.0) -> MockConfigEntry:
    """Build the singleton hub entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Clean Energy",
        data={},
        options={CONF_MAX_POWER_KW: max_power_kw},
        unique_id=HUB_UNIQUE_ID,
    )


def _sensor_entry(initial_offset: float = 0.0, **data) -> MockConfigEntry:
    """Build a per-sensor entry for SOURCE_ID."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Flaky Energy",
        data={CONF_ENTITY_ID: SOURCE_ID, CONF_INITIAL_OFFSET: initial_offset, **data},
        unique_id=SOURCE_ID,
    )


def _set_source(hass: HomeAssistant, value: float) -> None:
    """Publish the monitored source sensor's state."""
    hass.states.async_set(
        SOURCE_ID,
        str(value),
        {
            "unit_of_measurement": "kWh",
            "state_class": "total_increasing",
            "device_class": "energy",
            "friendly_name": "Flaky Energy",
        },
    )


def test_deprecated_device_link_helper_is_not_imported() -> None:
    """The deprecated helper must not come back.

    ``async_device_info_to_link_from_entity`` always returns None from
    2026.8, silently unlinking every entity we create, and is removed in
    2027.8. A module-level ``from`` import would bind the name in the module
    namespace, so checking for it there is enough.
    """
    assert not hasattr(sensor_module, "async_device_info_to_link_from_entity")
    assert hasattr(sensor_module, "async_entity_id_to_device")


async def test_entities_are_created(recorder_mock, hass: HomeAssistant) -> None:
    """A per-sensor entry produces the clean sensor plus four diagnostics."""
    _set_source(hass, 10.0)
    await _setup(hass, _hub(hass))
    entry = _sensor_entry()
    await _setup(hass, entry)

    registry = er.async_get(hass)
    ours = sorted(
        e.entity_id for e in registry.entities.values() if e.platform == DOMAIN
    )
    assert ours == [
        "sensor.flaky_energy_clean",
        "sensor.flaky_energy_energy_removed",
        "sensor.flaky_energy_last_spike",
        "sensor.flaky_energy_last_spike_size",
        "sensor.flaky_energy_spike_count",
    ]

    clean = hass.states.get("sensor.flaky_energy_clean")
    assert float(clean.state) == pytest.approx(10.0)
    assert clean.attributes["state_class"] == "total_increasing"
    assert clean.attributes["unit_of_measurement"] == "kWh"
    # No history to import, so the first rebuild had nothing to replace.
    assert entry.data[HISTORY_DONE_KEY] is True


async def test_entity_ids_ignore_the_device_name(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Home Assistant now prefixes the device name onto derived entity_ids.

    The README promises sensor.<source>_clean, and the Energy Dashboard is
    configured by entity_id, so the clean sensor suggests its own id.
    """
    source_entry = MockConfigEntry(domain="demo")
    source_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("demo", "flaky-plug")},
        name="Flaky Plug",
    )
    er.async_get(hass).async_get_or_create(
        "sensor",
        "demo",
        "flaky-energy",
        suggested_object_id="flaky_energy",
        device_id=device.id,
        config_entry=source_entry,
    )
    _set_source(hass, 10.0)
    await _setup(hass, _hub(hass))
    await _setup(hass, _sensor_entry())

    assert hass.states.get("sensor.flaky_energy_clean") is not None
    assert hass.states.get("sensor.flaky_plug_flaky_energy_clean") is None


async def test_clean_sensor_is_linked_to_the_source_device(
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """Our entities land on the source's device, without joining it."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    source_entry = MockConfigEntry(domain="demo")
    source_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("demo", "flaky-plug")},
        name="Flaky Plug",
    )
    entity_registry.async_get_or_create(
        "sensor",
        "demo",
        "flaky-energy",
        suggested_object_id="flaky_energy",
        device_id=device.id,
        config_entry=source_entry,
    )
    _set_source(hass, 10.0)

    await _setup(hass, _hub(hass))
    ours = _sensor_entry()
    await _setup(hass, ours)

    clean = entity_registry.async_get("sensor.flaky_energy_clean")
    assert clean is not None
    assert clean.device_id == device.id

    # The link comes from device_entry, not from a DeviceInfo payload.
    platform = next(
        p
        for p in async_get_platforms(hass, DOMAIN)
        if p.config_entry and p.config_entry.unique_id == SOURCE_ID
    )
    for entity in platform.entities.values():
        assert entity.device_info is None
        assert entity.device_entry is not None and entity.device_entry.id == device.id

    # The source's device still belongs to the source alone, and we own none.
    assert device_registry.async_get(device.id).config_entry_id == source_entry.entry_id
    assert dr.async_entries_for_config_entry(device_registry, ours.entry_id) == []


async def test_clean_sensor_never_reports_a_negative_state(
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """A stale discovery offset larger than the source is discarded.

    The source has no recorded history here, so the seed falls back to the
    offset discovery recorded, which must not drive the value negative.
    """
    _set_source(hass, 6.07)
    await _setup(hass, _hub(hass))
    entry = _sensor_entry(initial_offset=14316560.0)
    await _setup(hass, entry)

    _set_source(hass, 6.14)
    await hass.async_block_till_done()

    clean = hass.states.get("sensor.flaky_energy_clean")
    assert float(clean.state) >= 0

    # The stale offset is scrubbed from the entry so it can't come back.
    assert entry.data[CONF_INITIAL_OFFSET] == 0.0


async def test_hub_threshold_is_read_from_the_hub_entry(
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """Neither sensor entries nor ignored discoveries shadow the hub.

    Both carry no threshold. An ignored discovery also carries no sensor,
    which is why "the entry without a sensor" was the wrong way to find
    the hub: if one sorted first, the configured value was replaced by the
    default.
    """
    MockConfigEntry(
        domain=DOMAIN, source=SOURCE_IGNORE, data={}, unique_id="sensor.ignored"
    ).add_to_hass(hass)
    _set_source(hass, 10.0)
    await _setup(hass, _sensor_entry())
    await _setup(hass, _hub(hass, max_power_kw=17.5))

    assert entries.max_power_kw(hass) == pytest.approx(17.5)
    assert hass.data[DOMAIN]["hub"].max_power_kw == pytest.approx(17.5)


async def test_a_sensor_can_have_its_own_threshold(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A per-sensor ceiling wins over the hub's; without one, the hub's applies."""
    _set_source(hass, 10.0)
    await _setup(hass, _hub(hass, max_power_kw=50.0))
    entry = _sensor_entry()
    await _setup(hass, entry)
    assert entries.max_power_kw(hass, entry) == pytest.approx(50.0)

    hass.config_entries.async_update_entry(entry, options={CONF_MAX_POWER_KW: 3.0})
    await hass.async_block_till_done()
    assert entries.max_power_kw(hass, entry) == pytest.approx(3.0)
    assert (
        hass.states.get("sensor.flaky_energy_clean").attributes["max_power_kw"] == 3.0
    )


async def test_unload_removes_entities(recorder_mock, hass: HomeAssistant) -> None:
    """Unloading a per-sensor entry tears its entities down."""
    _set_source(hass, 10.0)
    await _setup(hass, _hub(hass))
    entry = _sensor_entry()
    await _setup(hass, entry)

    assert hass.states.get("sensor.flaky_energy_clean") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Registry entities keep a restored placeholder state after unload.
    clean = hass.states.get("sensor.flaky_energy_clean")
    assert clean.state == STATE_UNAVAILABLE


async def test_a_renamed_clean_sensor_keeps_its_name(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The clean sensor suggests sensor.<source>_clean, but only for new
    entities: one the user renamed (and pointed the Energy Dashboard at)
    must keep its id across reloads and upgrades."""
    _set_source(hass, 10.0)
    await _setup(hass, _hub(hass))
    entry = _sensor_entry()
    await _setup(hass, entry)

    registry = er.async_get(hass)
    registry.async_update_entity(
        "sensor.flaky_energy_clean", new_entity_id="sensor.my_lamp_for_the_dashboard"
    )
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.my_lamp_for_the_dashboard") is not None
    assert registry.async_get("sensor.flaky_energy_clean") is None
