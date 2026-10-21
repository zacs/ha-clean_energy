"""Upgrading from a pre-2026.8 install, through Home Assistant's real split.

Versions before 0.4.0 joined the source's device instead of linking to it.
Home Assistant 2026.8 made devices single-config-entry and, on first load,
split every shared device into one per config entry — so each monitored
device came back as a Z-Wave device plus a Clean Energy duplicate with our
entities on it, reported as "This device has no entities".

These tests seed the registries in the format Home Assistant 2026.7 wrote
(device registry 1.12) and let the real loader perform that split, rather
than constructing its result by hand.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.clean_energy.const import (
    CONF_ENTITY_ID,
    CONF_INITIAL_OFFSET,
    DOMAIN,
    LEGACY_BACKFILL_DONE_KEY,
)

from .test_setup import _hub, _setup

SOURCE_ID = "sensor.lamp_energy"
SUFFIXES = ("clean", "last_spike", "last_spike_size", "energy_removed", "spike_count")


def _entity(entity_id: str, platform: str, unique_id: str, entry_id: str) -> dict:
    """An entity registry row, as stored, linked to the shared device."""
    return {
        "aliases": [],
        "aliases_v2": [],
        "area_id": None,
        "categories": {},
        "capabilities": None,
        "config_entry_id": entry_id,
        "config_subentry_id": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "device_class": None,
        "device_id": "shared-device",
        "disabled_by": None,
        "entity_category": None,
        "entity_id": entity_id,
        "hidden_by": None,
        "icon": None,
        "id": f"{unique_id}-registry-id",
        "has_entity_name": False,
        "labels": [],
        "modified_at": "2026-01-01T00:00:00+00:00",
        "name": None,
        "object_id_base": None,
        "options": {},
        "original_device_class": None,
        "original_icon": None,
        "original_name": None,
        "platform": platform,
        "suggested_object_id": None,
        "supported_features": 0,
        "translation_key": None,
        "unique_id": unique_id,
        "previous_unique_id": None,
        "unit_of_measurement": None,
    }


def _shared_device(*entry_ids: str) -> dict:
    """A device as Home Assistant 2026.7 stored one shared by several entries."""
    return {
        "area_id": None,
        "config_entries": list(entry_ids),
        "config_entries_subentries": {entry_id: [None] for entry_id in entry_ids},
        "configuration_url": None,
        "connections": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "disabled_by": None,
        "entry_type": None,
        "hw_version": None,
        "id": "shared-device",
        "identifiers": [["zwave_js", "basement-stairwell"]],
        "labels": [],
        "manufacturer": "Inovelli",
        "model": "VZW31-SN",
        "model_id": None,
        "modified_at": "2026-01-01T00:00:00+00:00",
        "name_by_user": None,
        "name": "In Basement Stairwell",
        "primary_config_entry": entry_ids[0],
        "serial_number": None,
        "sw_version": "1.2.0",
        "via_device_id": None,
    }


async def _load_pre_split_registries(
    hass: HomeAssistant, hass_storage: dict, *, entity_rows: list[dict]
) -> None:
    hass_storage["core.device_registry"] = {
        "version": 1,
        "minor_version": 12,
        "key": "core.device_registry",
        "data": {
            "devices": [_shared_device("zwave_entry", "clean_entry")],
            "deleted_devices": [],
        },
    }
    hass_storage["core.entity_registry"] = {
        "version": er.STORAGE_VERSION_MAJOR,
        "minor_version": er.STORAGE_VERSION_MINOR,
        "key": "core.entity_registry",
        "data": {
            "entities": entity_rows,
            "deleted_entities": [],
            "settings": {"entity_id_parts": None},
        },
    }
    for registry in (ar, cr, fr, lr, ir):
        await registry.async_load(hass)
    dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


# Loading the registries by hand leaves their debounced save pending.
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


@pytest.mark.parametrize("load_registries", [False])
async def test_upgrade_puts_entities_back_on_the_source_device(
    recorder_mock,
    hass: HomeAssistant,
    hass_storage: dict,
) -> None:
    """The reported device page: Clean Energy listed, "no entities" shown."""
    zwave = MockConfigEntry(domain="zwave_js", entry_id="zwave_entry")
    zwave.add_to_hass(hass)
    ours = MockConfigEntry(
        domain=DOMAIN,
        entry_id="clean_entry",
        data={
            CONF_ENTITY_ID: SOURCE_ID,
            CONF_INITIAL_OFFSET: 0.0,
            LEGACY_BACKFILL_DONE_KEY: True,
        },
        unique_id=SOURCE_ID,
    )
    ours.add_to_hass(hass)
    await _load_pre_split_registries(
        hass,
        hass_storage,
        entity_rows=[
            _entity(SOURCE_ID, "zwave_js", "lamp-energy", "zwave_entry"),
            *(
                _entity(
                    f"sensor.lamp_energy_{suffix}",
                    DOMAIN,
                    f"clean_entry_{suffix}",
                    "clean_entry",
                )
                for suffix in SUFFIXES
            ),
        ],
    )
    device_registry, entity_registry = dr.async_get(hass), er.async_get(hass)

    # Home Assistant's split, as it happened on the reported install.
    [zwave_device] = dr.async_entries_for_config_entry(device_registry, "zwave_entry")
    [duplicate] = dr.async_entries_for_config_entry(device_registry, "clean_entry")
    assert duplicate.name == zwave_device.name == "In Basement Stairwell"
    assert entity_registry.async_get(SOURCE_ID).device_id == zwave_device.id
    assert (
        entity_registry.async_get("sensor.lamp_energy_clean").device_id == duplicate.id
    )

    hass.states.async_set(
        SOURCE_ID,
        "38.0",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    records = _Records()
    logging.getLogger().addHandler(records)
    try:
        # Setting up the hub sets up every Clean Energy entry, ours included.
        await _setup(hass, _hub(hass))
    finally:
        logging.getLogger().removeHandler(records)

    # The duplicate is gone and every entity is back on the real device,
    # under the entity_id it always had.
    assert dr.async_entries_for_config_entry(device_registry, "clean_entry") == []
    assert device_registry.async_get(zwave_device.id) is not None
    for suffix in SUFFIXES:
        entity = entity_registry.async_get(f"sensor.lamp_energy_{suffix}")
        assert entity is not None, suffix
        assert entity.device_id == zwave_device.id, suffix

    # And none of it went through deprecated device-registry API.
    reports = [
        message
        for message in records.messages
        if "clean_energy" in message
        and ("deprecated" in message or "Detected" in message)
    ]
    assert reports == []


@pytest.mark.parametrize("load_registries", [False])
async def test_upgrade_with_entities_already_detached(
    recorder_mock, hass: HomeAssistant, hass_storage: dict
) -> None:
    """The state 0.3.0 left behind: entities off any device, duplicate kept.

    0.3.0 linked via device_entry but never removed the duplicate, so an
    install that ran it on 2026.8 has the duplicate plus detached entities.
    """
    zwave = MockConfigEntry(domain="zwave_js", entry_id="zwave_entry")
    zwave.add_to_hass(hass)
    ours = MockConfigEntry(
        domain=DOMAIN,
        entry_id="clean_entry",
        data={CONF_ENTITY_ID: SOURCE_ID, LEGACY_BACKFILL_DONE_KEY: True},
        unique_id=SOURCE_ID,
    )
    ours.add_to_hass(hass)
    rows = [
        _entity(SOURCE_ID, "zwave_js", "lamp-energy", "zwave_entry"),
        *(
            _entity(
                f"sensor.lamp_energy_{s}", DOMAIN, f"clean_entry_{s}", "clean_entry"
            )
            for s in SUFFIXES
        ),
    ]
    for row in rows[1:]:
        row["device_id"] = None
    await _load_pre_split_registries(hass, hass_storage, entity_rows=rows)
    device_registry, entity_registry = dr.async_get(hass), er.async_get(hass)
    [zwave_device] = dr.async_entries_for_config_entry(device_registry, "zwave_entry")

    hass.states.async_set(
        SOURCE_ID,
        "38.0",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    await _setup(hass, _hub(hass))

    assert dr.async_entries_for_config_entry(device_registry, "clean_entry") == []
    for suffix in SUFFIXES:
        entity = entity_registry.async_get(f"sensor.lamp_energy_{suffix}")
        assert entity.device_id == zwave_device.id, suffix
