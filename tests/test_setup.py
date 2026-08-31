"""End-to-end setup tests through the real config-entry pipeline.

These go through ``async_setup_entry`` rather than poking the entity
directly, so they cover the parts the unit tests mock out: the entity
platform, the device link, and writing back to the config entry.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.clean_energy import sensor as sensor_module
from custom_components.clean_energy.const import (
    CONF_ENTITY_ID,
    CONF_INITIAL_OFFSET,
    CONF_MAX_POWER_KW,
    DOMAIN,
    HUB_UNIQUE_ID,
)

SOURCE_ID = "sensor.flaky_energy"


@pytest.fixture(autouse=True)
def _no_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the LTS backfill; it is not what these tests are about."""

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.clean_energy.sensor._backfill_history", _noop
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _hub(hass: HomeAssistant, max_power_kw: float = 50.0) -> MockConfigEntry:
    """Build the singleton hub entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Clean Energy",
        data={},
        options={CONF_MAX_POWER_KW: max_power_kw},
        unique_id=HUB_UNIQUE_ID,
    )


def _sensor_entry(initial_offset: float = 0.0) -> MockConfigEntry:
    """Build a per-sensor entry for SOURCE_ID."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Flaky Energy",
        data={CONF_ENTITY_ID: SOURCE_ID, CONF_INITIAL_OFFSET: initial_offset},
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

    ``async_device_info_to_link_from_entity`` still works on the Home
    Assistant version these tests run against, so no behavioural test here
    would catch a regression to it — but on 2026.8 it logs a deprecation
    warning and always returns None, silently unlinking every entity we
    create, and it is scheduled for removal in 2027.8. A module-level
    ``from`` import would bind the name in the module namespace, so checking
    for it there is enough.
    """
    assert not hasattr(sensor_module, "async_device_info_to_link_from_entity")
    assert hasattr(sensor_module, "async_entity_id_to_device")


async def test_entities_are_created(recorder_mock, hass: HomeAssistant) -> None:
    """A per-sensor entry produces the clean sensor plus four diagnostics."""
    _set_source(hass, 10.0)
    await _setup(hass, _hub(hass))
    await _setup(hass, _sensor_entry())

    registry = er.async_get(hass)
    ours = [e for e in registry.entities.values() if e.platform == DOMAIN]
    assert len(ours) == 5

    clean = hass.states.get("sensor.flaky_energy_clean")
    assert clean is not None
    assert float(clean.state) == pytest.approx(10.0)
    assert clean.attributes["state_class"] == "total_increasing"
    assert clean.attributes["unit_of_measurement"] == "kWh"


async def test_clean_sensor_is_linked_to_the_source_device(
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """Our entities land on the source's device, without adopting it.

    ``async_device_info_to_link_from_entity`` is deprecated and now always
    returns None; assigning ``device_entry`` is the supported replacement.
    """
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
    await _setup(hass, _sensor_entry())

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

    # Linking must not pull the source's device into our config entry.
    assert source_entry.entry_id in device_registry.async_get(device.id).config_entries
    assert (
        _sensor_entry().entry_id
        not in device_registry.async_get(device.id).config_entries
    )


async def test_clean_sensor_never_reports_a_negative_state(
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """The reported bug, end to end.

    A negative ``total_increasing`` state makes the recorder discard every
    statistics row for the entity, so the Energy Dashboard shows nothing.
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
    """Per-sensor entries carry no threshold, so they must not shadow the hub."""
    _set_source(hass, 10.0)
    # Add the per-sensor entry first, so it sorts ahead of the hub.
    await _setup(hass, _sensor_entry())
    await _setup(hass, _hub(hass, max_power_kw=17.5))

    assert sensor_module._hub_max_power_kw(hass) == pytest.approx(17.5)

    hub = hass.data[DOMAIN]["hub"]
    assert hub.max_power_kw == pytest.approx(17.5)


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
