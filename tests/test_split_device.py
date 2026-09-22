"""Split-device case: entities sitting on a Clean-Energy-only device."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.clean_energy.const import DOMAIN

from .test_setup import _hub, _sensor_entry, _set_source, _setup

SUFFIXES = ("clean", "last_spike", "last_spike_size", "energy_removed", "spike_count")


async def test_entity_ids_survive_deleting_a_split_device(
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """A device only we own is deleted; entity_ids must not be reshuffled.

    Deleting a device removes the registry entries of entities attached to
    it, so the ids have to come back from the deleted-entity cache rather
    than being reallocated with a _2 suffix, or every dashboard card and
    automation referencing them breaks.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    zwave = MockConfigEntry(domain="zwave_js")
    zwave.add_to_hass(hass)
    real_device = device_registry.async_get_or_create(
        config_entry_id=zwave.entry_id,
        identifiers={("zwave_js", "stairwell")},
        name="Basement Stairwell Lights",
    )
    entity_registry.async_get_or_create(
        "sensor",
        "zwave_js",
        "flaky-energy",
        suggested_object_id="flaky_energy",
        device_id=real_device.id,
        config_entry=zwave,
    )
    _set_source(hass, 10.0)
    await _setup(hass, _hub(hass))

    ours = _sensor_entry()
    ours.add_to_hass(hass)

    # The phantom: a device Clean Energy alone owns, carrying our entities.
    split = device_registry.async_get_or_create(
        config_entry_id=ours.entry_id,
        identifiers={(DOMAIN, "stairwell-split")},
        name="In Basement Stairwell",
    )
    assert split.config_entries == {ours.entry_id}
    expected = []
    for suffix in SUFFIXES:
        entry = entity_registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{ours.entry_id}_{suffix}",
            suggested_object_id=f"flaky_energy_{suffix}",
            device_id=split.id,
            config_entry=ours,
        )
        expected.append(entry.entity_id)

    assert await hass.config_entries.async_setup(ours.entry_id)
    await hass.async_block_till_done()

    # The phantom is gone.
    assert device_registry.async_get(split.id) is None

    # Entities are on the real device, with their original ids intact.
    got = {
        e.entity_id: e.device_id
        for e in entity_registry.entities.values()
        if e.platform == DOMAIN
    }
    assert sorted(got) == sorted(expected)
    assert all(d == real_device.id for d in got.values()), got
