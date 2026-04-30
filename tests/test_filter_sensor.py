"""Unit tests for the CleanFilterSensor spike filter algorithm.

These tests avoid the full config-entry setup pipeline and exercise the
state-change handler directly. The algorithm is the part that matters for
correctness, and it's pure logic over a few pieces of state.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.clean_energy import sensor as sensor_module
from custom_components.clean_energy.sensor import (
    CleanFilterSensor,
    LastSpikeSizeSensor,
    LastSpikeTimeSensor,
    SpikeCountSensor,
    TotalCorrectedSensor,
)


def _make_sensor(hass: HomeAssistant) -> CleanFilterSensor:
    """Build a CleanFilterSensor wired to a hass instance, no entity registry."""
    entry = SimpleNamespace(entry_id="test_entry", data={}, options={})
    inst = CleanFilterSensor(
        entry=entry,
        source_id="sensor.flaky",
        device_info=None,
        parent_object_id="flaky",
        parent_friendly="Flaky",
    )
    inst.hass = hass
    inst.entity_id = "sensor.flaky_clean"
    # Replace the HA state-write with a no-op; we don't need a registered entity.
    inst.async_write_ha_state = MagicMock()
    return inst


def _state_change_event(value: float, when: datetime) -> SimpleNamespace:
    """Build a minimal state-change event for the filter to consume."""
    new_state = SimpleNamespace(
        state=str(value),
        attributes={"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR},
        last_changed=when,
    )
    return SimpleNamespace(data={"new_state": new_state})


@pytest.fixture
def fixed_threshold(monkeypatch: pytest.MonkeyPatch) -> float:
    """Pin the hub threshold to 50 kW regardless of config-entry state."""
    threshold = 50.0
    monkeypatch.setattr(sensor_module, "_hub_max_power_kw", lambda _hass: threshold)
    return threshold


async def test_first_reading_seeds_baseline(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """The first observed reading should be adopted as the baseline."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()

    inst._handle_source_change(_state_change_event(100.0, t0))

    assert inst.native_value == 100.0
    assert inst._last_good_value == 100.0
    assert inst._spike_active is False


async def test_normal_increment_is_passed_through(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A normal sub-threshold increase advances the baseline."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(100.0, t0))
    # 1 kWh over 1 hour = 1 kW implied; well under 50.
    inst._handle_source_change(_state_change_event(101.0, t0 + timedelta(hours=1)))

    assert inst.native_value == 101.0
    assert inst._last_good_value == 101.0
    assert inst._spike_active is False


async def test_obvious_spike_is_held(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A jump implying >> threshold kW must hold the emitted state flat."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(100.0, t0))
    # 5000 kWh in 30s implies ~600,000 kW. Definitely a spike.
    inst._handle_source_change(_state_change_event(5100.0, t0 + timedelta(seconds=30)))

    assert inst.native_value == 100.0  # held
    assert inst._last_good_value == 100.0  # baseline NOT advanced
    assert inst._spike_active is True


async def test_spike_then_revert_recovers_cleanly(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """After a spike-and-revert, normal readings resume tracking."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(100.0, t0))
    inst._handle_source_change(_state_change_event(5100.0, t0 + timedelta(seconds=30)))
    # Source returns to a sane value: drop triggers the "real reset" branch.
    inst._handle_source_change(_state_change_event(100.5, t0 + timedelta(seconds=60)))

    assert inst.native_value == 100.5
    assert inst._last_good_value == 100.5
    assert inst._spike_active is False


async def test_multi_tick_spike_only_fires_dispatcher_once(
    hass: HomeAssistant,
    fixed_threshold: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spike that persists across multiple ticks should fire once per event."""
    inst = _make_sensor(hass)
    sends: list[tuple] = []
    monkeypatch.setattr(
        sensor_module,
        "async_dispatcher_send",
        lambda _hass, signal, *args: sends.append((signal, args)),
    )

    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(100.0, t0))
    inst._handle_source_change(_state_change_event(5100.0, t0 + timedelta(seconds=30)))
    inst._handle_source_change(_state_change_event(5101.0, t0 + timedelta(seconds=60)))
    inst._handle_source_change(_state_change_event(5102.0, t0 + timedelta(seconds=90)))

    assert len(sends) == 1
    signal, _args = sends[0]
    assert signal.endswith("_sensor.flaky")


async def test_real_reset_is_adopted(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A drop in the source (e.g. meter rollover) is adopted as new baseline."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(1000.0, t0))
    inst._handle_source_change(_state_change_event(0.5, t0 + timedelta(hours=1)))

    assert inst.native_value == 0.5
    assert inst._last_good_value == 0.5
    assert inst._spike_active is False


async def test_unknown_source_state_is_ignored(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """`unknown` / `unavailable` source states must not perturb the baseline."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(50.0, t0))

    bad_state = SimpleNamespace(
        state="unavailable",
        attributes={"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR},
        last_changed=t0 + timedelta(seconds=30),
    )
    inst._handle_source_change(SimpleNamespace(data={"new_state": bad_state}))

    assert inst.native_value == 50.0
    assert inst._last_good_value == 50.0


async def test_unknown_unit_is_ignored(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A reading with an unrecognized unit should be skipped, not crash."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(50.0, t0))

    weird = SimpleNamespace(
        state="51.0",
        attributes={"unit_of_measurement": "foo"},
        last_changed=t0 + timedelta(seconds=30),
    )
    inst._handle_source_change(SimpleNamespace(data={"new_state": weird}))

    assert inst.native_value == 50.0
    assert inst._last_good_value == 50.0


async def test_diagnostics_aggregate_dispatcher_signals(
    hass: HomeAssistant,
) -> None:
    """The diagnostic sensors should accumulate state from spike signals."""
    entry = SimpleNamespace(entry_id="diag_entry", data={}, options={})
    common = (entry, "sensor.flaky", None, "flaky", "Flaky")

    last_time = LastSpikeTimeSensor(*common)
    last_size = LastSpikeSizeSensor(*common)
    total = TotalCorrectedSensor(*common)
    count = SpikeCountSensor(*common)

    for s in (last_time, last_size, total, count):
        s.hass = hass
        s.entity_id = f"sensor.flaky_{s._id_suffix}"
        s.async_write_ha_state = MagicMock()

    when = dt_util.utcnow()
    last_time._handle_spike(2.5, when)
    last_size._handle_spike(2.5, when)
    total._handle_spike(2.5, when)
    total._handle_spike(1.5, when + timedelta(seconds=10))
    count._handle_spike(2.5, when)
    count._handle_spike(1.5, when + timedelta(seconds=10))

    assert last_time.native_value == when
    assert last_size.native_value == 2.5
    assert total.native_value == pytest.approx(4.0)
    assert count.native_value == 2
