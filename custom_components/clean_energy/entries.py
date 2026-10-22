"""Reading Clean Energy's config entries.

There are three kinds: the singleton hub, one entry per monitored sensor, and
"ignored" entries Home Assistant creates when a discovery is dismissed. An
ignored entry carries no data, so "the entry without a sensor" is not a safe
way to find the hub: an ignored one sorting first would silently replace the
configured power threshold with the default.
"""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_ENTITY_ID,
    CONF_MAX_POWER_KW,
    DEFAULT_MAX_POWER_KW,
    DOMAIN,
    HUB_UNIQUE_ID,
)


@callback
def hub_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the hub entry, if it exists."""
    return hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, HUB_UNIQUE_ID)


@callback
def sensor_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Return the entries that monitor a sensor."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
        if entry.source != SOURCE_IGNORE and entry.data.get(CONF_ENTITY_ID)
    ]


@callback
def managed_entity_ids(hass: HomeAssistant) -> set[str]:
    """Return the source entity_ids that have a monitoring entry."""
    return {entry.data[CONF_ENTITY_ID] for entry in sensor_entries(hass)}


@callback
def max_power_kw(hass: HomeAssistant, entry: ConfigEntry | None = None) -> float:
    """Return the power ceiling to test a sensor against.

    A sensor's own ceiling wins. Otherwise the hub's applies, which has to be
    generous enough for whole-home meters; small devices such as smart plugs
    benefit from their own, much lower one.
    """
    if entry is not None and (own := entry.options.get(CONF_MAX_POWER_KW)):
        return float(own)
    if (hub := hub_entry(hass)) is not None:
        return float(hub.options.get(CONF_MAX_POWER_KW, DEFAULT_MAX_POWER_KW))
    return DEFAULT_MAX_POWER_KW
