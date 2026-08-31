"""Regressions for the breakage reported against Home Assistant 2026.8.

Each test here corresponds to a line in the reported log:

* the clean sensor reporting a negative ``total_increasing`` state, which
  makes the recorder discard every statistics row for it (so the Energy
  Dashboard shows nothing at all);
* the ``async_device_info_to_link_from_entity`` deprecation, which now
  always returns None;
* a 0.512 kWh jump being filtered as a 61 kW "spike".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.util import dt as dt_util

from custom_components.clean_energy import sensor as sensor_module
from custom_components.clean_energy.const import (
    CONF_INITIAL_OFFSET,
    STORE_APPLIED_OFFSET,
    STORE_LAST_SOURCE,
    STORE_LAST_SOURCE_TS,
    STORE_NATIVE,
    STORE_SUPPRESSED,
    STORE_UNIT,
)
from custom_components.clean_energy.sensor import (
    CleanFilterSensor,
    SpikeCountSensor,
    TotalCorrectedSensor,
)

SOURCE_ID = "sensor.flaky"


def _make_sensor(
    hass: HomeAssistant,
    *,
    data: dict | None = None,
    restored: dict | None = None,
) -> CleanFilterSensor:
    """Build a filter sensor with optional entry data and restored state."""
    entry = SimpleNamespace(entry_id="test_entry", data=data or {}, options={})
    inst = CleanFilterSensor(
        entry=entry,
        source_id=SOURCE_ID,
        device_entry=None,
        parent_friendly="Flaky",
    )
    inst.hass = hass
    inst.entity_id = "sensor.flaky_clean"
    inst.async_write_ha_state = MagicMock()
    inst.async_on_remove = MagicMock()
    inst.async_get_last_extra_data = _async_return(
        RestoredExtraData(restored) if restored is not None else None
    )
    # The entry is a stand-in, so keep the config-entry write side-effect free.
    inst._clear_configured_offset = MagicMock(
        side_effect=lambda: setattr(inst, "_configured_offset", 0.0)
    )
    return inst


def _async_return(value):
    """Build a zero-arg coroutine function returning ``value``."""

    async def _inner():
        return value

    return _inner


def _event(value: float, when: datetime, unit: str = UnitOfEnergy.KILO_WATT_HOUR):
    """Build a minimal state-change event."""
    return SimpleNamespace(
        data={
            "new_state": SimpleNamespace(
                state=str(value),
                attributes={"unit_of_measurement": unit},
                last_changed=when,
            )
        }
    )


def _set_source(hass: HomeAssistant, value: float, unit: str = "kWh") -> None:
    """Publish a source-sensor state."""
    hass.states.async_set(
        SOURCE_ID,
        str(value),
        {"unit_of_measurement": unit, "state_class": "total_increasing"},
    )


@pytest.fixture
def fixed_threshold(monkeypatch: pytest.MonkeyPatch) -> float:
    """Pin the hub threshold to 50 kW regardless of config-entry state."""
    monkeypatch.setattr(sensor_module, "_hub_max_power_kw", lambda _hass: 50.0)
    return 50.0


# ---------------------------------------------------------------------------
# Negative total_increasing state
# ---------------------------------------------------------------------------


async def test_reverted_spike_does_not_go_negative(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """The reported bug: a stale offset drove the clean value to -14,316,553.

    The entry was created by discovery while the source was spiked, so a
    huge ``initial_offset`` was baked in. The source then reverted to its
    true value, but the offset was still subtracted from every reading.
    """
    inst = _make_sensor(hass, data={CONF_INITIAL_OFFSET: 14316560.0})
    _set_source(hass, 6.07)
    await hass.async_block_till_done()

    await inst.async_added_to_hass()
    assert inst.native_value >= 0

    inst._handle_source_change(_event(6.14, dt_util.utcnow() + timedelta(hours=1)))
    assert inst.native_value >= 0


async def test_stale_offset_larger_than_source_is_discarded(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """An offset exceeding the source is stale, so it is dropped, not clamped.

    Clamping to zero would silently throw away the sensor's real accumulated
    total. The offset only ever meant "cancel the spike currently sitting in
    the source"; if the source now reads below it, that spike is gone.
    """
    inst = _make_sensor(hass, data={CONF_INITIAL_OFFSET: 14316560.0})
    _set_source(hass, 6.07)
    await hass.async_block_till_done()

    await inst.async_added_to_hass()

    assert inst.native_value == pytest.approx(6.07)
    assert inst._configured_offset == 0.0
    inst._clear_configured_offset.assert_called_once()


async def test_valid_offset_is_still_applied(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """An offset the source can still account for is applied as before."""
    inst = _make_sensor(hass, data={CONF_INITIAL_OFFSET: 3583.26})
    _set_source(hass, 3584.0)
    await hass.async_block_till_done()

    await inst.async_added_to_hass()

    assert inst.native_value == pytest.approx(0.74)
    inst._clear_configured_offset.assert_not_called()


async def test_value_never_decreases_across_a_reverting_spike(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A transient spike-and-revert must not move the clean total at all."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_event(38.092, t0))
    # The reported boys_room_lamp spike.
    inst._handle_source_change(_event(234881324.0, t0 + timedelta(seconds=60)))
    assert inst.native_value == pytest.approx(38.092)

    # ...and the meter falls back to reality a few minutes later.
    inst._handle_source_change(_event(38.164, t0 + timedelta(minutes=5)))
    assert inst.native_value == pytest.approx(38.092)

    # Real consumption from there still accrues.
    inst._handle_source_change(_event(38.264, t0 + timedelta(hours=2)))
    assert inst.native_value == pytest.approx(38.192)


# ---------------------------------------------------------------------------
# Restart durability
# ---------------------------------------------------------------------------


async def test_state_survives_a_restart(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """The accumulator is restored, not rebuilt from the entry's offset.

    Rebuilding meant a restart re-applied the one-time discovery offset to a
    source that had long since moved on.
    """
    t0 = dt_util.utcnow() - timedelta(hours=3)
    restored = {
        STORE_NATIVE: 12.5,
        STORE_LAST_SOURCE: 3596.5,
        STORE_LAST_SOURCE_TS: t0.isoformat(),
        STORE_SUPPRESSED: 3583.26,
        STORE_UNIT: UnitOfEnergy.KILO_WATT_HOUR,
        STORE_APPLIED_OFFSET: 3583.26,
    }
    inst = _make_sensor(hass, data={CONF_INITIAL_OFFSET: 3583.26}, restored=restored)
    _set_source(hass, 3596.5)
    await hass.async_block_till_done()

    await inst.async_added_to_hass()

    assert inst.native_value == pytest.approx(12.5)
    assert inst._last_source == pytest.approx(3596.5)
    assert inst._suppressed == pytest.approx(3583.26)

    # And it keeps accumulating from there rather than restarting at 0.74.
    inst._handle_source_change(_event(3596.6, dt_util.utcnow()))
    assert inst.native_value == pytest.approx(12.6)


async def test_editing_the_offset_forces_a_reseed(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """Changing the offset in the options flow is a request to re-seed.

    Otherwise the restored total would win and the repair would do nothing.
    """
    restored = {
        STORE_NATIVE: 5000.0,
        STORE_LAST_SOURCE: 5000.0,
        STORE_LAST_SOURCE_TS: dt_util.utcnow().isoformat(),
        STORE_SUPPRESSED: 0.0,
        STORE_APPLIED_OFFSET: 0.0,
    }
    inst = _make_sensor(hass, data={CONF_INITIAL_OFFSET: 4900.0}, restored=restored)
    _set_source(hass, 5000.0)
    await hass.async_block_till_done()

    await inst.async_added_to_hass()

    assert inst.native_value == pytest.approx(100.0)


async def test_restart_gap_is_measured_from_the_restored_timestamp(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """Downtime counts toward elapsed, so a batched reading isn't a spike.

    A meter that reports the energy it accumulated while Home Assistant was
    down looks like a huge instantaneous jump if the clock restarts at boot.
    """
    t0 = dt_util.utcnow() - timedelta(hours=6)
    inst = _make_sensor(
        hass,
        restored={
            STORE_NATIVE: 66.594,
            STORE_LAST_SOURCE: 66.594,
            STORE_LAST_SOURCE_TS: t0.isoformat(),
            STORE_SUPPRESSED: 0.0,
            STORE_APPLIED_OFFSET: 0.0,
        },
    )
    _set_source(hass, 66.594)
    await hass.async_block_till_done()
    await inst.async_added_to_hass()

    # 6 hours of downtime, then the meter reports the lot: ~1.3 kW, not 61 kW.
    inst._handle_source_change(_event(74.594, dt_util.utcnow()))

    assert inst._suppressed == 0.0
    assert inst.native_value == pytest.approx(74.594)


# ---------------------------------------------------------------------------
# Spike-detection false positives
# ---------------------------------------------------------------------------


async def test_small_jump_is_not_a_spike(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """The reported garage_dehumidifier false positive: 0.512 kWh at 61.4 kW.

    The rate test alone flags it, because MIN_ELAPSED_SECONDS floors the
    denominator at 30s. A jump that small cannot be a broken meter, so the
    absolute floor keeps it.
    """
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_event(66.594, t0))
    inst._handle_source_change(_event(67.106, t0 + timedelta(seconds=20)))

    assert inst._suppressed == 0.0
    assert inst.native_value == pytest.approx(67.106)


async def test_large_jump_is_still_a_spike(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """The absolute floor must not blunt the filter for real spikes."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_event(66.594, t0))
    inst._handle_source_change(_event(1066.594, t0 + timedelta(seconds=20)))

    assert inst._suppressed == pytest.approx(1000.0)
    assert inst.native_value == pytest.approx(66.594)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


async def test_unit_change_rescales_the_running_total(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """If the source switches units, the total converts with it."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_event(2.0, t0, unit=UnitOfEnergy.KILO_WATT_HOUR))
    inst._handle_source_change(
        _event(2500.0, t0 + timedelta(hours=1), unit=UnitOfEnergy.WATT_HOUR)
    )

    assert inst.native_unit_of_measurement == UnitOfEnergy.WATT_HOUR
    assert inst.native_value == pytest.approx(2500.0)


async def test_watt_hour_source_is_not_over_filtered(
    hass: HomeAssistant, fixed_threshold: float
) -> None:
    """A Wh source jumping 900 Wh in an hour is 0.9 kW, not a spike."""
    inst = _make_sensor(hass)
    t0 = dt_util.utcnow()
    inst._handle_source_change(_event(1000.0, t0, unit=UnitOfEnergy.WATT_HOUR))
    inst._handle_source_change(
        _event(1900.0, t0 + timedelta(hours=1), unit=UnitOfEnergy.WATT_HOUR)
    )

    assert inst._suppressed == 0.0
    assert inst.native_value == pytest.approx(1900.0)


# ---------------------------------------------------------------------------
# Diagnostics restore
# ---------------------------------------------------------------------------


async def test_diagnostic_tallies_survive_a_restart(hass: HomeAssistant) -> None:
    """Spike Count and Energy Removed are lifetime tallies, so they restore."""
    entry = SimpleNamespace(entry_id="diag_entry", data={}, options={})

    total = TotalCorrectedSensor(entry, SOURCE_ID, None, "Flaky")
    count = SpikeCountSensor(entry, SOURCE_ID, None, "Flaky")

    for inst, prior in ((total, "4.0"), (count, "2")):
        inst.hass = hass
        inst.entity_id = f"sensor.flaky_{inst._id_suffix}"
        inst.async_write_ha_state = MagicMock()
        inst.async_on_remove = MagicMock()
        inst.async_get_last_state = _async_return(
            SimpleNamespace(state=prior, attributes={})
        )
        await inst.async_added_to_hass()

    assert total.native_value == pytest.approx(4.0)
    assert count.native_value == 2

    total._handle_spike(1.5, dt_util.utcnow())
    count._handle_spike(1.5, dt_util.utcnow())

    assert total.native_value == pytest.approx(5.5)
    assert count.native_value == 3
