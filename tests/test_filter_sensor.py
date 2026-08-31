"""Unit tests for the CleanFilterSensor offset-accumulator algorithm.

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
        device_entry=None,
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
    assert inst._last_source == 100.0
    assert inst._suppressed == 0.0


async def test_normal_increment_is_passed_through(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A normal sub-threshold increase advances the emitted value."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(100.0, t0))
    # 1 kWh over 1 hour = 1 kW implied; well under 50.
    inst._handle_source_change(_state_change_event(101.0, t0 + timedelta(hours=1)))

    assert inst.native_value == 101.0
    assert inst._last_source == 101.0
    assert inst._suppressed == 0.0


async def test_permanent_spike_is_not_counted(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A massive jump is refused; the emitted value stays put."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(0.74, t0))
    # 3583+ kWh in 60s implies ~215,000 kW. Definitely a spike.
    inst._handle_source_change(_state_change_event(3584.0, t0 + timedelta(seconds=60)))

    assert inst.native_value == pytest.approx(0.74)
    assert inst._last_source == 3584.0
    assert inst._suppressed == pytest.approx(3583.26)


async def test_normal_increments_after_permanent_spike_still_track(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """After a spike is absorbed into offset, small real increments pass through.

    This is the core scenario: the meter spikes permanently to a bogus value
    and then continues monotonically increasing. The clean entity should keep
    tracking those real-world increments without re-tripping the filter.
    """
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(0.74, t0))
    inst._handle_source_change(_state_change_event(3584.0, t0 + timedelta(seconds=60)))

    # An hour later, the plug really did consume 0.1 kWh.
    inst._handle_source_change(
        _state_change_event(3584.1, t0 + timedelta(seconds=60) + timedelta(hours=1))
    )
    assert inst.native_value == pytest.approx(0.84)

    # Another hour, another 0.2 kWh on top.
    inst._handle_source_change(
        _state_change_event(3584.3, t0 + timedelta(seconds=60) + timedelta(hours=2))
    )
    assert inst.native_value == pytest.approx(1.04)
    assert inst._suppressed == pytest.approx(3583.26)


async def test_repeated_spikes_keep_accumulating_tally(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """Multiple distinct spikes should each add to the suppressed tally."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(0.0, t0))
    inst._handle_source_change(_state_change_event(1000.0, t0 + timedelta(seconds=60)))
    inst._handle_source_change(_state_change_event(2500.0, t0 + timedelta(seconds=120)))

    assert inst.native_value == pytest.approx(0.0)
    assert inst._suppressed == pytest.approx(2500.0)


async def test_each_spike_fires_dispatcher(
    hass: HomeAssistant,
    fixed_threshold: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each detected spike fires the dispatcher signal once with its jump size."""
    inst = _make_sensor(hass)
    sends: list[tuple] = []
    monkeypatch.setattr(
        sensor_module,
        "async_dispatcher_send",
        lambda _hass, signal, *args: sends.append((signal, args)),
    )

    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(0.0, t0))
    inst._handle_source_change(_state_change_event(1000.0, t0 + timedelta(seconds=60)))
    # A normal increment in between shouldn't fire.
    inst._handle_source_change(_state_change_event(1000.5, t0 + timedelta(hours=1)))
    inst._handle_source_change(
        _state_change_event(2500.5, t0 + timedelta(hours=1, seconds=60))
    )

    assert len(sends) == 2
    for signal, _args in sends:
        assert signal.endswith("_sensor.flaky")
    assert sends[0][1][0] == pytest.approx(1000.0)
    assert sends[1][1][0] == pytest.approx(1500.0)


async def test_real_reset_re_anchors_without_rewinding(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A drop in the source (e.g. Z-Wave manual meter reset) re-anchors us.

    We only ever added increments we accepted, so a source reset has nothing
    to unwind: the clean total holds and we adopt the new source level as the
    baseline for the next comparison.
    """
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_state_change_event(0.0, t0))
    inst._handle_source_change(_state_change_event(1000.0, t0 + timedelta(seconds=60)))
    assert inst._suppressed == pytest.approx(1000.0)

    # User issues a Z-Wave reset.
    inst._handle_source_change(_state_change_event(0.0, t0 + timedelta(hours=1)))

    assert inst.native_value == 0.0
    assert inst._last_source == 0.0
    # The suppressed tally is a lifetime counter, not part of the arithmetic.
    assert inst._suppressed == pytest.approx(1000.0)

    # Real consumption after the reset still counts.
    inst._handle_source_change(_state_change_event(0.4, t0 + timedelta(hours=2)))
    assert inst.native_value == pytest.approx(0.4)


async def test_unknown_source_state_is_ignored(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """`unknown` / `unavailable` source states must not perturb anything."""
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
    assert inst._last_source == 50.0
    assert inst._suppressed == 0.0


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
    assert inst._last_source == 50.0


async def test_diagnostics_aggregate_dispatcher_signals(
    hass: HomeAssistant,
) -> None:
    """The diagnostic sensors should accumulate state from spike signals."""
    entry = SimpleNamespace(entry_id="diag_entry", data={}, options={})
    common = (entry, "sensor.flaky", None, "Flaky")

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
