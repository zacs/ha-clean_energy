"""Constants for Clean Energy."""

from datetime import timedelta
from enum import IntFlag

DOMAIN = "clean_energy"

CONF_MAX_POWER_KW = "max_power_kw"
CONF_ENTITY_ID = "entity_id"
# Energy (in the source's unit) that the hub saw arrive in the spike which
# triggered discovery. Only used to seed a clean sensor whose source has no
# recorded history yet; otherwise the seed is derived from that history.
CONF_INITIAL_OFFSET = "initial_offset"

SIGNAL_SPIKE_CORRECTED = f"{DOMAIN}_spike_corrected"

SERVICE_MONITOR_SENSOR = "monitor_sensor"
SERVICE_REBUILD_HISTORY = "rebuild_history"
ATTR_OFFSET = "offset"
ATTR_FORCE = "force"


class CleanEnergyFeature(IntFlag):
    """Entity features, used to target entity actions.

    Only the (Clean) sensor can rebuild its history. Flagging it lets
    "all", device and area targets skip the diagnostic sensors, rather than
    fail the whole action on the first one.
    """

    REBUILD_HISTORY = 1


# Stable unique_id for the singleton hub config entry. Any per-sensor entries
# use the source entity_id as their unique_id.
HUB_UNIQUE_ID = f"{DOMAIN}_hub"

# Marker set by versions before 0.4.0 once they had copied the source's
# statistics, spikes included. Entries carrying it are left alone until the
# user asks for a rebuild, since a rebuild rewrites existing history.
LEGACY_BACKFILL_DONE_KEY = "backfill_done"
# Marker set once the clean sensor's statistics have been derived from the
# source's history with spikes removed.
HISTORY_DONE_KEY = "history_done"

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

# How far the running total may drift from the value derived from history
# before a rebuild corrects it. Below this, the difference is the expected
# granularity gap between per-reading and per-hour filtering, not a spike.
RESEED_TOLERANCE_KWH = MIN_SPIKE_KWH

# Upper bound on waiting for the recorder to compile the five-minute window in
# which a corrected value was introduced (see history.py). The compile runs at
# ten seconds past every fifth minute, so this leaves room for a busy recorder.
COMPILE_WAIT_TIMEOUT = timedelta(minutes=15)

# Keys used in the filter sensor's RestoreEntity payload. The filter's state
# (how much energy it has counted, and where the source was when it last
# looked) has to survive a restart: rebuilding it from the config entry alone
# re-applies a stale offset to a source that has moved on.
STORE_NATIVE = "native"
STORE_LAST_SOURCE = "last_source"
STORE_LAST_SOURCE_TS = "last_source_ts"
STORE_SUPPRESSED = "suppressed_kwh"
STORE_UNIT = "unit"
