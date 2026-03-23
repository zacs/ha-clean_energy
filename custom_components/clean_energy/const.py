"""Constants for Clean Energy."""

DOMAIN = "clean_energy"

CONF_MAX_POWER_KW = "max_power_kw"
CONF_ENTITY_ID = "entity_id"

SIGNAL_SPIKE_CORRECTED = f"{DOMAIN}_spike_corrected"

# 50 kW is very generous - covers large homes, EV chargers, etc.
# A single 200A residential service tops out around 48 kW.
DEFAULT_MAX_POWER_KW = 50.0

# Minimum elapsed time (seconds) between readings for rate calculation.
# Prevents division-by-near-zero with rapid-fire updates.
MIN_ELAPSED_SECONDS = 30.0
