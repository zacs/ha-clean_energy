"""Constants for Clean Energy."""

DOMAIN = "clean_energy"

CONF_MAX_POWER_KW = "max_power_kw"
CONF_ENTITY_ID = "entity_id"
# Cumulative kWh (in the source's native unit) to subtract from the source
# when emitting the Clean value. Set at entry-creation time from the
# pre-spike baseline captured by the hub; can be edited via the options flow
# to repair an entry whose spike was missed (e.g. because the integration
# was installed *after* the spike already baked into the source).
CONF_INITIAL_OFFSET = "initial_offset"

SIGNAL_SPIKE_CORRECTED = f"{DOMAIN}_spike_corrected"

SERVICE_MONITOR_SENSOR = "monitor_sensor"

# Stable unique_id for the singleton hub config entry. Any per-sensor entries
# use the source entity_id as their unique_id.
HUB_UNIQUE_ID = f"{DOMAIN}_hub"

# Marker stored in entry.data once we've copied the source sensor's existing
# Long-Term Statistics into the new clean entity.
BACKFILL_DONE_KEY = "backfill_done"

# 50 kW is very generous - covers large homes, EV chargers, etc.
# A single 200A residential service tops out around 48 kW.
DEFAULT_MAX_POWER_KW = 50.0

# Minimum elapsed time (seconds) between readings for rate calculation.
# Prevents division-by-near-zero with rapid-fire updates.
MIN_ELAPSED_SECONDS = 30.0
