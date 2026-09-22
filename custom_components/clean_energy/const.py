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

# Absolute floor on what counts as a spike. The rate test alone is too eager:
# `MIN_ELAPSED_SECONDS` clamps the denominator, so *any* jump above roughly
# 0.4 kWh reported within 30s of the previous reading implies >50 kW and
# trips the filter. Meters that batch up readings (after an outage, or after
# a Home Assistant restart) legitimately do that. Real broken-meter spikes
# are orders of magnitude larger, so requiring both an impossible rate and a
# meaningful absolute jump costs no protection and removes the false
# positives.
MIN_SPIKE_KWH = 1.0

# Keys used in the filter sensor's RestoreEntity payload. The filter's state
# (how much energy it has counted, and where the source was when it last
# looked) has to survive a restart: rebuilding it from the config entry alone
# re-applies a stale offset to a source that has moved on.
STORE_NATIVE = "native"
STORE_LAST_SOURCE = "last_source"
STORE_LAST_SOURCE_TS = "last_source_ts"
STORE_SUPPRESSED = "suppressed_kwh"
STORE_UNIT = "unit"
STORE_APPLIED_OFFSET = "applied_initial_offset"
