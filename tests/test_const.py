"""Tests for Clean Energy constants and module sanity."""

from custom_components.clean_energy import const


def test_domain_matches_manifest() -> None:
    """The DOMAIN constant should match the folder + manifest domain."""
    assert const.DOMAIN == "clean_energy"


def test_threshold_default_is_reasonable() -> None:
    """The default kW threshold should be in a sensible residential range."""
    assert 10.0 <= const.DEFAULT_MAX_POWER_KW <= 200.0


def test_min_elapsed_seconds_positive() -> None:
    """Minimum elapsed seconds must be > 0 to avoid divide-by-zero."""
    assert const.MIN_ELAPSED_SECONDS > 0


def test_signal_namespaced() -> None:
    """The dispatcher signal should be namespaced under the domain."""
    assert const.SIGNAL_SPIKE_CORRECTED.startswith(const.DOMAIN)


def test_required_keys_exist() -> None:
    """Sanity-check that the public symbols the rest of the package imports exist."""
    for name in (
        "DOMAIN",
        "CONF_MAX_POWER_KW",
        "CONF_ENTITY_ID",
        "SIGNAL_SPIKE_CORRECTED",
        "SERVICE_MONITOR_SENSOR",
        "BACKFILL_DONE_KEY",
        "DEFAULT_MAX_POWER_KW",
        "MIN_ELAPSED_SECONDS",
    ):
        assert hasattr(const, name), f"const.{name} is missing"
