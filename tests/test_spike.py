"""Unit tests for the spike rules shared by the hub, filter and history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import State
from homeassistant.util.unit_conversion import EnergyConverter

from custom_components.clean_energy.spike import (
    ENERGY_UNITS,
    confirmed_at,
    continue_history,
    derive_clean_history,
    is_energy_sensor,
    is_spike,
    reading,
    to_kwh,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
ATTRS = {"unit_of_measurement": "kWh", "state_class": "total_increasing"}


def _state(value, *, changed=T0, reported=None, unit="kWh") -> State:
    return State(
        "sensor.lamp_energy",
        str(value),
        {**ATTRS, "unit_of_measurement": unit},
        last_changed=changed,
        last_reported=reported or changed,
        last_updated=reported or changed,
    )


def _hours(*increments, start_state=38.0, spike_at=(), gap_after=None):
    """Hourly source rows: (start, state, sum) with optional spikes."""
    rows, state, total = [], start_state, 0.0
    start = T0
    for i, inc in enumerate(increments):
        state += inc
        total += inc
        rows.append({"start": start.timestamp(), "state": state, "sum": total})
        start += timedelta(hours=1 if i != gap_after else 24)
    return rows


# -- units -------------------------------------------------------------------


def test_every_home_assistant_energy_unit_is_supported() -> None:
    """A gigajoule meter used to pass the hub but be ignored by the filter.

    Units come from Home Assistant's own converter, so ones it adds later
    (therms arrived after 2026.8) are picked up without a change here.
    """
    assert frozenset(EnergyConverter.VALID_UNITS) == ENERGY_UNITS
    for unit in ("J", "kJ", "MJ", "GJ", "Wh", "kWh", "MWh"):
        assert reading(_state(1.5, unit=unit)) == (1.5, unit)


def test_unit_conversion() -> None:
    assert to_kwh(1000.0, "Wh") == pytest.approx(1.0)
    assert to_kwh(1.0, "GJ") == pytest.approx(277.7778, rel=1e-6)


def test_non_energy_states_are_rejected() -> None:
    assert not is_energy_sensor(State("sensor.t", "21", {"unit_of_measurement": "°C"}))
    assert not is_energy_sensor(
        State("sensor.e", "21", {"unit_of_measurement": "kWh", "state_class": "total"})
    )
    assert is_energy_sensor(_state(1.0, unit="GJ"))


@pytest.mark.parametrize("raw", ["unknown", "unavailable", "garbage", "nan", "inf"])
def test_unusable_readings_are_ignored(raw: str) -> None:
    """NaN in particular would poison every later comparison for good."""
    assert reading(State("sensor.e", raw, ATTRS)) is None


# -- the spike test ----------------------------------------------------------


def test_small_jumps_are_never_spikes() -> None:
    """0.512 kWh in 20 s implies 92 kW, but is far too small to be a glitch."""
    assert not is_spike(0.512, 20, 50.0)


def test_large_fast_jumps_are_spikes() -> None:
    assert is_spike(1000.0, 60, 50.0)


def test_large_slow_jumps_are_not() -> None:
    """40 kWh over an hour is a big house, not a broken meter."""
    assert not is_spike(40.0, 3600, 50.0)


def test_a_lower_ceiling_catches_what_the_global_one_misses() -> None:
    """655 kWh over 14 h is 46.8 kW: real for a house, absurd for a lamp."""
    assert not is_spike(655.35, 14 * 3600, 50.0)
    assert is_spike(655.35, 14 * 3600, 3.0)


# -- how long the meter had --------------------------------------------------


def test_a_re_reported_value_shortens_the_window() -> None:
    """A lamp idle 14 h but re-reporting a minute ago only had a minute."""
    old = _state(38.0, changed=T0, reported=T0 + timedelta(hours=14))
    assert confirmed_at(old, 38.0, "kWh", T0) == T0 + timedelta(hours=14)


def test_a_value_seen_only_once_keeps_the_original_time() -> None:
    old = _state(38.0, changed=T0)
    assert confirmed_at(old, 38.0, "kWh", T0) == T0


def test_a_different_previous_value_is_not_trusted() -> None:
    old = _state(37.0, changed=T0, reported=T0 + timedelta(hours=1))
    assert confirmed_at(old, 38.0, "kWh", T0) == T0


def test_a_state_restored_at_startup_is_not_trusted() -> None:
    """Restored states have last_reported == last_changed == startup; trusting
    that would hide the downtime during which a meter really banks energy."""
    startup = T0 + timedelta(hours=10)
    old = _state(38.0, changed=startup, reported=startup)
    assert confirmed_at(old, 38.0, "kWh", T0) == T0


# -- deriving clean history --------------------------------------------------


def test_history_without_spikes_is_unchanged() -> None:
    hist = derive_clean_history(_hours(0.05, 0.05, 0.05, 0.05), "kWh", 50.0)
    assert hist is not None
    assert [r["sum"] for r in hist.rows] == pytest.approx([0.0, 0.05, 0.1, 0.15])
    assert hist.suppressed == 0.0
    assert hist.spike_hours == []


def test_history_sums_start_at_zero() -> None:
    """The dashboard measures the first row's change against 0, so a series
    that started anywhere else would show its start as one enormous hour."""
    hist = derive_clean_history(_hours(0.05, 9.0, 0.05), "kWh", 50.0)
    assert hist is not None
    assert hist.rows[0]["sum"] == 0.0


def test_a_permanent_spike_is_removed_from_history() -> None:
    """The reported case: 38 kWh, then a 234,881,286 kWh jump that stays."""
    rows = _hours(0.05, 0.05, 234881286.0, 0.05, 0.05)
    hist = derive_clean_history(rows, "kWh", 50.0)
    assert hist is not None
    deltas = [
        b["sum"] - a["sum"] for a, b in zip(hist.rows, hist.rows[1:], strict=False)
    ]
    assert deltas == pytest.approx([0.05, 0.0, 0.05, 0.05])
    assert hist.suppressed == pytest.approx(234881286.0)
    assert hist.spike_hours == [T0 + timedelta(hours=2)]
    # The clean reading tracks real consumption, not the spiked meter.
    assert hist.native == pytest.approx(38.0 + 0.05 * 4)
    assert hist.source_state == pytest.approx(38.0 + 0.05 * 4 + 234881286.0)


def test_a_reverted_spike_in_the_sum_is_removed() -> None:
    """A spike that comes and goes within an hour vanishes from the hourly
    state but stays in the recorder's sum, which the filter catches."""
    rows = _hours(0.05, 0.05, 0.05)
    rows[1]["sum"] += 1000.0
    rows[2]["sum"] += 1000.0
    hist = derive_clean_history(rows, "kWh", 50.0)
    assert hist is not None
    assert hist.total == pytest.approx(0.05)


def test_a_meter_reset_keeps_counting() -> None:
    """The recorder's sum is reset-aware; a falling state isn't consumption."""
    rows = _hours(0.05, 0.05, 0.05)
    rows[2]["state"] = 0.02  # meter reset, but the sum kept counting
    hist = derive_clean_history(rows, "kWh", 50.0)
    assert hist is not None
    assert hist.total == pytest.approx(0.1)


def test_a_hand_adjusted_falling_sum_counts_as_nothing() -> None:
    rows = _hours(0.05, 0.05, 0.05)
    rows[2]["sum"] -= 5.0
    hist = derive_clean_history(rows, "kWh", 50.0)
    assert hist is not None
    assert min(r["sum"] for r in hist.rows) >= 0.0


def test_gaps_are_measured_across_their_full_length() -> None:
    """60 kWh across a 24 h outage is 2.5 kW, not a spike."""
    rows = _hours(0.05, 60.0, 0.05, gap_after=0)
    hist = derive_clean_history(rows, "kWh", 50.0)
    assert hist is not None
    assert hist.suppressed == 0.0


def test_history_in_watt_hours() -> None:
    """The rate test runs in kWh whatever unit the meter uses."""
    rows = _hours(50.0, 50.0, 2_000_000.0, 50.0, start_state=38000.0)
    hist = derive_clean_history(rows, "Wh", 50.0)
    assert hist is not None
    assert hist.suppressed == pytest.approx(2_000_000.0)


def test_no_usable_rows() -> None:
    assert derive_clean_history([], "kWh", 50.0) is None
    assert (
        derive_clean_history([{"start": 0, "sum": None, "state": None}], "kWh", 50.0)
        is None
    )


# -- carrying history up to now ----------------------------------------------


def test_continues_with_real_consumption() -> None:
    hist = derive_clean_history(_hours(0.05, 0.05), "kWh", 50.0)
    assert hist is not None
    now = hist.end + timedelta(minutes=30)
    total, dropped = continue_history(hist, hist.source_state + 0.02, now, "kWh", 50.0)
    assert total == pytest.approx(hist.native + 0.02)
    assert dropped == 0.0


def test_a_spike_since_the_last_hour_is_dropped() -> None:
    """The spike that triggers discovery is usually still in the open hour."""
    hist = derive_clean_history(_hours(0.05, 0.05), "kWh", 50.0)
    assert hist is not None
    now = hist.end + timedelta(minutes=5)
    total, dropped = continue_history(
        hist, hist.source_state + 655.35, now, "kWh", 50.0
    )
    assert total == pytest.approx(hist.native)
    assert dropped == pytest.approx(655.35)


def test_a_reading_below_the_last_hour_re_anchors() -> None:
    hist = derive_clean_history(_hours(0.05, 0.05), "kWh", 50.0)
    assert hist is not None
    total, _ = continue_history(hist, 1.0, hist.end, "kWh", 50.0)
    assert total == pytest.approx(hist.native)
